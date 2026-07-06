from __future__ import annotations

import hashlib
import re


OPENAPI_SIGNAL_PROBE_COMMAND = (
    "tail -f /opt/tomcat/apache-tomcat-7.0.77/logs/catalina.$(date +%Y-%m-%d).log "
    '| grep "com.gmcc.realUsrAreaInfo;ESBURL" '
    "| grep -v 'start httpclient.execute....' "
    "| awk -F ':' '{sub(/ms$/,\" \",$NF);print $NF}' "
    "| awk 'NR{print}END{if(NR==0)print 0}'"
)


def parse_alarm(raw_text: str) -> dict[str, str]:
    text = raw_text.strip()
    alarm_type = classify_alarm(text)
    system_name = extract_system_name(text)
    object_name = extract_object_name(text)
    metric_name = metric_for_type(alarm_type)
    parsed = {
        "alarm_time": extract_alarm_time(text),
        "system_name": system_name,
        "alarm_level": extract_alarm_level(text),
        "object_name": object_name,
        "alarm_content": text[:500],
        "alarm_type": alarm_type,
        "metric_name": metric_name,
        "current_value": extract_current_value(text),
    }
    parsed["fingerprint"] = build_fingerprint(system_name, alarm_type, object_name, metric_name)
    return parsed


def classify_alarm(text: str) -> str:
    upper = text.upper()
    if "成功率" in text or "HTTP" in upper:
        return "接口成功率异常"
    if "端口" in text or "FALSE" in upper:
        return "端口异常"
    if "DB2" in upper or "数据库" in text:
        return "数据库异常"
    if any(word in text for word in ["页面打不开", "无法打开", "系统不可用", "登录失败", "系统无法访问"]):
        return "应用服务异常"
    if "日志" in text or "当前值" in text:
        return "日志异常"
    return "通用告警"


def recommended_probe_items(alarm_or_type: str | dict[str, str]) -> list[str]:
    if isinstance(alarm_or_type, dict):
        kind = alarm_resource_kind(alarm_or_type)
        if kind == "host":
            return ["主机连通性检查", "CPU/内存/磁盘检查", "系统日志和关键进程检查"]
        if kind == "database":
            return ["数据库端口连通", "数据库登录/轻量SQL", "应用连接池报错复查"]
        if kind == "middleware":
            return ["中间件实例状态", "监听端口与线程池检查", "中间件错误日志复查"]
        alarm_type = alarm_or_type.get("alarm_type", "通用告警")
    else:
        alarm_type = alarm_or_type
    if alarm_type == "接口成功率异常":
        return ["接口HTTP拨测", "核心交易接口返回码检查", "服务连通性检查"]
    if alarm_type == "端口异常":
        return ["端口拨测", "DB2拨测", "服务连通性检查"]
    if "数据库" in alarm_type:
        return ["DB2拨测", "数据库连接检查", "关键SQL查询检查"]
    if alarm_type == "应用服务异常":
        return ["服务进程检查", "服务状态检查", "监听端口检查"]
    if alarm_type == "日志异常":
        return ["日志关键字复查", "应用服务状态检查", "异常指标回落确认"]
    return ["服务连通性检查", "业务页面拨测", "关键依赖检查"]


