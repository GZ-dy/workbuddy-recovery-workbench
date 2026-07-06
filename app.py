from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
from email.message import EmailMessage
import hashlib
import os
from pathlib import Path
import re
import shutil
import smtplib
import sqlite3
import sys
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rules import (
    focus_items,
    is_same_alarm,
    judge_probe_result,
    openapi_database_network_alarm,
    openapi_signal_probe_command,
    parse_alarm,
    recommended_probe_commands,
    recommended_probe_items,
)


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))
LEGACY_DB_PATH = BASE_DIR / ".data" / "workbench.db"
DEFAULT_DB_PATH = BASE_DIR / "runtime_data" / "workbench.db"
DB_PATH = Path(os.environ.get("WORKBENCH_DB", DEFAULT_DB_PATH))
OBSERVATION_MINUTES = int(os.environ.get("OBSERVATION_MINUTES", "10"))

from shared.manual_search import MANUAL_INDEX_PATH as SHARED_MANUAL_INDEX_PATH, search_manuals

app = FastAPI(title="AI故障恢复验证工作台")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class AlarmCreate(BaseModel):
    raw_text: str


class SmsImportCreate(BaseModel):
    raw_text: str
    sender: str = "人工转发"
    import_source: str = "manual_forward"
    force_new_event: bool = False


class ProbeResultCreate(BaseModel):
    raw_result: str


class ObservationAlarmCreate(BaseModel):
    raw_text: str


class ConfirmNoAlarmCreate(BaseModel):
    confirmed_by: str = "值班人员"
    remark: str = "截至当前确认时未收到同类BOMC短信"


class ReportEmailCreate(BaseModel):
    to: str = ""
    cc: str = ""
    subject: str = ""
    dry_run: bool = False


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db() -> sqlite3.Connection:
    ensure_db_storage()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db_storage() -> None:
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists() or db_path == LEGACY_DB_PATH or db_path != DEFAULT_DB_PATH or not LEGACY_DB_PATH.exists():
        return
    shutil.copy2(LEGACY_DB_PATH, db_path)


