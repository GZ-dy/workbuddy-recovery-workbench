# AI故障恢复验证工作台

本版本是“人工转发短信收件箱 + 恢复验证报告”的本地工作台。值班人员把一条或多条 BOMC 短信复制到页面，系统自动拆分、解析、去重、归并，并在观察期内判断是否为同类告警复发。

## 运行

```powershell
python -m pip install -r requirements.txt
$env:WORKBENCH_DB = "$PWD\runtime_data\workbench.db"
python -m uvicorn app:app --host 127.0.0.1 --port 8123
```

打开：

```text
http://127.0.0.1:8123
```

演示时可以把观察窗口调短：

```powershell
$env:OBSERVATION_MINUTES = "1"
```

## 当前接口

- `POST /api/sms/import`：批量导入短信，自动拆分、解析、去重、归并。
- `GET /api/sms/inbox`：查看短信收件箱。
- `POST /api/alarms`：单条告警兜底录入。
- `GET /api/alarms`：告警列表。
- `GET /api/alarms/{alarm_id}`：告警详情、拨测记录、观察窗口、报告元数据。
- `POST /api/alarms/{alarm_id}/manual-recovery`：人工确认故障已处理。
- `POST /api/alarms/{alarm_id}/probe-result`：提交拨测结果。
- `POST /api/alarms/{alarm_id}/observation/new-alarm`：观察期新告警录入。
- `POST /api/alarms/{alarm_id}/observation/confirm-no-alarm`：人工确认10分钟无同类告警。
- `GET /api/alarms/{alarm_id}/report`：文本恢复验证报告。
- `GET /reports/{alarm_id}`：HTML交互报告。

## 报告判定口径

报告不只展示字段，必须能支撑值班人员判断“是否允许关闭故障单”。当前判定口径：

- 告警已解析并形成唯一事件，有告警类型和指纹。
- 故障处置动作已完成，事件进入拨测或后续验证阶段。
- 建议拨测项已有结果，且恢复成功必须有通过记录。
- 10分钟观察期内无同类告警复发，或复发时明确判定失败。
- 关闭证据链完整，至少包含短信记录、拨测记录、观察窗口记录。
- 输出关闭建议：允许关闭、禁止关闭继续排障、暂缓关闭等待验证完成。
- 输出遗留风险和后续动作，便于归档、升级或二次验证。

## 数据库

默认使用：

```text
runtime_data/workbench.db
```

核心表：

- `alarm_events`
- `sms_inbox`
- `probe_results`
- `observation_windows`

旧库 `.data/workbench.db`、`restore.db`、`workbench.db` 不再作为当前版本入口使用；如果 `.data/workbench.db` 存在且新库不存在，启动时会自动复制一份到 `runtime_data/workbench.db`。