def recommended_probe_commands(alarm: dict[str, str]) -> list[dict[str, str]]:
    alarm_type = alarm.get("alarm_type", "通用告警")
    raw_text = alarm.get("raw_text", "")
    object_name = alarm.get("object_name") or ""
    target = best_target(object_name, raw_text)
    port = best_port(object_name, raw_text)
    url = target if target.lower().startswith(("http://", "https://")) else "<接口URL>"
    system = probe_system(alarm)
    kind = alarm_resource_kind(alarm)
    mandatory = [openapi_signal_probe_command()] if system == "OPENAPI" else []

    if kind == "host":
        return mandatory + [
            command("主机连通性", f"Test-NetConnection {target}", "PingSucceeded=True 或网络可达"),
            command("主机资源检查", 'Get-Counter "\\Processor(_Total)\\% Processor Time","\\Memory\\Available MBytes","\\LogicalDisk(_Total)\\% Free Space"', "CPU、内存、磁盘未持续告警"),
            command("系统错误事件", 'Get-EventLog -LogName System -Newest 50 -EntryType Error', "无同一主机持续错误事件"),
        ]
    if kind == "database":
        return mandatory + [
            command("数据库端口连通", f"Test-NetConnection {target} -Port {port or 50000}", "TcpTestSucceeded=True"),
            command("数据库连接验证", "db2 connect to <DB_ALIAS> user <USER> using <PASSWORD>", "连接成功，无 SQLSTATE 连接错误"),
            command("数据库轻量SQL", 'db2 "select current timestamp from sysibm.sysdummy1"', "SQL正常返回当前时间"),
        ]
    if kind == "middleware":
        return mandatory + [
            command("中间件服务状态", 'Get-Service "<中间件服务名>" | Format-Table Status,Name,DisplayName', "服务状态为 Running"),
            command("中间件端口探活", f"Test-NetConnection {target} -Port {port or '<端口>'}", "TcpTestSucceeded=True"),
            command("中间件错误日志", 'Get-Content "<中间件日志路径>" -Tail 200 | Select-String -Pattern "ERROR|Exception|timeout|失败|异常"', "无持续新增同类异常"),
        ]
    if alarm_type == "接口成功率异常":
        return mandatory + [
            command("接口HTTP返回码", f'curl.exe -I --max-time 10 "{url}"', "返回 200/302 或业务约定成功码"),
            command("接口耗时与状态码", f'curl.exe -s -o NUL -w "%{{http_code}} %{{time_total}}\\n" "{url}"', "状态码正常且耗时未明显升高"),
            command("服务端口连通", f"Test-NetConnection {target} -Port {port or 443}", "TcpTestSucceeded=True"),
        ]
    if alarm_type == "端口异常":
        return mandatory + [
            command("端口连通性", f"Test-NetConnection {target} -Port {port or '<端口>'}", "TcpTestSucceeded=True"),
            command("目标端口快速判断", f"tnc {target} -Port {port or '<端口>'}", "TcpTestSucceeded=True"),
            command("关联服务HTTP探活", f'curl.exe -I --max-time 10 "http://{target}:{port or "<端口>"}/"', "能返回 HTTP 状态码或明确服务响应"),
        ]
    if "数据库" in alarm_type:
        return mandatory + [
            command("数据库端口连通", f"Test-NetConnection {target} -Port {port or 50000}", "TcpTestSucceeded=True"),
            command("DB2连接验证", "db2 connect to <DB_ALIAS> user <USER> using <PASSWORD>", "连接成功，无 SQLSTATE 连接错误"),
            command("DB2轻量SQL", 'db2 "select current timestamp from sysibm.sysdummy1"', "SQL正常返回当前时间"),
        ]
    if alarm_type == "应用服务异常":
        specific = system_probe_commands(system, target, port)
        if specific:
            return mandatory + specific
        return mandatory + [
            command("服务进程检查", 'ps -ef | grep -Ei "service|tomcat|web|app" | grep -v grep', "进程存活且无重复/僵死"),
            command("服务状态检查", 'systemctl status <服务名> --no-pager', "服务状态为 Running"),
            command("监听端口检查", 'ss -lntp | grep -Ei "service|tomcat|web|app|8080|8443"', "端口监听正常"),
        ]
    if alarm_type == "日志异常":
        return mandatory + [
            command("异常关键字复查", 'Get-Content "<日志路径>" -Tail 200 | Select-String -Pattern "ERROR|Exception|timeout|失败|异常"', "无持续新增同类异常"),
            command("应用服务状态", 'Get-Service "<服务名>" | Format-Table Status,Name,DisplayName', "服务状态为 Running"),
            command("最近错误事件", 'Get-EventLog -LogName Application -Newest 50 -EntryType Error', "无同一组件持续错误事件"),
        ]
    return mandatory + [
        command("主机连通性", f"Test-NetConnection {target}", "PingSucceeded=True 或网络可达"),
        command("业务URL探活", f'curl.exe -I --max-time 10 "{url}"', "返回正常 HTTP 状态码"),
        command("服务状态检查", 'Get-Service "<服务名>" | Format-Table Status,Name,DisplayName', "关键服务为 Running"),
    ]