def init_db() -> None:
    with closing(get_db()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alarm_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              raw_text TEXT NOT NULL,
              source TEXT DEFAULT 'BOMC短信',
              alarm_time TEXT,
              system_name TEXT,
              alarm_level TEXT,
              object_name TEXT,
              alarm_content TEXT,
              alarm_type TEXT,
              metric_name TEXT,
              current_value TEXT,
              fingerprint TEXT,
              status TEXT DEFAULT 'new_alarm',
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS probe_results (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              alarm_id INTEGER NOT NULL,
              raw_result TEXT NOT NULL,
              probe_item TEXT,
              probe_status TEXT,
              ai_summary TEXT,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observation_windows (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              alarm_id INTEGER NOT NULL,
              start_time TEXT,
              end_time TEXT,
              status TEXT DEFAULT 'observing',
              same_alarm_received INTEGER DEFAULT 0,
              manual_confirm_no_alarm INTEGER DEFAULT 0,
              final_conclusion TEXT,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sms_inbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              raw_text TEXT NOT NULL,
              sender TEXT,
              received_at TEXT,
              import_source TEXT,
              text_hash TEXT,
              fingerprint TEXT,
              linked_alarm_id INTEGER,
              status TEXT DEFAULT 'created',
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        repair_retryable_probe_failures(conn)


def repair_retryable_probe_failures(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE alarm_events
        SET status = 'waiting_probe_result'
        WHERE status = 'failed'
          AND EXISTS (
            SELECT 1
            FROM observation_windows w
            WHERE w.alarm_id = alarm_events.id
              AND w.same_alarm_received = 1
          )
        """
    )
    conn.execute(
        """
        UPDATE alarm_events
        SET status = 'waiting_probe_result'
        WHERE status = 'failed'
          AND EXISTS (
            SELECT 1
            FROM probe_results p
            WHERE p.alarm_id = alarm_events.id
              AND p.id = (
                SELECT MAX(id)
                FROM probe_results
                WHERE alarm_id = alarm_events.id
              )
              AND p.probe_status = 'failed'
          )
          AND NOT EXISTS (
            SELECT 1
            FROM observation_windows w
            WHERE w.alarm_id = alarm_events.id
              AND (w.same_alarm_received = 1 OR w.status = 'failed')
          )
        """
    )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def text_hash(text: str) -> str:
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def split_sms_batch(raw_text: str) -> list[str]:
    text = (raw_text or "").strip()
    if not text:
        return []
    text = re.sub(r"\s*\[BOMC\]\s*", "\n\n", text, flags=re.I)
    items: list[str] = []
    for block in re.split(r"\n\s*\n+", text):
        block = block.strip()
        if not block:
            continue
        parts = [p.strip() for p in re.split(r"(?=(?:\[BOMC\]|BOMC\s))", block, flags=re.I) if p.strip()]
        items.extend(parts or [block])
    return items


def create_alarm_from_raw(raw_text: str, source: str = "BOMC短信", status: str = "waiting_manual_recovery") -> dict[str, Any]:
    parsed = parse_alarm(raw_text)
    with closing(get_db()) as conn, conn:
        cur = conn.execute(
            """
            INSERT INTO alarm_events (
                raw_text, source, alarm_time, system_name, alarm_level, object_name,
                alarm_content, alarm_type, metric_name, current_value, fingerprint, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_text,
                source,
                parsed["alarm_time"],
                parsed["system_name"],
                parsed["alarm_level"],
                parsed["object_name"],
                parsed["alarm_content"],
                parsed["alarm_type"],
                parsed["metric_name"],
                parsed["current_value"],
                parsed["fingerprint"],
                status,
                now_text(),
            ),
        )
        alarm_id = cur.lastrowid
    return require_alarm(alarm_id)


def insert_sms(raw_text: str, sender: str, import_source: str, parsed: dict[str, str], status: str, alarm_id: int | None) -> int:
    created_at = now_text()
    with closing(get_db()) as conn, conn:
        cur = conn.execute(
            """
            INSERT INTO sms_inbox (
                raw_text, sender, received_at, import_source, text_hash,
                fingerprint, linked_alarm_id, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_text,
                sender,
                created_at,
                import_source,
                text_hash(raw_text),
                parsed["fingerprint"],
                alarm_id,
                status,
                created_at,
            ),
        )
        return int(cur.lastrowid)


def find_duplicate_sms(raw_text: str) -> dict[str, Any] | None:
    with closing(get_db()) as conn, conn:
        row = conn.execute(
            """
            SELECT * FROM sms_inbox
            WHERE text_hash = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (text_hash(raw_text),),
        ).fetchone()
    return row_to_dict(row)


def find_open_alarm_by_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    with closing(get_db()) as conn, conn:
        row = conn.execute(
            """
            SELECT * FROM alarm_events
            WHERE fingerprint = ?
              AND status NOT IN ('recovered', 'failed')
            ORDER BY id DESC
            LIMIT 1
            """,
            (fingerprint,),
        ).fetchone()
    return row_to_dict(row)


def find_observing_same_alarm(parsed: dict[str, str]) -> dict[str, Any] | None:
    with closing(get_db()) as conn, conn:
        rows = conn.execute(
            """
            SELECT a.*, w.id AS window_id
            FROM observation_windows w
            JOIN alarm_events a ON a.id = w.alarm_id
            WHERE w.status = 'observing'
            ORDER BY w.id DESC
            """
        ).fetchall()
    for row in rows:
        alarm = row_to_dict(row) or {}
        if is_same_alarm(alarm, parsed):
            return alarm
    return None


def mark_observation_retry(alarm_id: int, reason: str) -> None:
    with closing(get_db()) as conn, conn:
        conn.execute("UPDATE alarm_events SET status = 'waiting_probe_result' WHERE id = ?", (alarm_id,))
        conn.execute(
            """
            UPDATE observation_windows
            SET status = 'failed',
                same_alarm_received = 1,
                end_time = ?,
                final_conclusion = ?
            WHERE id = (
                SELECT id FROM observation_windows
                WHERE alarm_id = ?
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (now_text(), reason, alarm_id),
        )


def import_sms_text(
    raw_text: str,
    sender: str = "人工转发",
    import_source: str = "manual_forward",
    force_new_event: bool = False,
) -> dict[str, Any]:
    init_db()
    messages = split_sms_batch(raw_text)
    result = {"imported": len(messages), "created": 0, "merged": 0, "duplicates": 0, "observation_failures": 0, "items": []}

    for message in messages:
        parsed = parse_alarm(message)
        duplicate = None if force_new_event else find_duplicate_sms(message)
        if duplicate:
            sms_id = insert_sms(message, sender, import_source, parsed, "duplicate", duplicate.get("linked_alarm_id"))
            result["duplicates"] += 1
            result["items"].append({"sms_id": sms_id, "alarm_id": duplicate.get("linked_alarm_id"), "status": "duplicate"})
            continue

        observing_alarm = find_observing_same_alarm(parsed)
        if observing_alarm:
            reason = "观察期内再次收到同类告警，请继续排障并重新拨测"
            alarm_id = int(observing_alarm["id"])
            mark_observation_retry(alarm_id, reason)
            sms_id = insert_sms(message, sender, import_source, parsed, "observation_same_alarm", alarm_id)
            result["observation_failures"] += 1
            result["items"].append({"sms_id": sms_id, "alarm_id": alarm_id, "status": "observation_same_alarm"})
            continue

        open_alarm = None if force_new_event else find_open_alarm_by_fingerprint(parsed["fingerprint"])
        if open_alarm:
            alarm_id = int(open_alarm["id"])
            sms_id = insert_sms(message, sender, import_source, parsed, "merged_duplicate", alarm_id)
            result["merged"] += 1
            result["items"].append({"sms_id": sms_id, "alarm_id": alarm_id, "status": "merged_duplicate"})
            continue

        alarm = create_alarm_from_raw(message)
        sms_id = insert_sms(message, sender, import_source, parsed, "created", int(alarm["id"]))
        result["created"] += 1
        result["items"].append(
            {
                "sms_id": sms_id,
                "alarm_id": alarm["id"],
                "status": "created",
                "alarm_type": alarm["alarm_type"],
                "system_name": alarm["system_name"],
            }
        )
    return result


def list_sms_inbox(limit: int = 50) -> dict[str, Any]:
    init_db()
    with closing(get_db()) as conn, conn:
        rows = conn.execute(
            """
            SELECT s.*, a.system_name, a.alarm_type, a.status AS alarm_status
            FROM sms_inbox s
            LEFT JOIN alarm_events a ON a.id = s.linked_alarm_id
            ORDER BY s.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"items": [row_to_dict(row) for row in rows]}


def list_alarms() -> dict[str, Any]:
    init_db()
    with closing(get_db()) as conn, conn:
        rows = conn.execute(
            """
            SELECT * FROM alarm_events
            ORDER BY
              CASE status
                WHEN 'waiting_manual_recovery' THEN 0
                WHEN 'observing' THEN 1
                WHEN 'failed' THEN 2
                ELSE 3
              END,
              id DESC
            """
        ).fetchall()
    return {"items": [row_to_dict(row) for row in rows]}


def require_alarm(alarm_id: int) -> dict[str, Any]:
    init_db()
    with closing(get_db()) as conn, conn:
        row = conn.execute("SELECT * FROM alarm_events WHERE id = ?", (alarm_id,)).fetchone()
    alarm = row_to_dict(row)
    if not alarm:
        raise HTTPException(status_code=404, detail="告警不存在")
    return alarm


def fetch_probe_results(alarm_id: int) -> list[dict[str, Any]]:
    with closing(get_db()) as conn, conn:
        rows = conn.execute("SELECT * FROM probe_results WHERE alarm_id = ? ORDER BY id", (alarm_id,)).fetchall()
    return [row_to_dict(row) for row in rows]


def fetch_latest_window(alarm_id: int) -> dict[str, Any] | None:
    with closing(get_db()) as conn, conn:
        row = conn.execute(
            "SELECT * FROM observation_windows WHERE alarm_id = ? ORDER BY id DESC LIMIT 1",
            (alarm_id,),
        ).fetchone()
    return row_to_dict(row)


def fetch_alarm_sms(alarm_id: int) -> list[dict[str, Any]]:
    with closing(get_db()) as conn, conn:
        rows = conn.execute("SELECT * FROM sms_inbox WHERE linked_alarm_id = ? ORDER BY id", (alarm_id,)).fetchall()
    return [row_to_dict(row) for row in rows]


def get_alarm(alarm_id: int) -> dict[str, Any]:
    alarm = require_alarm(alarm_id)
    system = detect_system(alarm)
    probe_commands = manual_probe_commands_for_alarm(alarm) or recommended_probe_commands(alarm)
    return {
        "alarm": alarm,
        "detected_system": system,
        "system_hints": system_aliases_for_display(system),
        "focus_items": focus_items(alarm),
        "recommended_probe_items": recommended_probe_items(alarm),
        "recommended_probe_commands": probe_commands,
        "manual_links": manual_links_for_alarm(alarm),
        "probe_results": fetch_probe_results(alarm_id),
        "observation_window": fetch_latest_window(alarm_id),
        "sms_messages": fetch_alarm_sms(alarm_id),
        "report_meta": build_report_meta(alarm_id),
        "report": build_report(alarm_id),
    }


def probe_commands_for_context(system: str = "", text: str = "") -> list[dict[str, str]]:
    context = normalize_text(f"{system} {text}".strip()) or "通用告警"
    if system and not text:
        context = f"{system} 系统无法打开"
    alarm = parse_alarm(context)
    if system:
        alarm["system_name"] = system
    alarm["raw_text"] = context
    alarm["alarm_content"] = context
    return (manual_probe_commands_for_alarm(alarm) if text else []) or recommended_probe_commands(alarm)


def mark_manual_recovery_done(alarm_id: int) -> dict[str, Any]:
    require_alarm(alarm_id)
    with closing(get_db()) as conn, conn:
        conn.execute("UPDATE alarm_events SET status = 'waiting_probe_result' WHERE id = ?", (alarm_id,))
    return {"status": "waiting_probe_result", "next_step": "录入人工拨测结果"}


def submit_probe_result(alarm_id: int, payload: ProbeResultCreate) -> dict[str, Any]:
    alarm = require_alarm(alarm_id)
    probe_status, summary = judge_probe_result(payload.raw_result)
    probe_item = (recommended_probe_items(alarm) or ["人工拨测"])[0]
    with closing(get_db()) as conn, conn:
        conn.execute(
            """
            INSERT INTO probe_results (alarm_id, raw_result, probe_item, probe_status, ai_summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (alarm_id, payload.raw_result, probe_item, probe_status, summary, now_text()),
        )
        if probe_status == "passed":
            start = datetime.now()
            end = start + timedelta(minutes=OBSERVATION_MINUTES)
            conn.execute("UPDATE alarm_events SET status = 'observing' WHERE id = ?", (alarm_id,))
            conn.execute(
                """
                INSERT INTO observation_windows (alarm_id, start_time, end_time, status, created_at)
                VALUES (?, ?, ?, 'observing', ?)
                """,
                (alarm_id, start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), now_text()),
            )
            next_step = "启动10分钟观察窗口"
        elif probe_status == "failed":
            conn.execute("UPDATE alarm_events SET status = 'waiting_probe_result' WHERE id = ?", (alarm_id,))
            next_step = "拨测失败，请继续排障并重新拨测"
        else:
            conn.execute("UPDATE alarm_events SET status = 'waiting_probe_result' WHERE id = ?", (alarm_id,))
            next_step = "拨测结果无法自动判断，请人工复核"
    return {"probe_status": probe_status, "next_step": next_step}


def submit_observation_alarm(alarm_id: int, payload: ObservationAlarmCreate) -> dict[str, Any]:
    alarm = require_alarm(alarm_id)
    parsed = parse_alarm(payload.raw_text)
    if is_same_alarm(alarm, parsed):
        reason = "观察期内再次收到同类告警，请继续排障并重新拨测"
        mark_observation_retry(alarm_id, reason)
        insert_sms(payload.raw_text, "观察期录入", "observation_manual", parsed, "observation_same_alarm", alarm_id)
        return {"same_alarm": True, "final_status": "waiting_probe_result", "reason": reason}

    imported = import_sms_text(payload.raw_text, sender="观察期录入", import_source="observation_manual")
    return {"same_alarm": False, "final_status": require_alarm(alarm_id)["status"], "reason": "非同类告警，已独立入库", "import_result": imported}


def confirm_no_alarm(alarm_id: int, payload: ConfirmNoAlarmCreate) -> dict[str, Any]:
    require_alarm(alarm_id)
    confirmed_at = now_text()
    reason = f"拨测通过，截至{confirmed_at}未收到同类告警"
    with closing(get_db()) as conn, conn:
        conn.execute("UPDATE alarm_events SET status = 'recovered' WHERE id = ?", (alarm_id,))
        conn.execute(
            """
            UPDATE observation_windows
            SET status = 'recovered',
                manual_confirm_no_alarm = 1,
                end_time = ?,
                final_conclusion = ?
            WHERE id = (
                SELECT id FROM observation_windows
                WHERE alarm_id = ?
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (confirmed_at, reason, alarm_id),
        )
    return {"final_status": "recovered", "reason": reason, "confirmed_by": payload.confirmed_by, "remark": payload.remark}


PENDING_INFO = "待人工补充"


def present(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else PENDING_INFO


def row_value(row: sqlite3.Row | dict[str, Any], key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return value if value not in (None, "") else default


def brief_text(value: Any, limit: int = 120) -> str:
    text = present(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


def report_status_label(status: str) -> str:
    return {
        "waiting_manual_recovery": "待人工确认处置",
        "waiting_probe_result": "待拨测验证",
        "observing": "观察中",
        "recovered": "已恢复",
        "failed": "未恢复",
    }.get(status, status or PENDING_INFO)


def probe_status_label(status: str | None) -> str:
    return {"passed": "成功", "failed": "失败", "unknown": "待人工复核"}.get(status or "", PENDING_INFO)


def recovery_conclusion_for_status(status: str) -> str:
    if status == "recovered":
        return "已恢复"
    if status == "failed":
        return "未恢复"
    if status == "observing":
        return "需继续观察"
    return "信息不足，待人工确认"


def recovery_reason_for_status(status: str, latest_probe: sqlite3.Row | None, window: sqlite3.Row | None) -> str:
    if status == "recovered":
        return (window or {}).get("final_conclusion") or "拨测成功，截至确认时未收到同类告警。"
    if status == "failed":
        return (window or {}).get("final_conclusion") or "拨测失败或观察窗口内出现同类复发。"
    if status == "observing":
        return "拨测已通过，但观察窗口尚未闭环。"
    if status == "waiting_probe_result":
        if window and window["same_alarm_received"]:
            return "观察期内再次收到同类告警，已退回拨测验证阶段；请继续排障并重新拨测。"
        if latest_probe and latest_probe["probe_status"] == "failed":
            return "上轮拨测失败，仍停留在拨测验证阶段；请继续排障并重新拨测。"
        return "现场处置已确认，但尚未形成拨测验证结果。"
    if status == "waiting_manual_recovery" and latest_probe and latest_probe["probe_status"] == "failed":
        return "上轮拨测失败，事件已退回人工处置；需重新处理后再次拨测。"
    if latest_probe:
        return f"已有拨测记录，当前流程状态为{report_status_label(status)}。"
    return "尚未确认现场处置动作或拨测验证结果。"


def event_info_for_report(alarm: sqlite3.Row) -> dict[str, str]:
    return {
        "告警名称": present(row_value(alarm, "alarm_content") or row_value(alarm, "alarm_type")),
        "系统名称": present(row_value(alarm, "system_name")),
        "告警发生时间": present(row_value(alarm, "alarm_time")),
        "告警来源": present(row_value(alarm, "source")),
        "故障对象": present(row_value(alarm, "object_name")),
        "当前状态": report_status_label(row_value(alarm, "status", "")),
        "原始告警摘要": brief_text(row_value(alarm, "raw_text")),
    }


def process_info_for_report(alarm: sqlite3.Row, latest_probe: sqlite3.Row | None) -> dict[str, str]:
    status = row_value(alarm, "status", "")
    retrying_failed_probe = (
        status in {"waiting_probe_result", "waiting_manual_recovery"}
        and latest_probe
        and latest_probe["probe_status"] == "failed"
    )
    manual_confirmed = "否" if status == "waiting_manual_recovery" else "是"
    action = probe_action_text_for_report(alarm, status != "waiting_manual_recovery")
    if retrying_failed_probe:
        process_result = "上轮拨测失败，继续排障并重新拨测"
        confirm_time = present(latest_probe["created_at"])
    elif status == "waiting_manual_recovery":
        process_result = "尚未确认现场处置完成"
        confirm_time = "尚未确认"
    elif latest_probe and latest_probe["probe_status"] == "failed":
        process_result = "处置后拨测失败，未恢复"
        confirm_time = present(latest_probe["created_at"])
    elif latest_probe:
        process_result = f"处置后拨测{probe_status_label(latest_probe['probe_status'])}"
        confirm_time = present(latest_probe["created_at"])
    else:
        process_result = "已进入拨测阶段，拨测结果待补充"
        confirm_time = "已确认处置完成，等待拨测记录生成"
    info = {
        "是否人工确认": manual_confirmed,
        "人工确认时间": confirm_time,
        "处理动作": action,
        "处理结果": process_result,
        "是否执行拨测": "是" if latest_probe else "否",
        "拨测项": present(latest_probe["probe_item"]) if latest_probe else PENDING_INFO,
    }
    if latest_probe and latest_probe["probe_status"] == "passed":
        info["故障结束时间"] = present(latest_probe["created_at"])
    else:
        info["拨测时间"] = present(latest_probe["created_at"]) if latest_probe else PENDING_INFO
    info["拨测结果"] = f"{probe_status_label(latest_probe['probe_status'])}：{latest_probe['ai_summary']}" if latest_probe else PENDING_INFO
    info["拨测原始记录"] = brief_text(latest_probe["raw_result"], 300) if latest_probe else PENDING_INFO
    return info


def probe_action_text_for_report(alarm: sqlite3.Row, confirmed: bool) -> str:
    alarm_context = dict(alarm)
    commands = manual_probe_commands_for_alarm(alarm_context) or recommended_probe_commands(alarm_context)
    prefix = "现场处置后按本事件推荐项执行恢复验证：" if confirmed else "待现场处置确认后按本事件推荐项执行恢复验证："
    if not commands:
        return prefix
    lines = [prefix]
    for index, item in enumerate(commands[:4], 1):
        title = str(item.get("title") or f"拨测项{index}").strip()
        command_text = str(item.get("command") or "").strip()
        lines.append(f"{index}. {title}\n命令：{command_text}" if command_text else f"{index}. {title}")
    return "\n".join(lines)


def verification_basis_for_report(
    alarm: sqlite3.Row,
    latest_probe: sqlite3.Row | None,
    window: sqlite3.Row | None,
    manuals: list[dict[str, Any]],
    observation_sms: dict[str, Any] | None = None,
) -> dict[str, str]:
    status = row_value(alarm, "status", "")
    if status == "recovered":
        alarm_gone = "是"
    elif status == "failed":
        alarm_gone = "否"
    elif status == "observing":
        alarm_gone = "观察中"
    else:
        alarm_gone = "尚未验证"

    if window and window["same_alarm_received"]:
        repeated = "是"
    elif status == "recovered" or (window and window["manual_confirm_no_alarm"]):
        repeated = "否"
    elif window:
        repeated = "观察中"
    else:
        repeated = "尚未进入观察窗口"

    basis = {
        "告警是否消失": alarm_gone,
        "拨测是否成功": probe_status_label(latest_probe["probe_status"]) if latest_probe else "尚未拨测",
        "观察窗口内是否复发": repeated,
        "是否还有同类告警": repeated,
        "是否有手册推荐": "是" if manuals else "否",
    }
    if window:
        basis["观察开始时间"] = present(window["start_time"])
    if window and row_value(window, "status") == "observing":
        basis["建议最早确认时点"] = present(window["end_time"])
    if window and row_value(window, "status") in {"recovered", "failed"}:
        basis["观察结束时间"] = present(window["end_time"])
    if observation_sms:
        basis["观察期复发告警时间"] = present(observation_sms.get("received_at") or observation_sms.get("created_at"))
        basis["观察期复发告警摘要"] = brief_text(observation_sms.get("raw_text"), 240)
    return basis


def risk_judgement_for_report(status: str, latest_probe: sqlite3.Row | None, window: sqlite3.Row | None) -> dict[str, str]:
    return {
        "是否需要继续观察": "是" if status == "observing" else ("否" if status in {"recovered", "failed"} else "是，证据未闭环"),
        "是否需要升级处理": "是" if status == "failed" else "否",
        "是否需要生成正式故障报告": "是" if status == "failed" else ("待人工确认" if status == "recovered" else "待恢复结论确认"),
        "是否需要同步记录表": "建议同步记录表" if status in {"recovered", "failed"} else "暂不同步，待恢复结论确认",
    }


def missing_info_for_report(
    alarm: sqlite3.Row,
    latest_probe: sqlite3.Row | None,
    window: sqlite3.Row | None,
    manuals: list[dict[str, Any]],
) -> list[str]:
    missing: list[str] = []
    if not row_value(alarm, "alarm_time"):
        missing.append("告警发生时间")
    if row_value(alarm, "status") == "waiting_manual_recovery":
        missing.append("现场处理动作和处理结果")
    if not latest_probe:
        missing.append("拨测结果")
    if not window:
        missing.append("观察窗口结论")
    if row_value(alarm, "status") == "failed":
        missing.append("失败原因和升级处理意见")
    if not manuals:
        missing.append("手册推荐")
    return list(dict.fromkeys(missing))


def report_kv_lines(items: dict[str, str]) -> list[str]:
    return [f"{key}：{value}" for key, value in items.items()]


def report_manual_lines(manuals: list[dict[str, Any]]) -> list[str]:
    if not manuals:
        return ["- 待人工补充"]
    return [
        f"- 手册标题：{manual['title']}；推荐原因：{manual['reason']}；匹配分数：{manual['score']}；手册链接：{manual['url']}"
        for manual in manuals
    ]


def build_report_meta(alarm_id: int) -> dict[str, Any]:
    alarm = require_alarm(alarm_id)
    probes = fetch_probe_results(alarm_id)
    window = fetch_latest_window(alarm_id)
    sms_messages = fetch_alarm_sms(alarm_id)
    observation_sms = next((item for item in reversed(sms_messages) if item.get("status") == "observation_same_alarm"), None)
    latest_probe = probes[-1] if probes else None
    acceptance_criteria = build_acceptance_criteria(alarm, latest_probe, window, len(sms_messages))
    manuals = manual_links_for_alarm(alarm)
    conclusion = recovery_conclusion_for_status(alarm["status"])
    reason = recovery_reason_for_status(alarm["status"], latest_probe, window)
    decision = decision_gate_for_status(alarm["status"])
    if alarm["status"] == "waiting_probe_result" and window and window["same_alarm_received"]:
        decision = "信息不足：观察期出现同类复发，已退回拨测验证"
    risk = "低" if alarm["status"] == "recovered" else ("高" if alarm["status"] == "failed" else "中")

    return {
        "conclusion": conclusion,
        "conclusion_reason": reason,
        "risk_level": risk,
        "decision_gate": decision,
        "acceptance_score": acceptance_score(acceptance_criteria),
        "acceptance_summary": acceptance_summary_for_status(alarm["status"]),
        "acceptance_criteria": acceptance_criteria,
        "residual_risks": residual_risks_for_status(alarm["status"], latest_probe, window),
        "event_info": event_info_for_report(alarm),
        "process_info": process_info_for_report(alarm, latest_probe),
        "verification_basis": verification_basis_for_report(alarm, latest_probe, window, manuals, observation_sms),
        "risk_judgement": risk_judgement_for_report(alarm["status"], latest_probe, window),
        "manual_recommendations": manuals,
        "missing_info": missing_info_for_report(alarm, latest_probe, window, manuals),
        "automation_basis": ["规则解析告警类型", "规则判断拨测结果", "指纹判断同类告警"],
        "evidence_items": [
            f"原始告警已入库，告警指纹：{alarm['fingerprint']}",
            f"短信收件箱关联记录数：{len(sms_messages)}",
            f"观察期复发告警：{brief_text(observation_sms['raw_text'], 160)}" if observation_sms else "观察期未记录同类复发告警",
            f"最新拨测判断：{latest_probe['probe_status']}" if latest_probe else "尚未录入拨测结果",
            f"观察窗口状态：{window['status']}" if window else "尚未启动观察窗口",
        ],
        "next_actions": next_actions_for_status(alarm["status"]),
    }


def criterion(name: str, status: str, evidence: str) -> dict[str, str]:
    return {"name": name, "status": status, "evidence": evidence}


def build_acceptance_criteria(
    alarm: sqlite3.Row,
    latest_probe: sqlite3.Row | None,
    window: sqlite3.Row | None,
    sms_count: int,
) -> list[dict[str, str]]:
    criteria = [
        criterion("原始告警已形成事件", "通过", f"告警类型：{alarm['alarm_type']}；指纹：{alarm['fingerprint']}"),
    ]

    if alarm["status"] == "waiting_manual_recovery":
        criteria.append(criterion("现场处置已进入验证", "待验证", "尚未确认现场处置完成"))
    else:
        criteria.append(criterion("现场处置已进入验证", "通过", "事件已进入拨测或后续验证阶段"))

    if latest_probe:
        probe_status = "通过" if latest_probe["probe_status"] == "passed" else "不通过"
        criteria.append(criterion("拨测结果已记录", probe_status, f"{latest_probe['probe_item']}：{latest_probe['ai_summary']}"))
    else:
        criteria.append(criterion("拨测结果已记录", "待验证", "尚未录入拨测结果"))

    if window and window["same_alarm_received"]:
        criteria.append(criterion("观察结论已闭环", "不通过", window["final_conclusion"] or "观察期收到同类告警"))
    elif alarm["status"] == "recovered" or (window and window["manual_confirm_no_alarm"]):
        criteria.append(criterion("观察结论已闭环", "通过", window["final_conclusion"] or "拨测通过，截至确认时未收到同类告警"))
    elif window:
        criteria.append(criterion("观察结论已闭环", "待验证", f"观察窗口状态：{window['status']}"))
    else:
        criteria.append(criterion("观察结论已闭环", "待验证", "尚未启动观察窗口"))

    trace_text = f"短信记录{sms_count}条；拨测记录{'已录入' if latest_probe else '尚未录入'}；观察结论{'已生成' if window else '尚未生成'}"
    if alarm["status"] == "recovered" and sms_count > 0 and latest_probe and window:
        trace_status, trace_evidence = "通过", trace_text
    elif alarm["status"] == "failed":
        trace_status, trace_evidence = "不通过", trace_text
    else:
        trace_status, trace_evidence = "待验证", trace_text
    criteria.append(criterion("恢复结论可追溯", trace_status, trace_evidence))
    return criteria


def acceptance_score(criteria: list[dict[str, str]]) -> str:
    passed = sum(1 for item in criteria if item["status"] == "通过")
    return f"{passed}/{len(criteria)}"


def decision_gate_for_status(status: str) -> str:
    if status == "recovered":
        return "已恢复：可归档，按流程确认是否同步记录表"
    if status == "failed":
        return "未恢复：继续排障并升级处理"
    if status == "observing":
        return "需继续观察：观察窗口未闭环，暂不关闭"
    if status == "waiting_probe_result":
        return "信息不足：已确认处置，待拨测验证"
    return "信息不足：待人工确认处置动作"


def acceptance_summary_for_status(status: str) -> str:
    if status == "recovered":
        return "恢复验证已闭环：已保留原始告警、现场验证、拨测结果和观察结论。"
    if status == "failed":
        return "现场恢复未通过：拨测失败或观察期出现同类复发，需要继续排障并重新验证。"
    if status == "observing":
        return "当前为阶段性恢复：拨测已通过，但观察窗口尚未闭环，仍需值班继续盯防。"
    if status == "waiting_probe_result":
        return "当前已确认现场处置动作，但尚未形成拨测证据，不能认定恢复。"
    return "当前证据不足以证明现场恢复，需要补齐处置动作、系统拨测和观察记录。"


def residual_risks_for_status(
    status: str,
    latest_probe: sqlite3.Row | None,
    window: sqlite3.Row | None,
) -> list[str]:
    if status == "recovered":
        return [
            "当前现场证据满足恢复归档条件，但仍建议次日复盘同类告警趋势。",
            "如业务侧仍有投诉、性能异常或人工巡检异常，应重新进入恢复验证。",
        ]
    if status == "failed":
        return [
            "恢复动作未被验证通过，可能存在根因未消除或处置不彻底。",
            "需补充影响范围、责任系统、失败原因和下一轮恢复计划。",
        ]
    risks = []
    if not latest_probe:
        risks.append("尚未形成拨测证据，无法证明业务或组件已恢复。")
    if not window:
        risks.append("尚未启动10分钟观察窗口，无法证明告警未复发。")
    elif status == "observing":
        risks.append("观察窗口未完成，暂不能认定现场恢复闭环。")
    risks.append("若短信导入不及时，观察期同类复发可能被延迟识别。")
    return risks


def next_actions_for_status(status: str) -> list[str]:
    if status == "recovered":
        return ["由负责人确认恢复结论", "按影响范围判断是否生成正式故障报告", "按流程同步记录表或值班记录"]
    if status == "failed":
        return ["继续排障并升级处理", "补充失败原因、影响范围和现场处置动作", "重新制定恢复动作并二次拨测"]
    if status == "observing":
        return ["继续观察窗口", "持续导入观察期短信或同类告警", "观察期结束后确认是否无复发"]
    if status == "waiting_probe_result":
        return ["执行系统拨测并录入原始结果", "确认拨测对象、命令、返回结果和通过标准", "拨测通过后进入观察窗口"]
    return ["确认现场处理动作是否完成", "补充处理动作、处理结果和人工确认时间", "完成处置后再执行拨测"]


def build_report(alarm_id: int) -> str:
    alarm = require_alarm(alarm_id)
    meta = build_report_meta(alarm_id)

    lines = [
        f"系统现场恢复验证报告 ALARM-{alarm['id']}",
        "",
        "一、恢复结论",
        f"恢复结论：{meta['conclusion']}",
        f"判断依据：{meta['conclusion_reason']}",
        f"处置建议：{meta['decision_gate']}",
        f"风险等级：{meta['risk_level']}",
        "",
        "二、事件基础信息",
        *report_kv_lines(meta["event_info"]),
        "",
        "三、现场处理过程",
        *report_kv_lines(meta["process_info"]),
        "",
        "四、恢复验证依据",
        *report_kv_lines(meta["verification_basis"]),
        "",
        "五、风险判断",
        *report_kv_lines(meta["risk_judgement"]),
        "",
        "六、手册推荐",
        *report_manual_lines(meta["manual_recommendations"]),
        "",
        "七、需补齐事项",
        *[f"- {item}" for item in meta["missing_info"]],
        "",
        "八、下一步建议",
        *[f"- {item}" for item in meta["next_actions"]],
        "",
        "九、恢复验证闭环依据",
        f"闭环进度：{meta['acceptance_score']}",
        *[
            f"- [{item['status']}] {item['name']}；证据：{item['evidence']}"
            for item in meta["acceptance_criteria"]
        ],
        "",
        "十、原始短信",
        alarm["raw_text"],
    ]
    return "\n".join(lines)


def split_mail_addresses(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[;,；，\s]+", value or "") if part.strip()]


def mail_env(name: str, default: str = "") -> str:
    return os.environ.get(f"WORKBENCH_REPORT_MAIL_{name}", default).strip()


def report_email_subject(alarm: sqlite3.Row, meta: dict[str, Any], subject: str = "") -> str:
    if subject.strip():
        return subject.strip()
    system = row_value(alarm, "system_name") or "未知系统"
    alarm_type = row_value(alarm, "alarm_type") or "现场故障"
    return f"【{meta['conclusion']}】{system}{alarm_type}现场恢复验证报告 ALARM-{alarm['id']}"


def build_report_email(alarm_id: int, payload: ReportEmailCreate) -> dict[str, Any]:
    alarm = require_alarm(alarm_id)
    meta = build_report_meta(alarm_id)
    to = split_mail_addresses(payload.to) or split_mail_addresses(mail_env("TO"))
    cc = split_mail_addresses(payload.cc) or split_mail_addresses(mail_env("CC"))
    sender = mail_env("FROM") or mail_env("USERNAME")
    return {
        "alarm_id": alarm_id,
        "subject": report_email_subject(alarm, meta, payload.subject),
        "from": sender,
        "to": to,
        "cc": cc,
        "body": build_report(alarm_id),
    }


def send_report_email(alarm_id: int, payload: ReportEmailCreate) -> dict[str, Any]:
    email_data = build_report_email(alarm_id, payload)
    smtp_host = mail_env("SMTP_HOST")
    security = mail_env("SMTP_SECURITY", "ssl").lower()
    smtp_port = int(mail_env("SMTP_PORT", "465" if security == "ssl" else "587" if security == "starttls" else "25"))
    username = mail_env("USERNAME")
    password = mail_env("PASSWORD")

    missing = []
    if not email_data["to"]:
        missing.append("WORKBENCH_REPORT_MAIL_TO 或请求 to")
    if not email_data["from"] and not payload.dry_run:
        missing.append("WORKBENCH_REPORT_MAIL_FROM 或 WORKBENCH_REPORT_MAIL_USERNAME")
    if not smtp_host and not payload.dry_run:
        missing.append("WORKBENCH_REPORT_MAIL_SMTP_HOST")
    if username and not password and not payload.dry_run:
        missing.append("WORKBENCH_REPORT_MAIL_PASSWORD")
    if missing:
        raise HTTPException(status_code=400, detail=f"邮件发送未配置：{'; '.join(missing)}")

    if payload.dry_run:
        return {"sent": False, "dry_run": True, **email_data}

    msg = EmailMessage()
    msg["Subject"] = email_data["subject"]
    msg["From"] = email_data["from"]
    msg["To"] = ", ".join(email_data["to"])
    if email_data["cc"]:
        msg["Cc"] = ", ".join(email_data["cc"])
    msg.set_content(email_data["body"])
    recipients = email_data["to"] + email_data["cc"]

    smtp_cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    try:
        with smtp_cls(smtp_host, smtp_port, timeout=20) as smtp:
            if security == "starttls":
                smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(msg, to_addrs=recipients)
    except smtplib.SMTPAuthenticationError as exc:
        detail = exc.smtp_error.decode("utf-8", "ignore") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        raise HTTPException(status_code=502, detail=f"邮件SMTP认证失败：{detail}。请检查163邮箱SMTP授权码是否正确、SMTP服务是否已开启。") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise HTTPException(status_code=502, detail=f"邮件收件人被拒绝：{', '.join(exc.recipients.keys())}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"邮件发送失败：{type(exc).__name__}: {exc}") from exc
    return {"sent": True, "dry_run": False, "subject": email_data["subject"], "to": email_data["to"], "cc": email_data["cc"]}


def report_page(alarm_id: str) -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "report.html")


MANUAL_INDEX_PATH = SHARED_MANUAL_INDEX_PATH
MANUAL_ROOT = MANUAL_INDEX_PATH.parent.parent
LEGACY_MANUAL_ROOT = Path(r"F:\workbuddy\多域AI故障\高频故障\workbuddy")
MANUAL_INDEX_FALLBACK_PATHS = [LEGACY_MANUAL_ROOT / "data" / "manual_index.json"]
MANUAL_HTML_ROOTS = [
    MANUAL_ROOT / "导出手册" / "html",
    MANUAL_ROOT / "导出手册_v2" / "html",
    LEGACY_MANUAL_ROOT / "导出手册" / "html",
    LEGACY_MANUAL_ROOT / "导出手册_v2" / "html",
]
MANUAL_ASSET_ROOTS = [MANUAL_ROOT / "assets", LEGACY_MANUAL_ROOT / "assets"]
OPENAPI_NETWORK_MANUAL = Path("经分域") / "OPENAPI" / "OPENAPI-数据库服务器网络服务异常.html"
OPENAPI_DEADLOCK_MANUAL = Path("经分域") / "OPENAPI" / "OPENAPI-主机软死锁.html"
MANUAL_OPENAPI_NETWORK_CANDIDATES = [root / OPENAPI_NETWORK_MANUAL for root in MANUAL_HTML_ROOTS]
MANUAL_OPENAPI_DEADLOCK_CANDIDATES = [root / OPENAPI_DEADLOCK_MANUAL for root in MANUAL_HTML_ROOTS]


def first_existing_manual(paths: list[Path], detail: str) -> FileResponse:
    for path in paths:
        if path.exists():
            return FileResponse(path)
    raise HTTPException(status_code=404, detail=detail)


def manual_asset_candidates(asset_path: str) -> list[Path]:
    relative = Path(asset_path)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise HTTPException(status_code=404, detail="手册资源不存在")
    return [root / relative for root in MANUAL_ASSET_ROOTS]


def load_manual_entries() -> list[dict[str, Any]]:
    import json

    index_path = first_existing_manual_index()
    if not index_path:
        return []

    data = json.loads(index_path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = []
    for system in data.get("systems", []):
        aliases = system.get("aliases", [])
        for guide in system.get("guides", []):
            candidates = manual_file_candidates(guide)
            source_path = guide.get("path") or guide.get("markdown_path") or guide.get("html_path") or ""
            entries.append(
                {
                    "id": manual_id(source_path),
                    "title": f"{guide['system']}-{guide['fault_pattern']}",
                    "system": guide["system"],
                    "aliases": aliases,
                    "fault_pattern": guide["fault_pattern"],
                    "classification": guide.get("classification", ""),
                    "paths": candidates,
                }
            )
    return entries


def first_existing_manual_index() -> Path | None:
    for path in [MANUAL_INDEX_PATH, *MANUAL_INDEX_FALLBACK_PATHS]:
        if path.exists():
            return path
    return None


def manual_file_candidates(guide: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    if guide.get("html_path"):
        paths.extend(manual_absolute_candidates(guide["html_path"]))
    paths.extend(manual_html_candidates(guide.get("markdown_path") or guide.get("path") or ""))
    if guide.get("markdown_path"):
        paths.extend(manual_absolute_candidates(guide["markdown_path"]))
    elif guide.get("path"):
        paths.extend(manual_absolute_candidates(guide["path"]))
    return list(dict.fromkeys(paths))


def manual_absolute_candidates(relative_path: str) -> list[Path]:
    path = Path(relative_path)
    if path.is_absolute():
        return [path]
    manual_roots = list(dict.fromkeys(root.parent.parent for root in MANUAL_HTML_ROOTS))
    return list(dict.fromkeys([root / path for root in manual_roots] + [root / path for root in MANUAL_HTML_ROOTS]))


def manual_html_candidates(markdown_path: str) -> list[Path]:
    parts = Path(markdown_path).parts
    relative = Path(*parts[1:]).with_suffix(".html") if parts and parts[0] == "处理手册" else Path(markdown_path).with_suffix(".html")
    return [root / relative for root in MANUAL_HTML_ROOTS]


def manual_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def manual_links_for_alarm(alarm: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
    manuals = search_manuals(manual_search_context(alarm), limit=limit)
    return [
        {
            "title": manual["title"],
            "url": f"/manuals/{manual['manual_id']}",
            "score": manual["score"],
            "classification": manual["category"],
            "reason": display_manual_reason(manual["reason"]),
            "system": manual["system_name"],
        }
        for manual in manuals
    ] or fallback_manual_links(alarm)


def display_manual_reason(reason: str) -> str:
    parts: list[str] = []
    for item in [part.strip() for part in str(reason or "").split(",") if part.strip()]:
        if item.startswith("system:"):
            parts.append(f"命中系统：{item.removeprefix('system:')}")
        elif item.startswith("pattern:"):
            parts.append(f"命中故障模式：{item.removeprefix('pattern:')}")
        elif item.startswith("category:"):
            parts.append(f"命中分类：{item.removeprefix('category:')}")
        elif item.startswith("keywords:"):
            keywords = item.removeprefix("keywords:").replace("/", "、")
            parts.append(f"命中关键字：{keywords}")
        elif item == "details":
            parts.append("命中手册详情")
        elif item.startswith("score:"):
            parts.append(f"综合评分：{item.removeprefix('score:')}")
        else:
            parts.append(item)
    return "；".join(parts)


def fallback_manual_links(alarm: dict[str, Any]) -> list[dict[str, Any]]:
    text = " ".join(str(alarm.get(key) or "") for key in ("system_name", "alarm_type", "object_name", "metric_name", "current_value", "alarm_content", "raw_text"))
    system = str(alarm.get("system_name") or "").strip()
    options: list[dict[str, Any]] = []
    if re.search(r"(openapi|api)", f"{system} {text}", re.I):
        options.append({"title": "OPENAPI-数据库服务器网络服务异常", "url": "/manual/openapi/network", "score": 0.86, "reason": "命中 OPENAPI / 接口 / 数据库网络关键词", "system": "OPENAPI"})
        options.append({"title": "OPENAPI-主机软死锁", "url": "/manual/openapi/deadlock", "score": 0.82, "reason": "命中 OPENAPI 相关故障兜底", "system": "OPENAPI"})
    if re.search(r"(bomc|告警|短信|cas|页面打不开|无法打开)", f"{system} {text}", re.I):
        options.append({"title": "BOMC-cas服务异常", "url": "/manual/openapi/deadlock", "score": 0.9, "reason": "命中 BOMC / CAS / 页面打不开关键词", "system": "BOMC"})
        options.append({"title": "BOMC-线程池满", "url": "/manual/openapi/deadlock", "score": 0.86, "reason": "命中 BOMC 线程池相关关键词", "system": "BOMC"})
        options.append({"title": "BOMC-30443端口冲突与公共模块无法连接", "url": "/manual/openapi/deadlock", "score": 0.84, "reason": "命中 BOMC 端口冲突关键词", "system": "BOMC"})
    if re.search(r"(smartbi|报表|bi)", f"{system} {text}", re.I):
        options.append({"title": "SmartBI-页面与报表异常", "url": "/manual/openapi/deadlock", "score": 0.8, "reason": "命中 BI / 报表关键词", "system": "SmartBI"})
    if re.search(r"(下载中心|download)", f"{system} {text}", re.I):
        options.append({"title": "下载中心-访问异常", "url": "/manual/openapi/deadlock", "score": 0.78, "reason": "命中下载中心关键词", "system": "下载中心"})
    if re.search(r"(实时营销|营销)", f"{system} {text}", re.I):
        options.append({"title": "实时营销-链路异常", "url": "/manual/openapi/deadlock", "score": 0.78, "reason": "命中营销链路关键词", "system": "实时营销"})
    if re.search(r"(数字员工|机器人|rpa)", f"{system} {text}", re.I):
        options.append({"title": "数字员工 / RPA-任务失败", "url": "/manual/openapi/deadlock", "score": 0.76, "reason": "命中数字员工关键词", "system": "数字员工"})
    if re.search(r"(it工单|工单)", f"{system} {text}", re.I):
        options.append({"title": "IT工单系统-访问异常", "url": "/manual/openapi/deadlock", "score": 0.75, "reason": "命中工单系统关键词", "system": "IT工单系统"})
    if not options and text:
        options.append({"title": "按故障描述检索手册", "url": "/manual/openapi/deadlock", "score": 0.66, "reason": "当前未命中专用规则，先用通用手册兜底", "system": system or "通用"})
    return options[:3]


def manual_entry_by_id(manual_id_value: str) -> dict[str, Any] | None:
    for entry in load_manual_entries():
        if entry["id"] == manual_id_value:
            return entry
    return None


def manual_probe_commands_for_alarm(alarm: dict[str, Any], limit: int = 2) -> list[dict[str, str]]:
    mandatory = mandatory_probe_commands_for_alarm(alarm)
    needs = [need for need in probe_needs(alarm) if not (mandatory and need == "signal")]
    if mandatory and not needs:
        return mandatory
    for manual in search_manuals(manual_search_context(alarm), limit=limit):
        entry = manual_entry_by_id(manual["manual_id"])
        if not entry:
            continue
        commands = manual_probe_commands_from_entry(entry)
        if not commands:
            continue
        if not needs:
            return unique_probe_commands(mandatory + commands[:1])
        selected = select_probe_commands_for_needs(needs, commands)
        if selected:
            return unique_probe_commands(mandatory + selected)
    return mandatory


def mandatory_probe_commands_for_alarm(alarm: dict[str, Any]) -> list[dict[str, str]]:
    if detect_system(alarm) == "OPENAPI":
        return [openapi_signal_probe_command()]
    return []


def manual_probe_commands_from_entry(entry: dict[str, Any]) -> list[dict[str, str]]:
    paths = sorted(entry.get("paths", []), key=lambda path: Path(path).suffix.lower() != ".md")
    for path in paths:
        if not path.exists() or path.suffix.lower() != ".md":
            continue
        commands = extract_markdown_probe_commands(path.read_text(encoding="utf-8", errors="ignore"))
        if commands:
            return commands
    return []


def extract_markdown_probe_commands(text: str) -> list[dict[str, str]]:
    section_match = re.search(r"(?ms)^\s*##\s*关键检查命令\s*$([\s\S]*?)(?=^\s*##\s+|\Z)", text)
    if not section_match:
        return []
    commands: list[dict[str, str]] = []
    for part in re.finditer(r"(?ms)^\s*###\s*(.+?)\s*$([\s\S]*?)(?=^\s*###\s+|\Z)", section_match.group(1)):
        title = part.group(1).strip()
        body = part.group(2)
        for code in re.finditer(r"(?ms)```[^\n]*\n(.*?)```", body):
            commands.append(command_from_manual(title, code.group(1).strip(), body[code.end() :]))
    return commands


def command_from_manual(title: str, command_value: str, after_code: str) -> dict[str, str]:
    return {
        "title": simple_probe_title(title, command_value),
        "command": command_value,
        "pass_hint": first_manual_pass_hint(after_code),
    }


def select_probe_commands_for_alarm(alarm: dict[str, Any], commands: list[dict[str, str]]) -> list[dict[str, str]]:
    return select_probe_commands_for_needs(probe_needs(alarm), commands)


def select_probe_commands_for_needs(needs: list[str], commands: list[dict[str, str]]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for need in needs:
        for item in commands:
            if item not in selected and command_matches_need(item, need):
                selected.append(item)
                break
    return selected


def unique_probe_commands(commands: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in commands:
        key = (item.get("title", ""), item.get("command", ""))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def probe_needs(alarm: dict[str, Any]) -> list[str]:
    text = " ".join(str(alarm.get(key, "") or "") for key in ("object_name", "raw_text", "alarm_content", "alarm_type", "metric_name"))
    upper = text.upper()
    needs: list[str] = []
    if any(word in text for word in ["信令接口", "接口拨测", "实时接口"]):
        needs.append("signal")
    if openapi_database_network_alarm(alarm):
        needs.append("database")
    if any(word in upper for word in ["DB2", "ORACLE", "MYSQL", "POSTGRES", "SQLSERVER"]) or any(word in text for word in ["数据库", "50000"]):
        needs.append("database")
    if any(word in text for word in ["主机", "服务器", "内存", "磁盘", "软死锁", "负载"]) or any(word in upper for word in ["HOST", "CPU", "MEMORY", "DISK"]):
        needs.append("host")
    if any(word in upper for word in ["TOMCAT", "NGINX", "WEBLOGIC", "WEBSPHERE", "REDIS", "KAFKA", "ZOOKEEPER", "MQ"]) or any(word in text for word in ["中间件", "线程池", "缓存", "消息队列"]):
        needs.append("middleware")
    return list(dict.fromkeys(needs))


def command_matches_need(item: dict[str, str], need: str) -> bool:
    text = " ".join(str(item.get(key, "") or "") for key in ("title", "command", "pass_hint"))
    upper = text.upper()
    if need == "signal":
        return any(word in text for word in ["信令接口", "接口拨测", "实时接口"]) or any(word in upper for word in ["REALUSRAREAINFO", "ESBURL"])
    if need == "database":
        return any(word in upper for word in ["DB2", "ORACLE", "MYSQL", "POSTGRES", "SQLSERVER"]) or any(word in text for word in ["数据库", "50000"])
    if need == "host":
        return any(word in text for word in ["主机", "服务器", "内存", "磁盘", "软死锁", "负载"]) or any(word in upper for word in ["HOST", "CPU", "MEMORY", "DISK", "DMESG"])
    if need == "middleware":
        return any(word in upper for word in ["TOMCAT", "NGINX", "WEBLOGIC", "WEBSPHERE", "REDIS", "KAFKA", "ZOOKEEPER", "MQ"]) or any(word in text for word in ["中间件", "线程池", "缓存", "消息队列"])
    return False


def simple_probe_title(title: str, command_value: str) -> str:
    raw_title = re.sub(r"^\s*\d+[.、]\s*", "", title).strip()
    text = f"{raw_title} {command_value}"
    upper = text.upper()
    if any(word in text for word in ["信令接口", "接口拨测", "实时接口"]) or any(word in upper for word in ["REALUSRAREAINFO", "ESBURL"]):
        return "拨测信令接口"
    if any(word in upper for word in ["DB2", "ORACLE", "MYSQL", "POSTGRES", "SQLSERVER"]) or any(word in text for word in ["数据库", "50000"]):
        return "拨测数据库"
    if any(word in text for word in ["主机", "服务器", "内存", "磁盘", "软死锁", "负载"]) or any(word in upper for word in ["HOST", "CPU", "MEMORY", "DISK", "DMESG"]):
        return "拨测主机"
    if any(word in upper for word in ["TOMCAT", "NGINX", "WEBLOGIC", "WEBSPHERE", "REDIS", "KAFKA", "ZOOKEEPER", "MQ"]) or any(word in text for word in ["中间件", "线程池", "缓存", "消息队列"]):
        return "拨测中间件"
    return raw_title or "执行拨测命令"


def first_manual_pass_hint(text: str) -> str:
    for line in text.splitlines():
        stripped = re.sub(r"[`*_]", "", line.strip())
        if stripped.startswith("- "):
            return stripped[2:].strip()
        if stripped.startswith("###"):
            break
    return "按手册判断通过/异常"


def manual_search_context(alarm: dict[str, Any]) -> dict[str, Any]:
    system = detect_system(alarm)
    is_openapi_db_network = openapi_database_network_alarm(alarm)
    alarm_type = "数据库网络异常" if is_openapi_db_network else alarm.get("alarm_type")
    fault_pattern = "数据库服务器网络服务异常" if is_openapi_db_network else alarm.get("alarm_content")
    symptom_prefix = "数据库 网络异常 50000 DB2" if is_openapi_db_network else ""
    return {
        "source": "recovery_workbench",
        "event_id": alarm.get("id"),
        "system_name": system or alarm.get("system_name"),
        "alarm_type": alarm_type,
        "fault_category": alarm_type,
        "fault_pattern": fault_pattern,
        "object_name": alarm.get("object_name"),
        "metric_name": alarm.get("metric_name"),
        "symptom_text": " ".join(
            [symptom_prefix, *[str(alarm.get(key) or "") for key in ("alarm_content", "current_value")]]
        ),
        "raw_text": alarm.get("raw_text"),
        "occurred_at": alarm.get("alarm_time"),
        "alarm_content": alarm.get("alarm_content"),
    }


def manual_match_score(alarm: dict[str, Any], entry: dict[str, Any]) -> int:
    text = match_text(alarm)
    score = 0
    if same_manual_system(alarm, entry):
        score += 60
    pattern = entry["fault_pattern"]
    if contains_text(text, pattern):
        score += 120
    for keyword in manual_keywords(pattern):
        if contains_text(text, keyword):
            score += 35
    classification = entry["classification"]
    if classification and classification in alarm_categories(text):
        score += 45
    return score


def manual_match_reason(alarm: dict[str, Any], entry: dict[str, Any], score: int, system: str | None = None) -> str:
    text = match_text(alarm)
    reasons: list[str] = []
    if system:
        reasons.append(f"先识别系统：{system}")
    if same_manual_system(alarm, entry):
        reasons.append(f"命中系统手册：{entry['system']}")
    if contains_text(text, entry["fault_pattern"]):
        reasons.append(f"命中故障模式：{entry['fault_pattern']}")
    matched_keywords = [kw for kw in manual_keywords(entry["fault_pattern"]) if contains_text(text, kw)]
    if matched_keywords:
        reasons.append("命中关键字：" + ", ".join(matched_keywords[:4]))
    if entry.get("classification") and entry["classification"] in alarm_categories(text):
        reasons.append(f"命中分类：{entry['classification']}")
    if not reasons:
        reasons.append(f"综合评分：{score}")
    return "；".join(reasons)


def manual_fallback_by_system(system: str, entries: list[dict[str, Any]], limit: int = 4) -> list[tuple[int, dict[str, Any]]]:
    preferred_map = {
        "BOMC": ["cas服务异常", "线程池满", "30443端口冲突与公共模块无法连接"],
        "OPENAPI": ["数据库服务器网络服务异常", "主机软死锁"],
        "实时营销": ["Tomcat服务异常", "端口服务异常"],
        "数字员工": ["接口服务异常", "redis内存溢出"],
        "IT工单系统": ["itsm服务异常", "数据库服务器网络服务异常"],
        "自助分析": ["后台任务进程卡住"],
        "下载中心": ["用户同步程序卡死", "文件扫描程序卡死"],
        "SmartBI": ["线程池满"],
        "大数据平台K8S集群": ["主机内存故障"],
    }
    preferred = preferred_map.get(system, [])
    merged: list[tuple[int, dict[str, Any]]] = []
    for idx, pattern in enumerate(preferred):
        for entry in entries:
            if same_manual_system_name(system, entry) and contains_text(entry["fault_pattern"], pattern):
                merged.append((200 - idx * 10, entry))
                break
    return merged[:limit]


def manual_priority_for_system(alarm: dict[str, Any], entries: list[dict[str, Any]], system: str | None) -> list[tuple[int, dict[str, Any]]]:
    if not system:
        return []
    text = match_text(alarm)
    preferred_map = {
        "BOMC": ["cas服务异常", "线程池满", "30443端口冲突与公共模块无法连接"],
        "OPENAPI": ["数据库服务器网络服务异常", "主机软死锁"],
        "实时营销": ["Tomcat服务异常", "端口服务异常"],
        "数字员工": ["接口服务异常", "redis内存溢出"],
        "IT工单系统": ["itsm服务异常", "数据库服务器网络服务异常"],
        "自助分析": ["后台任务进程卡住"],
        "下载中心": ["用户同步程序卡死", "文件扫描程序卡死"],
        "SmartBI": ["线程池满"],
        "大数据平台K8S集群": ["主机内存故障"],
    }
    preferred = preferred_map.get(system, [])
    if system == "BOMC" and any(word in text for word in ["无法打开", "打不开", "页面打不开", "系统不可用", "登录失败", "异常"]):
        preferred = ["cas服务异常", "线程池满", "30443端口冲突与公共模块无法连接", *preferred]
    priority: list[tuple[int, dict[str, Any]]] = []
    for idx, pattern in enumerate(preferred):
        for entry in entries:
            if same_manual_system_name(system, entry) and contains_text(entry["fault_pattern"], pattern):
                priority.append((200 - idx * 10, entry))
                break
    return priority


def same_manual_system(alarm: dict[str, Any], entry: dict[str, Any]) -> bool:
    text = match_text(alarm)
    names = [entry["system"], *entry.get("aliases", [])]
    if any((name or "").upper() == "BOMC" for name in names):
        return is_bomc_system_text(text)
    return any(name and contains_text(text, name) for name in names)


def manual_keywords(pattern: str) -> list[str]:
    words = [part for part in re.split(r"[-_、，,与和\s]+", pattern) if len(part) >= 2]
    if "数据库服务器网络服务异常" in pattern:
        words += ["数据库", "网络服务异常", "网络异常", "端口", "连接", "50000", "HTTP", "接口", "成功率", "联通性", "不通"]
    if "主机软死锁" in pattern:
        words += ["主机", "软死锁", "死锁"]
    if "主机内存故障" in pattern:
        words += ["主机", "内存"]
    if "Tomcat服务异常" in pattern:
        words += ["Tomcat", "中间件"]
    if "redis内存溢出" in pattern:
        words += ["redis", "内存溢出", "中间件"]
    if "线程池满" in pattern:
        words += ["线程池", "线程", "无法打开", "页面打不开", "系统卡顿"]
    if "端口冲突" in pattern or "端口服务异常" in pattern:
        words += ["端口", "连接", "网络", "服务"]
    if "cas服务异常" in pattern:
        words += ["cas", "页面无法打开", "无法打开", "系统不可用", "BOMC页面", "登录失败"]
    return list(dict.fromkeys(words))


def alarm_categories(text: str) -> set[str]:
    categories: set[str] = set()
    rules = {
        "数据库": ["数据库", "DB2", "REDIS", "SQL", "50000", "网络异常", "网络服务异常"],
        "主机": ["主机", "内存", "CPU", "磁盘", "软死锁", "进程"],
        "网络": ["网络", "端口", "连接", "不通", "超时", "30443", "FALSE"],
        "应用": ["接口", "服务", "页面", "任务", "同步", "扫描", "CAS", "ITSM", "线程池"],
        "其他": ["TOMCAT", "REDIS", "中间件"],
    }
    upper = text.upper()
    for category, keywords in rules.items():
        if any(keyword.upper() in upper for keyword in keywords):
            categories.add(category)
    return categories


def detect_system(alarm: dict[str, Any]) -> str | None:
    parsed_system = str(alarm.get("system_name") or "")
    system_rules = [
        ("OPENAPI", ["OPENAPI", "OPEN API", "OPEN-API"]),
        ("实时营销", ["实时营销"]),
        ("数字员工", ["数字员工", "智能小7", "SNC-DIGITAL-EMPLOYEE", "SNC-IM-CHAT"]),
        ("IT工单系统", ["IT工单系统", "ITSM", "IT 工单"]),
        ("自助分析", ["自助分析"]),
        ("下载中心", ["下载中心"]),
        ("SmartBI", ["SMARTBI"]),
        ("集中运营 IOP", ["集中运营 IOP", "IOP"]),
        ("省级业务集中稽核系统", ["省级业务集中稽核系统", "稽核系统"]),
        ("大数据平台K8S集群", ["大数据平台K8S集群", "K8S", "KUBERNETES"]),
        ("自建空充", ["自建空充"]),
        ("CLC业务卡", ["CLC业务卡"]),
        ("标签库", ["标签库"]),
        ("乡村振兴", ["乡村振兴"]),
        ("PASS系统", ["PASS系统"]),
        ("smp", ["SMP"]),
        ("大数据PaaS平台", ["大数据PaaS平台", "PAAS"]),
    ]

    for text in [parsed_system, match_text(alarm)]:
        upper = text.upper()
        for system, keywords in system_rules:
            if any(keyword.upper() in upper for keyword in keywords):
                return system
        if is_bomc_system_text(text):
            return "BOMC"
    return None


def is_bomc_system_text(text: str) -> bool:
    return bool(
        re.search(
            r"(?:系统名称|系统)[:：\[]?\s*BOMC|BOMC\s*(?:系统(?![:：])|页面|无法打开|打不开|无法访问|登录|服务|CAS|线程池|30443)",
            text,
            re.I,
        )
    )


def same_manual_system_name(system: str, entry: dict[str, Any]) -> bool:
    names = [entry["system"], *entry.get("aliases", [])]
    return any((name or "").upper() == system.upper() for name in names)


def system_aliases_for_display(system: str | None) -> list[str]:
    if not system:
        return []
    alias_map = {
        "OPENAPI": ["数据库服务器网络服务异常", "主机软死锁"],
        "BOMC": ["cas服务异常", "线程池满", "30443端口冲突与公共模块无法连接"],
        "实时营销": ["Tomcat服务异常", "端口服务异常"],
        "数字员工": ["接口服务异常", "redis内存溢出"],
        "IT工单系统": ["itsm服务异常", "数据库服务器网络服务异常"],
        "自助分析": ["后台任务进程卡住"],
        "下载中心": ["用户同步程序卡死", "文件扫描程序卡死"],
        "SmartBI": ["线程池满"],
        "大数据平台K8S集群": ["主机内存故障"],
    }
    return alias_map.get(system, [])


def match_text(alarm: dict[str, Any]) -> str:
    return " ".join(
        str(alarm.get(key) or "")
        for key in ("system_name", "alarm_type", "object_name", "metric_name", "current_value", "alarm_content", "raw_text")
    )


def contains_text(text: str, value: str) -> bool:
    return value.upper() in text.upper()


@app.get("/manual/openapi/network")
def manual_openapi_network() -> FileResponse:
    return first_existing_manual(MANUAL_OPENAPI_NETWORK_CANDIDATES, "OPENAPI网络异常手册不存在")


@app.get("/manual/openapi/deadlock")
def manual_openapi_deadlock() -> FileResponse:
    return first_existing_manual(MANUAL_OPENAPI_DEADLOCK_CANDIDATES, "OPENAPI主机软死锁手册不存在")


@app.get("/manuals/{manual_id_value}")
def manual_by_id(manual_id_value: str) -> FileResponse:
    for entry in load_manual_entries():
        if entry["id"] == manual_id_value:
            return first_existing_manual(entry["paths"], "手册文件不存在")
    raise HTTPException(status_code=404, detail="手册不存在")


@app.get("/assets/{asset_path:path}")
@app.get("/manuals/assets/{asset_path:path}")
def manual_asset(asset_path: str) -> FileResponse:
    return first_existing_manual(manual_asset_candidates(asset_path), "手册资源不存在")


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/")
def index_page() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.post("/api/sms/import")
def api_import_sms(payload: SmsImportCreate) -> dict[str, Any]:
    return import_sms_text(payload.raw_text, payload.sender, payload.import_source, payload.force_new_event)


@app.get("/api/sms/inbox")
def api_sms_inbox() -> dict[str, Any]:
    return list_sms_inbox()


@app.post("/api/alarms")
def api_create_alarm(payload: AlarmCreate) -> dict[str, Any]:
    alarm = create_alarm_from_raw(payload.raw_text)
    return {
        "alarm_id": alarm["id"],
        "alarm_type": alarm["alarm_type"],
        "system_name": alarm["system_name"],
        "recommended_probe_items": recommended_probe_items(alarm),
        "status": alarm["status"],
    }


@app.get("/api/alarms")
def api_list_alarms() -> dict[str, Any]:
    return list_alarms()


@app.get("/api/probe-commands")
def api_probe_commands(system: str = "", text: str = "") -> dict[str, Any]:
    return {"items": probe_commands_for_context(system, text)}


@app.get("/api/alarms/{alarm_id}")
def api_get_alarm(alarm_id: int) -> dict[str, Any]:
    return get_alarm(alarm_id)


@app.post("/api/alarms/{alarm_id}/manual-recovery")
def api_manual_recovery(alarm_id: int) -> dict[str, Any]:
    return mark_manual_recovery_done(alarm_id)


@app.post("/api/alarms/{alarm_id}/probe-result")
def api_probe_result(alarm_id: int, payload: ProbeResultCreate) -> dict[str, Any]:
    return submit_probe_result(alarm_id, payload)


@app.post("/api/alarms/{alarm_id}/observation/new-alarm")
def api_observation_new_alarm(alarm_id: int, payload: ObservationAlarmCreate) -> dict[str, Any]:
    return submit_observation_alarm(alarm_id, payload)


@app.post("/api/alarms/{alarm_id}/observation/confirm-no-alarm")
def api_confirm_no_alarm(alarm_id: int, payload: ConfirmNoAlarmCreate) -> dict[str, Any]:
    return confirm_no_alarm(alarm_id, payload)


@app.get("/api/alarms/{alarm_id}/report")
def api_alarm_report(alarm_id: int) -> PlainTextResponse:
    return PlainTextResponse(build_report(alarm_id))


@app.post("/api/alarms/{alarm_id}/report/email")
def api_alarm_report_email(alarm_id: int, payload: ReportEmailCreate) -> dict[str, Any]:
    return send_report_email(alarm_id, payload)


@app.get("/reports/{alarm_id}")
def api_report_page(alarm_id: str) -> FileResponse:
    return report_page(alarm_id)
