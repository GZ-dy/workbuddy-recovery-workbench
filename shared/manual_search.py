from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
MANUAL_INDEX_CANDIDATES = [
    ROOT_DIR / "故障手册知识库" / "workbuddy" / "data" / "manual_index.json",
    ROOT_DIR.parent / "故障手册知识库" / "workbuddy" / "data" / "manual_index.json",
    Path(r"F:\workbuddy\多域AI故障\故障手册知识库\workbuddy\data\manual_index.json"),
]
MANUAL_INDEX_PATH = next((path for path in MANUAL_INDEX_CANDIDATES if path.exists()), MANUAL_INDEX_CANDIDATES[0])


def search_manuals(context: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
    entries = load_manual_entries()
    if not entries:
        return []

    system_name = normalize_text(context.get("system_name") or context.get("system") or "")
    scoped_entries = [entry for entry in entries if same_system(system_name, entry)] if system_name else []
    candidates = scoped_entries or entries

    ranked: list[dict[str, Any]] = []
    for entry in candidates:
        score, reason = score_entry(context, entry, bool(scoped_entries))
        if score <= 0 and not scoped_entries:
            continue
        ranked.append(
            {
                "manual_id": manual_id(entry["source_path"]),
                "title": entry["title"],
                "system_name": entry["system"],
                "category": entry["classification"],
                "score": score,
                "reason": reason,
                "path": entry["source_path"],
            }
        )

    ranked.sort(key=lambda item: (-item["score"], item["title"]))
    return ranked[: max(limit, 0)]


def load_manual_entries() -> list[dict[str, Any]]:
    path = Path(MANUAL_INDEX_PATH)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    entries: list[dict[str, Any]] = []
    for system in data.get("systems", []):
        system_name = str(system.get("system") or "").strip()
        aliases = [str(alias).strip() for alias in system.get("aliases", []) if str(alias).strip()]
        for guide in system.get("guides", []):
            source_path = str(guide.get("path") or guide.get("markdown_path") or guide.get("html_path") or "").strip()
            fault_pattern = str(guide.get("fault_pattern") or guide.get("title") or Path(source_path).stem).strip()
            guide_system = str(guide.get("system") or system_name).strip()
            classification = str(guide.get("classification") or guide.get("category") or "").strip()
            entries.append(
                {
                    "source_path": source_path,
                    "title": f"{guide_system}-{fault_pattern}",
                    "system": guide_system,
                    "aliases": aliases,
                    "fault_pattern": fault_pattern,
                    "classification": classification,
                    "domain": str(guide.get("domain") or system.get("domain") or "").strip(),
                    "details": details_text(guide),
                }
            )
    return entries


def score_entry(context: dict[str, Any], entry: dict[str, Any], system_scoped: bool) -> tuple[int, str]:
    text = context_text(context)
    score = 0
    reasons: list[str] = []

    system_name = normalize_text(context.get("system_name") or context.get("system") or "")
    if system_name and same_system(system_name, entry):
        score += 500
        reasons.append(f"system:{entry['system']}")
    elif system_scoped:
        score += 300

    pattern = entry["fault_pattern"]
    if pattern and contains(text, pattern):
        score += 220
        reasons.append(f"pattern:{pattern}")

    matched_keywords = [keyword for keyword in manual_keywords(pattern, entry["classification"]) if contains(text, keyword)]
    if matched_keywords:
        score += 45 * len(matched_keywords)
        reasons.append("keywords:" + "/".join(matched_keywords[:6]))

    if entry["classification"] and category_matches(text, entry["classification"]):
        score += 80
        reasons.append(f"category:{entry['classification']}")

    detail_hits = [keyword for keyword in detail_keywords(text) if contains(entry["details"], keyword)]
    if detail_hits:
        score += 10 * min(len(detail_hits), 5)
        reasons.append("details")

    if not reasons:
        reasons.append(f"score:{score}")
    return score, ",".join(dict.fromkeys(reasons))


def context_text(context: dict[str, Any]) -> str:
    keys = (
        "system_name",
        "alarm_type",
        "fault_category",
        "fault_pattern",
        "object_name",
        "metric_name",
        "symptom_text",
        "raw_text",
        "alarm_content",
    )
    return " ".join(normalize_text(context.get(key)) for key in keys if normalize_text(context.get(key)))


def details_text(guide: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("symptoms", "recovery_actions"):
        value = guide.get(key) or []
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        else:
            parts.append(str(value))
    for command in guide.get("common_commands", []) or []:
        if isinstance(command, dict):
            parts.extend(str(command.get(key) or "") for key in ("title", "description", "command"))
    return " ".join(parts)


def same_system(system_name: str, entry: dict[str, Any]) -> bool:
    if not system_name:
        return False
    wanted = normalize_system(system_name)
    names = [entry.get("system", ""), *entry.get("aliases", [])]
    return any(normalize_system(name) == wanted for name in names if name)


def normalize_system(value: str) -> str:
    text = normalize_text(value).upper()
    text = re.sub(r"(?:系统|平台)$", "", text)
    text = text.replace("OPEN API", "OPENAPI").replace("OPEN-API", "OPENAPI")
    return text


def manual_keywords(pattern: str, classification: str = "") -> list[str]:
    words = [part for part in re.split(r"[-_、，,与和\s]+", pattern) if len(part) >= 2]
    if "数据库服务器网络服务异常" in pattern:
        words += ["数据库", "网络服务异常", "网络异常", "端口", "连接", "50000", "HTTP", "接口", "成功率", "联通性", "不通", "DB2"]
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
    if classification == "数据库":
        words += ["数据库", "DB2", "SQL", "50000"]
    if classification == "网络":
        words += ["网络", "端口", "连接", "不通"]
    if classification == "应用":
        words += ["接口", "服务", "页面", "任务", "线程池", "CAS"]
    if classification == "主机":
        words += ["主机", "内存", "CPU", "磁盘", "软死锁"]
    return list(dict.fromkeys(words))


def category_matches(text: str, category: str) -> bool:
    category_keywords = {
        "数据库": ["数据库", "DB2", "REDIS", "SQL", "50000", "网络异常", "网络服务异常"],
        "主机": ["主机", "内存", "CPU", "磁盘", "软死锁", "进程"],
        "网络": ["网络", "端口", "连接", "不通", "超时", "30443", "FALSE"],
        "应用": ["接口", "服务", "页面", "任务", "同步", "扫描", "CAS", "ITSM", "线程池"],
        "其他": ["TOMCAT", "REDIS", "中间件"],
    }
    return any(contains(text, keyword) for keyword in category_keywords.get(category, []))


def detail_keywords(text: str) -> list[str]:
    return [word for word in re.split(r"[-_、，,与和\s:：*()\[\]【】]+", text) if len(word) >= 3]


def manual_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def contains(text: str, value: str) -> bool:
    return bool(value) and value.upper() in text.upper()


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())