def openapi_signal_probe_command() -> dict[str, str]:
    return command("拨测信令接口", OPENAPI_SIGNAL_PROBE_COMMAND, "输出为 0 或回落到正常区间")


def probe_system(alarm: dict[str, str]) -> str:
    text = f"{alarm.get('system_name', '')} {alarm.get('raw_text', '')} {alarm.get('alarm_content', '')}".upper()
    rules = [
        ("OPENAPI", ["OPENAPI", "OPEN API", "OPEN-API"]),
        ("BOMC", ["BOMC"]),
        ("实时营销", ["实时营销"]),
        ("数字员工", ["数字员工", "智能小7", "SNC-DIGITAL-EMPLOYEE", "SNC-IM-CHAT"]),
        ("IT工单系统", ["IT工单系统", "ITSM"]),
        ("自助分析", ["自助分析"]),
        ("下载中心", ["下载中心"]),
        ("SmartBI", ["SMARTBI"]),
        ("大数据平台K8S集群", ["大数据平台K8S集群", "K8S", "KUBERNETES"]),
    ]
    for system, keywords in rules:
        if any(keyword.upper() in text for keyword in keywords):
            return system
    return ""


def system_probe_commands(system: str, target: str, port: str) -> list[dict[str, str]]:
    profiles = {
        "BOMC": [
            command("BOMC/CAS进程检查", 'ps -ef | grep -Ei "bomc|cas|web" | grep -v grep', "cas、web、bomc 相关进程存在且无僵死"),
            command("BOMC/CAS服务状态", 'systemctl status bomc --no-pager', "服务为 active/running，最近无持续异常"),
            command("BOMC页面探活", 'curl -I --max-time 10 "http://<BOMC地址>/cas/login"', "登录页返回 200/302"),
            command("BOMC最近日志", 'tail -n 200 /var/log/bomc.log | grep -Ei "ERROR|Exception|timeout|无法|失败"', "无持续新增同类错误"),
        ],
        "OPENAPI": [
            command("OPENAPI接口探活", 'curl -s -o /dev/null -w "%{http_code} %{time_total}\\n" "http://<OPENAPI地址>/health"', "返回 200/302 且耗时正常"),
            command("OPENAPI进程检查", 'ps -ef | grep -Ei "openapi|api-gateway|nginx" | grep -v grep', "接口服务进程存在且无重复僵死"),
            command("OPENAPI端口监听", f"ss -lntp | grep -E \"{port or '8080|8443|80|443'}\"", "关键端口处于 LISTEN"),
            command("OPENAPI数据库链路", f"Test-NetConnection {target} -Port {port or 50000}", "TcpTestSucceeded=True"),
        ],
        "实时营销": [
            command("Tomcat进程检查", 'ps -ef | grep -Ei "tomcat|realtime|marketing" | grep -v grep', "Tomcat/实时营销进程存在"),
            command("Tomcat端口监听", 'ss -lntp | grep -Ei "8080|8443|tomcat"', "Tomcat端口处于 LISTEN"),
            command("实时营销页面探活", 'curl -I --max-time 10 "http://<实时营销地址>/"', "页面返回正常 HTTP 状态码"),
            command("Tomcat错误日志", 'tail -n 200 <tomcat日志路径>/catalina.out | grep -Ei "ERROR|Exception|timeout"', "无持续新增同类异常"),
        ],
        "数字员工": [
            command("数字员工接口探活", 'curl -s -o /dev/null -w "%{http_code} %{time_total}\\n" "http://<数字员工接口>/health"', "接口返回成功码且耗时正常"),
            command("Redis状态检查", 'redis-cli -h <redis-host> -p <port> info memory', "Redis 可连接且内存未持续上涨"),
            command("机器人服务进程", 'ps -ef | grep -Ei "digital|employee|chatbot|snc" | grep -v grep', "机器人相关服务进程存在"),
            command("接口错误日志", 'tail -n 200 <应用日志路径> | grep -Ei "ERROR|Exception|null|timeout"', "无持续新增接口异常"),
        ],
        "IT工单系统": [
            command("ITSM页面探活", 'curl -I --max-time 10 "http://<ITSM地址>/"', "工单页面返回 200/302"),
            command("ITSM服务进程", 'ps -ef | grep -Ei "itsm|tomcat|nginx" | grep -v grep', "ITSM相关进程存在"),
            command("工单数据库连接", f"Test-NetConnection {target} -Port {port or 50000}", "数据库端口可达"),
            command("ITSM应用日志", 'tail -n 200 <ITSM日志路径> | grep -Ei "ERROR|Exception|连接|超时"', "无持续新增业务异常"),
        ],
        "自助分析": [
            command("后台任务进程", 'ps -ef | grep -Ei "analysis|scheduler|worker|task" | grep -v grep', "任务进程存在且无僵死"),
            command("任务队列堆积", 'ps -eo pid,stat,etime,cmd | grep -Ei "analysis|scheduler|worker"', "无长时间 D/Z 状态进程"),
            command("自助分析页面探活", 'curl -I --max-time 10 "http://<自助分析地址>/"', "页面可访问"),
            command("任务日志复查", 'tail -n 200 <任务日志路径> | grep -Ei "ERROR|Exception|卡住|timeout"', "无持续新增任务异常"),
        ],
        "下载中心": [
            command("下载中心任务进程", 'ps -ef | grep -Ei "download|scan|sync" | grep -v grep', "扫描/同步进程存在且无僵死"),
            command("文件扫描进度", 'ls -lt <扫描目录> | head', "扫描目录有更新且无长时间停滞"),
            command("下载服务页面探活", 'curl -I --max-time 10 "http://<下载中心地址>/"', "页面返回正常 HTTP 状态码"),
            command("任务日志复查", 'tail -n 200 <下载中心日志路径> | grep -Ei "ERROR|Exception|timeout|卡死"', "无持续新增任务异常"),
        ],
        "SmartBI": [
            command("SmartBI页面探活", 'curl -I --max-time 10 "http://<SmartBI地址>/"', "登录页返回 200/302"),
            command("SmartBI进程检查", 'ps -ef | grep -Ei "smartbi|tomcat|java" | grep -v grep', "SmartBI/Tomcat进程存在"),
            command("线程池状态", 'jstack <java_pid> | grep -Ei "BLOCKED|WAITING|http|pool" | head -n 40', "无大量阻塞线程持续堆积"),
            command("SmartBI错误日志", 'tail -n 200 <SmartBI日志路径> | grep -Ei "ERROR|Exception|timeout"', "无持续新增同类异常"),
        ],
        "大数据平台K8S集群": [
            command("K8S节点状态", 'kubectl get nodes -o wide', "相关节点 Ready"),
            command("Pod状态检查", 'kubectl get pods -A | grep -Ev "Running|Completed"', "无关键 Pod 异常"),
            command("节点内存检查", 'kubectl top nodes', "故障节点内存使用率回落"),
            command("节点事件复查", 'kubectl describe node <node-name> | grep -Ei "MemoryPressure|DiskPressure|NotReady"', "无持续资源压力"),
        ],
    }
    return profiles.get(system, [])


