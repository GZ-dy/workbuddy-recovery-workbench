from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
MANUAL_INDEX_CANDIDATES = [
    BASE_DIR / "data" / "manual_index.json",
    PROJECT_ROOT / "故障手册知识库" / "workbuddy" / "data" / "manual_index.json",
    PROJECT_ROOT.parent / "故障手册知识库" / "workbuddy" / "data" / "manual_index.json",
]
MANUAL_INDEX_PATH = next((path for path in MANUAL_INDEX_CANDIDATES if path.exists()), MANUAL_INDEX_CANDIDATES[0])
FALLBACK_INDEX_PATHS = [
    PROJECT_ROOT / "高频故障" / "data" / "manual_index.json",
    PROJECT_ROOT / "高频故障" / "workbuddy" / "data" / "manual_index.json",
]


@dataclass(frozen=True)
class ManualCandidate:
    manual_id: str
    title: str
    score: int
    reason: str
    system_name: str
    category: str


@lru_cache(maxsize=1)
def _load_index() -> dict[str, Any]:
    for path in [MANUAL_INDEX_PATH, *FALLBACK_INDEX_PATHS]:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {"systems": []}
    return {"systems": []}


def search_manuals(context: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
    entries = _flatten_entries(_load_index())
    if not entries:
        return _fallback_manuals(context, limit)

    scored: list[ManualCandidate] = []
    for entry in entries:
        score, reason = _score_entry(context, entry)
        if score <= 0:
            continue
        scored.append(
            ManualCandidate(
                manual_id=entry["manual_id"],
                title=entry["title"],
                score=score,
                reason=reason,
                system_name=entry.get("system_name", ""),
                category=entry.get("category", ""),
            )
        )

    scored.sort(key=lambda item: (-item.score, item.title))
    if not scored:
        return _fallback_manuals(context, limit)

    return [
        {
            "manual_id": item.manual_id,
            "title": item.title,
            "score": round(item.score / 100, 2),
            "reason": item.reason,
            "system_name": item.system_name,
            "category": item.category,
        }
        for item in scored[:limit]
    ]


def _flatten_entries(index: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for system in index.get("systems", []) or []:
        system_name = str(system.get("system") or system.get("system_name") or system.get("name") or "").strip()
        aliases = [str(item).strip() for item in system.get("aliases", []) if str(item).strip()]
        resource_kind = _infer_system_kind(system_name, aliases)
        for guide in system.get("guides", []) or []:
            source_path = str(guide.get("path") or guide.get("markdown_path") or guide.get("html_path") or "").strip()
            fault_pattern = str(guide.get("fault_pattern") or guide.get("pattern") or guide.get("title") or guide.get("name") or Path(source_path).stem or "未命名手册").strip()
            guide_system = str(guide.get("system") or system_name).strip()
            title = str(guide.get("title") or f"{guide_system}-{fault_pattern}" if guide_system else fault_pattern).strip()
            manual_id = str(guide.get("manual_id") or guide.get("id") or _manual_id(source_path or title))
            category = str(guide.get("classification") or guide.get("category") or "").strip()
            keywords = _keywords_from_text(" ".join([title, fault_pattern, category, " ".join(aliases)]))
            entries.append(
                {
                    "manual_id": manual_id,
                    "title": title,
                    "system_name": guide_system or system_name,
                    "aliases": aliases,
                    "category": category,
                    "fault_pattern": fault_pattern,
                    "keywords": keywords,
                    "resource_kind": resource_kind,
                }
            )
    return entries


def _score_entry(context: dict[str, Any], entry: dict[str, Any]) -> tuple[int, str]:
    text = _context_text(context)
    score = 0
    reasons: list[str] = []

    system_name = str(context.get("system_name") or "").strip()
    aliases = [str(item).strip() for item in context.get("system_hints", []) if str(item).strip()]
    candidate_names = [entry["system_name"], *entry.get("aliases", [])]
    if system_name and _matches_any(system_name, candidate_names):
        score += 180
        reasons.append(f"system:{entry['system_name'] or system_name}")
    elif any(_matches_any(alias, candidate_names) for alias in aliases):
        score += 120
        reasons.append(f"system:{entry['system_name'] or system_name}")

    kind = _classify_resource_kind(text)
    if kind and kind == entry.get("resource_kind"):
        score += 70
        reasons.append(f"category:{kind}")

    if entry["fault_pattern"] and _contains(text, entry["fault_pattern"]):
        score += 120
        reasons.append(f"pattern:{entry['fault_pattern']}")

    keyword_hits = _keyword_hits(text, entry.get("keywords", []))
    if keyword_hits:
        keyword_score = min(140, len(keyword_hits) * 28)
        score += keyword_score
        reasons.append(f"keywords:{'/'.join(keyword_hits[:4])}")

    category = str(entry.get("category") or "").strip()
    if category and _contains(text, category):
        score += 40
        reasons.append(f"category:{category}")

    if _contains(text, entry["title"]):
        score += 50
        reasons.append("details")

    if not reasons and system_name and _contains(entry["title"], system_name):
        score += 30
        reasons.append(f"system:{system_name}")

    if not reasons:
        return 0, ""

    reasons.append(f"score:{score}")
    return score, ",".join(reasons)


def _fallback_manuals(context: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    text = _context_text(context)
    system_name = str(context.get("system_name") or "").strip()
    kind = _classify_resource_kind(text)
    candidates = [
        ("OPENAPI数据库服务器网络服务异常", "OPENAPI", "database", "系统识别到 OPENAPI 和网络/数据库异常关键词，优先排查链路、端口和数据库连接。", 0.95),
        ("OPENAPI主机软死锁", "OPENAPI", "host", "系统识别到 OPENAPI 和主机异常关键词，优先排查软死锁与资源压力。", 0.93),
        ("BOMC CAS服务异常", "BOMC", "application", "系统识别到 BOMC 和页面异常关键词，优先排查 CAS、登录和线程池。", 0.92),
        ("BOMC 线程池满", "BOMC", "middleware", "系统识别到 BOMC 和线程池异常关键词，优先排查线程池与后端依赖。", 0.91),
        ("SmartBI 线程池满", "SmartBI", "middleware", "系统识别到 SmartBI 和线程池异常关键词，优先排查线程池与页面服务。", 0.9),
        ("下载中心 文件扫描程序卡死", "下载中心", "application", "系统识别到下载中心和任务卡死关键词，优先排查扫描进程与队列。", 0.88),
        ("数据库网络服务异常 通用手册", "数据库", "database", "检测到数据库相关告警，优先排查端口、连通性与数据库登录。", 0.8),
        ("主机资源异常 通用手册", "主机", "host", "检测到主机相关告警，优先排查 CPU、内存、磁盘和进程。", 0.78),
        ("中间件服务异常 通用手册", "中间件", "middleware", "检测到中间件相关告警，优先排查服务进程、端口监听和日志。", 0.76),
    ]
    items: list[dict[str, Any]] = []
    for title, system, resource_kind, reason, score in candidates:
        if kind != resource_kind and not _contains(text, system):
            continue
        if system_name and system_name != system and not _contains(text, system):
            continue
        items.append(
            {
                "manual_id": _manual_id(title),
                "title": title,
                "score": score,
                "reason": f"system:{system}，pattern:{resource_kind}，{reason}",
                "system_name": system,
                "category": resource_kind,
            }
        )
    if not items and system_name:
        items.append(
            {
                "manual_id": _manual_id(system_name),
                "title": f"{system_name} 通用处置手册",
                "score": 0.55,
                "reason": f"system:{system_name}，当前无精确命中，先打开系统通用手册。",
                "system_name": system_name,
                "category": "通用",
            }
        )
    if not items and kind in {"database", "host", "middleware"}:
        generic = {
            "database": ("数据库网络服务异常 通用手册", "数据库", "数据库相关告警，优先排查端口、连通性与数据库登录。"),
            "host": ("主机资源异常 通用手册", "主机", "主机相关告警，优先排查 CPU、内存、磁盘和进程。"),
            "middleware": ("中间件服务异常 通用手册", "中间件", "中间件相关告警，优先排查服务进程、端口监听和日志。"),
        }[kind]
        title, system, reason = generic
        items.append(
            {
                "manual_id": _manual_id(title),
                "title": title,
                "score": 0.7,
                "reason": f"system:{system}，pattern:{kind}，{reason}",
                "system_name": system,
                "category": kind,
            }
        )

    if not items:
        text = _context_text(context)
        keyword_based = [
            ("数据库", "数据库网络服务异常 通用手册", "数据库", "命中数据库关键词，优先排查数据库链路。"),
            ("DB2", "数据库网络服务异常 通用手册", "数据库", "命中 DB2 关键词，优先排查数据库链路。"),
            ("主机", "主机资源异常 通用手册", "主机", "命中主机关键词，优先排查主机资源。"),
            ("服务器", "主机资源异常 通用手册", "主机", "命中服务器关键词，优先排查主机资源。"),
            ("线程池", "中间件服务异常 通用手册", "中间件", "命中线程池关键词，优先排查中间件线程池。"),
            ("中间件", "中间件服务异常 通用手册", "中间件", "命中中间件关键词，优先排查服务进程和日志。"),
            ("Tomcat", "中间件服务异常 通用手册", "中间件", "命中 Tomcat 关键词，优先排查服务进程和日志。"),
            ("Redis", "中间件服务异常 通用手册", "中间件", "命中 Redis 关键词，优先排查缓存与进程。"),
        ]
        for keyword, title, system, reason in keyword_based:
            if _contains(text, keyword):
                items.append(
                    {
                        "manual_id": _manual_id(title),
                        "title": title,
                        "score": 0.68,
                        "reason": f"keywords:{keyword}，{reason}",
                        "system_name": system,
                        "category": system.lower() if system != "数据库" else "database",
                    }
                )
                break

    return items[:limit]


def _context_text(context: dict[str, Any]) -> str:
    parts = [
        context.get("system_name"),
        context.get("alarm_type"),
        context.get("fault_category"),
        context.get("fault_pattern"),
        context.get("object_name"),
        context.get("metric_name"),
        context.get("symptom_text"),
        context.get("raw_text"),
        context.get("alarm_content"),
    ]
    return " ".join(str(part or "") for part in parts)


def _classify_resource_kind(text: str) -> str:
    upper = text.upper()
    if any(word in upper for word in ["DB2", "ORACLE", "MYSQL", "POSTGRES", "SQLSERVER"]) or "数据库" in text:
        return "database"
    if any(word in upper for word in ["TOMCAT", "NGINX", "WEBLOGIC", "WEBSPHERE", "REDIS", "KAFKA", "ZOOKEEPER", "MQ"]) or any(word in text for word in ["中间件", "线程池", "缓存", "消息队列"]):
        return "middleware"
    if any(word in upper for word in ["HOST", "CPU", "MEMORY", "DISK"]) or any(word in text for word in ["主机", "服务器", "内存", "磁盘", "软死锁", "负载"]):
        return "host"
    return "application"


def _infer_system_kind(system_name: str, aliases: list[str]) -> str:
    text = f"{system_name} {' '.join(aliases)}"
    if _contains(text, "OPENAPI"):
        return "database"
    if any(word in text for word in ["BOMC", "SmartBI", "实时营销", "数字员工", "IT工单系统", "下载中心", "自助分析"]):
        return _classify_resource_kind(text)
    return "application"


def _keywords_from_text(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[\s,/，,；;、_()-]+", text) if len(part.strip()) >= 2]
    return list(dict.fromkeys(parts))


def _keyword_hits(text: str, keywords: list[str]) -> list[str]:
    hits: list[str] = []
    for keyword in keywords:
        tokens = _expand_keyword(keyword)
        if any(_contains(text, token) for token in tokens):
            hits.append(keyword)
    return hits


def _expand_keyword(keyword: str) -> list[str]:
    base = str(keyword or "").strip()
    if not base:
        return []
    variants = [base]
    lower = base.lower()
    if lower == "db2":
        variants.extend(["数据库", "数据库连接", "数据库端口", "sql"])
    elif lower in {"tomcat", "nginx", "weblogic", "websphere"}:
        variants.extend(["中间件", "服务", "进程"])
    elif lower in {"redis", "kafka", "zookeeper", "mq"}:
        variants.extend(["中间件", "缓存", "消息队列", "进程"])
    elif lower in {"cpu", "memory", "disk"}:
        variants.extend(["主机", "资源", "负载"])
    elif "线程池" in base:
        variants.extend(["中间件", "服务", "卡死"])
    elif "页面" in base or "接口" in base:
        variants.extend(["应用", "服务", "访问"])
    return list(dict.fromkeys(variants))


def _manual_id(value: str) -> str:
    import hashlib

    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _contains(text: str, value: str) -> bool:
    return value.lower() in text.lower()


def _matches_any(value: str, candidates: list[str]) -> bool:
    return any(_contains(candidate, value) or _contains(value, candidate) for candidate in candidates if candidate)
