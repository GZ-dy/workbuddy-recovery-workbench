# 故障恢复验证工作台交接文档

给接手 AI：先读本文件，不要从零开始扫全仓。当前用户在浏览器打开的是 `http://127.0.0.1:8123/`，工作目录是：

```text
F:\workbuddy\多域AI故障\本地\故障恢复验证工作
```

## 当前目标

这是“现场故障恢复验证工作台”，不是完整故障复盘系统。核心流程是：

1. 现场录入故障现象，创建本次验证事件。
2. 推荐处理手册和拨测命令。
3. 值班确认已处理后进入拨测。
4. 拨测失败或观察期同类复发时，回到继续拨测。
5. 拨测通过后进入观察窗口。
6. 观察期无复发后确认已恢复。
7. 输出现场恢复验证报告。

## 运行与验证

启动服务：

```powershell
.\.venv\Scripts\python.exe -B -m uvicorn app:app --host 127.0.0.1 --port 8123
```

或用脚本：

```powershell
.\restart_workbench_8123.bat
```

测试命令必须在本目录运行：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

当前已验证：`54` 个测试通过，服务已重启到 `http://127.0.0.1:8123/`。

运行库：

```text
runtime_data/workbench.db
```

不要为了验证随便污染运行库；优先用单元测试里的临时数据库。

## 当前关键流程状态

### 拨测失败

拨测失败不进入终态失败，保持：

```text
alarm_events.status = waiting_probe_result
```

用户继续排障后可以重新提交拨测结果。所有拨测记录保留在 `probe_results`，前端“拨测历史”会按第 N 次显示。

相关代码：

- `app.py::submit_probe_result`
- `static/index.html::renderProbeHistory`
- `tests/test_sms_import.py::test_failed_probe_stays_in_probe_retry`
- `tests/test_sms_import.py::test_repeated_failed_probes_are_kept_in_history`

### 观察期收到同类告警

用户明确要求：观察期收到同类复发告警后，应返回继续拨测，不要卡在未恢复终态。

当前实现：

- `observation_windows.status = failed`
- `observation_windows.same_alarm_received = 1`
- `alarm_events.status = waiting_probe_result`
- `sms_inbox.status = observation_same_alarm`
- 报告仍保留复发告警摘要和复发时间
- 前端第 3 步高亮：`观察期收到同类复发告警；继续拨测`

相关代码：

- `app.py::mark_observation_retry`
- `app.py::import_sms_text`
- `app.py::submit_observation_alarm`
- `app.py::recovery_reason_for_status`
- `app.py::build_report_meta`
- `app.py::verification_basis_for_report`
- `static/index.html::renderWorkflow`
- `static/index.html::renderObservationEvidence`
- `tests/test_sms_import.py::test_observation_same_alarm_returns_to_probe_retry`
- `tests/test_sms_import.py::test_manual_observation_same_alarm_returns_to_probe_retry`

### 观察期无复发

用户点击“截至当前确认无复发”后：

- 记录当前确认时间
- 观察窗口闭环
- 事件进入 `recovered`
- 报告结论为已恢复

相关代码：

- `app.py::confirm_no_alarm`
- `tests/test_sms_import.py::test_existing_recovery_flow_still_recovers`

## 当前 UI 行为

主要文件：

```text
static/index.html
static/report.html
```

工作台页面：

- 流程卡当前高亮为红色。
- 完成步骤为绿色。
- 未到步骤为灰色。
- “拨测验证”下方有“拨测历史”，显示所有拨测记录。
- “观察窗口结论”上方有观察记录提示框：
  - 同类复发：红色显示“观察期同类复发告警”和原文摘要。
  - 确认无复发：绿色显示结论。
  - 观察中：显示建议确认时间。
- 观察期按钮文案是“提交同类复发告警”。

HTML 报告页面：

- 摘要卡片新增“观察期复发”。
- “恢复验证依据”里会显示：
  - `观察窗口内是否复发`
  - `观察期复发告警时间`
  - `观察期复发告警摘要`

## 当前报告语义

报告不是正式故障复盘，而是现场恢复验证报告。

状态含义：

- `waiting_manual_recovery`：信息不足，待值班确认处置。
- `waiting_probe_result`：信息不足，待拨测验证；也可能是拨测失败或观察期复发后退回继续拨测。
- `observing`：拨测通过，观察窗口中。
- `recovered`：观察期无复发，恢复闭环。
- `failed`：只用于真正需要失败终态的场景；当前“拨测失败”和“观察期同类复发”都不应停在这个状态。

## 重要文件

```text
app.py
rules.py
static/index.html
static/report.html
tests/test_sms_import.py
tests/test_rules.py
shared/manual_search.py
runtime_data/workbench.db
```

## 不要破坏的约束

- 不要把拨测失败直接改成终态未恢复。
- 不要把观察期同类复发直接改成终态未恢复。
- 不要删除 `probe_results` 历史展示。
- 不要让“已处理”这类文本直接判定恢复；需要真实拨测输出。
- 不要在运行库随意造测试数据。
- 如果要测试业务流，用单元测试临时库。
- 当前用户偏好中文、短闭环、先诊断再改。

## 常用检查

页面是否返回：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8123/
```

查询事件：

```powershell
Invoke-RestMethod http://127.0.0.1:8123/api/alarms
```

查询某条事件详情：

```powershell
Invoke-RestMethod http://127.0.0.1:8123/api/alarms/<id>
```

打开报告：

```text
http://127.0.0.1:8123/reports/<id>
```

## 下一位 AI 的建议处理方式

1. 先复现用户当前截图对应的事件 ID。
2. 用 `/api/alarms/<id>` 看真实状态、`probe_results`、`observation_window`、`sms_messages`。
3. 再改 `app.py` 或 `static/index.html`。
4. 每次改完至少跑：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

5. 如果影响浏览器页面，重启 8123 服务，并让用户刷新页面。