def command(title: str, value: str, pass_hint: str) -> dict[str, str]:
    return {"title": title, "command": value, "pass_hint": pass_hint}


def best_target(object_name: str, raw_text: str) -> str:
    text = f"{object_name} {raw_text}"
    url = re.search(r"https?://[^\s，。；;\]]+", text, re.I)
    if url:
        return url.group(0)
    host = re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
    if host:
        return host.group(0)
    domain = re.search(r"\b[a-zA-Z0-9][a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b", text)
    if domain:
        return domain.group(0)
    return "<目标IP或域名>"


def best_port(object_name: str, raw_text: str) -> str:
    text = f"{object_name} {raw_text}"
    labelled = re.search(r"(?:端口|port)[:：_ -]*(\d{2,5})", text, re.I)
    if labelled:
        return labelled.group(1)
    host_port = re.search(
        r"(?:https?://[^/\s:]+|\b\d{1,3}(?:\.\d{1,3}){3}\b|\b[a-zA-Z0-9][a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b):(\d{2,5})\b",
        text,
        re.I,
    )
    if host_port:
        return host_port.group(1)
    standalone = re.search(r"\b(50000|8080|8123|443|80|1521|3306|5432)\b", text)
    return standalone.group(1) if standalone else ""


def alarm_resource_kind(alarm: dict[str, str]) -> str:
    text = " ".join(str(alarm.get(key, "") or "") for key in ("object_name", "raw_text", "alarm_content", "alarm_type", "metric_name"))
    upper = text.upper()
    if openapi_database_network_alarm(alarm):
        return "database"
    if any(word in upper for word in ["DB2", "ORACLE", "MYSQL", "POSTGRES", "SQLSERVER"]) or "数据库" in text:
        return "database"
    if any(word in upper for word in ["TOMCAT", "NGINX", "WEBLOGIC", "WEBSPHERE", "REDIS", "KAFKA", "ZOOKEEPER", "MQ"]) or any(word in text for word in ["中间件", "线程池", "缓存", "消息队列"]):
        return "middleware"
    if any(word in upper for word in ["HOST", "CPU", "MEMORY", "DISK"]) or any(word in text for word in ["主机", "服务器", "内存", "磁盘", "软死锁", "负载"]):
        return "host"
    return "application"


def openapi_database_network_alarm(alarm: dict[str, str]) -> bool:
    text = " ".join(str(alarm.get(key, "") or "") for key in ("object_name", "raw_text", "alarm_content", "alarm_type", "metric_name"))
    upper = text.upper()
    if not any(word in upper for word in ["OPENAPI", "OPEN API", "OPEN-API"]):
        return False
    return any(
        word in upper
        for word in [
            "DB2",
            "50000",
            "HTTP成功率",
            "成功率",
            "信令接口",
            "接口拨测",
            "实时接口",
            "OPENAPI新方式",
            "HANTELE_MULTAREALATESTUSERCOUNT",
        ]
    ) or any(word in text for word in ["数据库", "网络异常", "网络服务异常"])


def focus_items(alarm_or_type: str | dict[str, str]) -> list[str]:
    if isinstance(alarm_or_type, dict):
        kind = alarm_resource_kind(alarm_or_type)
        if kind == "host":
            return ["主机是否可达", "CPU/内存/磁盘/负载是否恢复", "系统日志和关键进程是否正常"]
        if kind == "database":
            return ["数据库实例和监听是否正常", "连接池与关键SQL是否恢复", "应用侧数据库报错是否停止"]
        if kind == "middleware":
            return ["中间件实例是否恢复", "端口/线程池/连接数是否正常", "中间件日志是否停止报错"]
        alarm_type = alarm_or_type.get("alarm_type", "通用告警")
    else:
        alarm_type = alarm_or_type
    if alarm_type == "接口成功率异常":
        return ["接口成功率是否恢复", "HTTP返回码是否正常", "核心交易是否可用"]
    if alarm_type == "端口异常":
        return ["端口监听状态", "服务连通性", "关联数据库可用性"]
    if "数据库" in alarm_type:
        return ["数据库连接状态", "关键SQL执行结果", "应用侧数据库报错"]
    if alarm_type == "应用服务异常":
        return ["页面或服务是否恢复", "关键进程是否存活", "端口监听与日志是否正常"]
    if alarm_type == "日志异常":
        return ["异常日志是否持续增长", "当前值是否回落", "应用服务状态"]
    return ["告警对象状态", "业务可用性", "同类告警是否复现"]


def judge_probe_result(raw_result: str) -> tuple[str, str]:
    text = raw_result.strip()
    lower = text.lower()
    clear_fail_phrases = ["未恢复", "没有恢复", "仍未恢复", "未通过", "不正常", "未连通", "无法连通", "仍有告警", "还有告警", "再次告警", "持续告警"]
    clear_pass_phrases = [
        "拨测正常",
        "拨测通过",
        "验证通过",
        "无异常",
        "没有异常",
        "未见异常",
        "未发现异常",
        "无告警",
        "没有告警",
        "未触发告警",
        "没有告警触发",
        "未收到告警",
        "无复发",
        "未复发",
        "服务正常",
        "接口正常",
        "页面正常",
        "端口正常",
        "tcptestsucceeded=true",
    ]
    fail_words = ["fail", "failed", "失败", "异常", "不通", "超时", "error", "false", "不可用", "拒绝", "down"]
    pass_words = ["ok", "pass", "passed", "成功", "正常", "恢复", "200", "可用", "连通"]
    if any(phrase in lower or phrase in text for phrase in clear_fail_phrases):
        return "failed", "拨测结果包含失败或异常信号"
    if any(phrase in lower or phrase in text for phrase in clear_pass_phrases):
        return "passed", "拨测结果表示无异常或未触发告警"
    if any(word in lower or word in text for word in fail_words):
        return "failed", "拨测结果包含失败或异常信号"
    if any(word in lower or word in text for word in pass_words):
        return "passed", "拨测结果包含成功或恢复信号"
    return "unknown", "拨测结果无法通过规则确认，需要人工复核"


def is_same_alarm(original: dict[str, str], candidate: dict[str, str]) -> bool:
    if original.get("fingerprint") and original.get("fingerprint") == candidate.get("fingerprint"):
        return True
    return (
        original.get("system_name") == candidate.get("system_name")
        and original.get("alarm_type") == candidate.get("alarm_type")
        and original.get("metric_name") == candidate.get("metric_name")
    )


def build_fingerprint(system_name: str, alarm_type: str, object_name: str, metric_name: str) -> str:
    parts = [system_name or "未知系统", alarm_type or "通用告警", object_name or "未知对象", metric_name or "告警指标"]
    value = "|".join(part.strip().lower() for part in parts)
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def metric_for_type(alarm_type: str) -> str:
    return {
        "接口成功率异常": "接口成功率",
        "端口异常": "端口状态",
        "数据库异常": "数据库状态",
        "日志异常": "日志指标",
    }.get(alarm_type, "告警指标")


def extract_alarm_time(text: str) -> str:
    match = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?", text)
    return match.group(0) if match else ""


def extract_system_name(text: str) -> str:
    labelled = _extract_label(text, ["系统名称", "应用系统", "业务系统", "系统", "应用"])
    if labelled:
        return labelled
    upper = text.upper()
    if any(word in upper for word in ["BOMC", "B O M C"]):
        return "BOMC"
    if "OPENAPI" in upper or "OPEN API" in upper or "OPEN-API" in upper:
        return "OPENAPI"
    if "DB2" in upper:
        return "DB2数据库"
    if "实时营销" in text:
        return "实时营销"
    if "数字员工" in text:
        return "数字员工"
    if "IT工单系统" in text or "ITSM" in upper:
        return "IT工单系统"
    return "未知系统"


def extract_object_name(text: str) -> str:
    labelled = _extract_label(text, ["告警对象", "对象", "实例", "主机", "端口", "URL"])
    if labelled:
        return labelled
    url = re.search(r"https?://[^\s，。；;]+", text, re.I)
    if url:
        return url.group(0)
    host = re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", text)
    if host:
        return host.group(0)
    return "未知对象"


def extract_alarm_level(text: str) -> str:
    upper = text.upper()
    if any(word in text for word in ["一级", "严重", "紧急"]) or "CRITICAL" in upper or "P1" in upper:
        return "严重"
    if any(word in text for word in ["二级", "重要"]) or "MAJOR" in upper or "P2" in upper:
        return "重要"
    if any(word in text for word in ["三级", "一般"]) or "MINOR" in upper or "P3" in upper:
        return "一般"
    level = _extract_label(text, ["告警级别", "级别"])
    return level or "未知"


def extract_current_value(text: str) -> str:
    match = re.search(r"(?:当前值|当前状态|current_value)[:：为]?\s*([^\s，。；;]+)", text, re.I)
    if match:
        return match.group(1)
    if "False" in text:
        return "False"
    return ""


def _extract_label(text: str, labels: list[str]) -> str:
    stop_labels = [
        "告警级别",
        "最后发生时间",
        "系统名称",
        "应用系统",
        "业务系统",
        "系统",
        "应用",
        "对象",
        "告警对象",
        "实例",
        "主机",
        "端口",
        "URL",
        "告警内容",
        "当前值",
        "当前状态",
        "内容",
        "执行结果",
        "BOMC",
    ]
    stop = "|".join(re.escape(label) for label in stop_labels)
    for label in labels:
        pattern = rf"\[?{re.escape(label)}\]?[:：]\s*(.+?)(?=(?:\[?(?:{stop})\]?[:：])|$)"
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip().strip("，。；;")
            if value.startswith("[") and value.endswith("]") and value.count("[") == 1 and value.count("]") == 1:
                value = value[1:-1]
            return value
    return ""
