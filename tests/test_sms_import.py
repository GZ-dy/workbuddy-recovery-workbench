from pathlib import Path
import os
import tempfile
import unittest

import app as workbench
from shared import manual_search


PORT_ALARM = "BOMC 系统:OpenAPI系统 端口:8080 当前状态:False"
DB_ALARM = "BOMC 系统:DB2数据库 数据库异常 当前值:error"
OPENAPI_DATABASE_NETWORK_ALARMS = [
    "(业务支持网管系统):2026-04-22 11:44:44 [告警级别]:5级告警[最后发生时间]:04-22 11:44:35[能力运营中心**异常返回信息]:stp1cnjmcrm-vs-4*AA*能运-openapi新方式(hantele_multareaLatestUserCount)HTTP成功率监控[告警内容]:实时检查,成功率在 [0,90) 区间内,当前值为0[BOMC]",
    "【新一代数智化运维平台】:2026-04-30 17:30:31 [告警级别]:5级告警[最后发生时间]:04-30 17:30:26系统名称:[OpenAPI系统**其他日志分析][对象]:XJ_openapi_信令接口拨测-实时接口*状态_27[对象]:-[对象]:-[对象]:-[告警内容]:实时检查,进程-进程日志监控在 [0,2000] 区间外,当前值为11,085[内容]:-[内容]:-[内容]:-[内容]:-[BOMC]",
    "【广东移动】[新一代数智化运维平台]:2026-05-15 09:39:27 [告警级别]:5级告警[最后发生时间]:05-15 09:39:18系统名称:[OpenAPI系统**端口][对象]:XJ_大数据_OpenAPI_数据库_50000端口状态_OPENAPI-01[告警内容]:连续检查 1 次, 端口连接数始终不合法, 最新的状态为: False[BOMC]",
]


class SmsImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        workbench.DB_PATH = Path(self.tmp.name) / "workbench-test.db"
        workbench.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def set_mail_env(self, **values):
        names = [
            "WORKBENCH_REPORT_MAIL_SMTP_HOST",
            "WORKBENCH_REPORT_MAIL_SMTP_PORT",
            "WORKBENCH_REPORT_MAIL_SMTP_SECURITY",
            "WORKBENCH_REPORT_MAIL_USERNAME",
            "WORKBENCH_REPORT_MAIL_PASSWORD",
            "WORKBENCH_REPORT_MAIL_FROM",
            "WORKBENCH_REPORT_MAIL_TO",
            "WORKBENCH_REPORT_MAIL_CC",
        ]
        old_values = {name: os.environ.get(name) for name in names}

        def restore():
            for name, old_value in old_values.items():
                if old_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old_value

        self.addCleanup(restore)
        for name in names:
            os.environ.pop(name, None)
        for key, value in values.items():
            os.environ[f"WORKBENCH_REPORT_MAIL_{key}"] = value

    def test_single_sms_import_creates_alarm(self):
        result = workbench.import_sms_text(PORT_ALARM)

        self.assertEqual(result["created"], 1)
        alarm = workbench.require_alarm(result["items"][0]["alarm_id"])
        self.assertEqual(alarm["status"], "waiting_manual_recovery")
        self.assertEqual(alarm["alarm_type"], "端口异常")

    def test_batch_sms_import_splits_multiple_messages(self):
        result = workbench.import_sms_text(f"{PORT_ALARM}\n\n{DB_ALARM}")

        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["created"], 2)
        self.assertEqual(len(workbench.list_sms_inbox()["items"]), 2)

    def test_batch_sms_import_splits_bracketed_bomc_markers(self):
        result = workbench.import_sms_text(f"{PORT_ALARM}[BOMC]{DB_ALARM}")

        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["created"], 2)

    def test_duplicate_sms_does_not_create_second_alarm(self):
        first = workbench.import_sms_text(PORT_ALARM)
        second = workbench.import_sms_text(PORT_ALARM)

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(workbench.list_alarms()["items"]), 1)

    def test_force_new_onsite_import_does_not_merge_old_open_alarm(self):
        first = workbench.import_sms_text("告警发生时间：2026-07-06 09:00:00\nBOMC系统无法打开", force_new_event=True)
        second = workbench.import_sms_text("告警发生时间：2026-07-06 10:00:00\nBOMC系统无法打开", force_new_event=True)

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 1)
        self.assertNotEqual(first["items"][0]["alarm_id"], second["items"][0]["alarm_id"])
        self.assertEqual(len(workbench.list_alarms()["items"]), 2)

    def test_onsite_import_uses_local_created_time(self):
        old_now_text = workbench.now_text
        try:
            workbench.now_text = lambda: "2026-07-06 09:14:12"
            result = workbench.import_sms_text("告警发生时间：2026-07-06 09:14:00\nOPENAPI系统无法打开", force_new_event=True)
        finally:
            workbench.now_text = old_now_text

        alarm = workbench.require_alarm(result["items"][0]["alarm_id"])
        sms = workbench.list_sms_inbox()["items"][0]
        self.assertEqual(alarm["alarm_time"], "2026-07-06 09:14:00")
        self.assertEqual(alarm["created_at"], "2026-07-06 09:14:12")
        self.assertEqual(sms["received_at"], "2026-07-06 09:14:12")
        self.assertEqual(sms["created_at"], "2026-07-06 09:14:12")

    def test_observation_same_alarm_returns_to_probe_retry(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        workbench.mark_manual_recovery_done(alarm_id)
        workbench.submit_probe_result(alarm_id, workbench.ProbeResultCreate(raw_result="端口拨测：OK"))

        result = workbench.import_sms_text("BOMC 系统:OpenAPI系统 端口:8080 当前状态:False 再次告警")

        self.assertEqual(result["observation_failures"], 1)
        self.assertEqual(workbench.require_alarm(alarm_id)["status"], "waiting_probe_result")
        window = workbench.fetch_latest_window(alarm_id)
        self.assertEqual(window["status"], "failed")
        self.assertEqual(window["same_alarm_received"], 1)
        meta = workbench.build_report_meta(alarm_id)
        report = workbench.build_report(alarm_id)
        self.assertEqual(meta["conclusion"], "信息不足，待人工确认")
        self.assertIn("已退回拨测验证", meta["decision_gate"])
        self.assertIn("重新拨测", meta["conclusion_reason"])
        self.assertEqual(meta["verification_basis"]["观察窗口内是否复发"], "是")
        self.assertIn("观察期复发告警摘要", meta["verification_basis"])
        self.assertIn("再次告警", meta["verification_basis"]["观察期复发告警摘要"])
        self.assertIn("观察期复发告警摘要", report)
        self.assertIn("再次告警", report)

        retry = workbench.submit_probe_result(alarm_id, workbench.ProbeResultCreate(raw_result="端口拨测正常"))
        self.assertEqual(retry["probe_status"], "passed")
        self.assertEqual(workbench.require_alarm(alarm_id)["status"], "observing")

    def test_observation_different_alarm_does_not_fail_original(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        workbench.mark_manual_recovery_done(alarm_id)
        workbench.submit_probe_result(alarm_id, workbench.ProbeResultCreate(raw_result="端口拨测：OK"))

        result = workbench.import_sms_text(DB_ALARM)

        self.assertEqual(result["created"], 1)
        self.assertEqual(workbench.require_alarm(alarm_id)["status"], "observing")
        window = workbench.fetch_latest_window(alarm_id)
        self.assertEqual(window["status"], "observing")

    def test_manual_observation_same_alarm_returns_to_probe_retry(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        workbench.mark_manual_recovery_done(alarm_id)
        workbench.submit_probe_result(alarm_id, workbench.ProbeResultCreate(raw_result="端口拨测：OK"))

        result = workbench.submit_observation_alarm(
            alarm_id,
            workbench.ObservationAlarmCreate(raw_text="BOMC 系统:OpenAPI系统 端口:8080 当前状态:False 观察期复发"),
        )

        self.assertTrue(result["same_alarm"])
        self.assertEqual(result["final_status"], "waiting_probe_result")
        self.assertIn("重新拨测", result["reason"])
        self.assertEqual(workbench.require_alarm(alarm_id)["status"], "waiting_probe_result")

    def test_passed_probe_time_is_reported_as_fault_end_time(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        workbench.mark_manual_recovery_done(alarm_id)
        old_now_text = workbench.now_text
        try:
            workbench.now_text = lambda: "2026-07-06 10:49:34"
            workbench.submit_probe_result(alarm_id, workbench.ProbeResultCreate(raw_result="端口拨测：OK"))
        finally:
            workbench.now_text = old_now_text

        process_info = workbench.build_report_meta(alarm_id)["process_info"]
        self.assertEqual(process_info["故障结束时间"], "2026-07-06 10:49:34")
        self.assertNotIn("拨测时间", process_info)

    def test_existing_recovery_flow_still_recovers(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        workbench.mark_manual_recovery_done(alarm_id)
        workbench.submit_probe_result(alarm_id, workbench.ProbeResultCreate(raw_result="端口拨测：OK"))
        old_now_text = workbench.now_text
        try:
            workbench.now_text = lambda: "2026-07-06 10:45:00"
            result = workbench.confirm_no_alarm(alarm_id, workbench.ConfirmNoAlarmCreate())
        finally:
            workbench.now_text = old_now_text

        self.assertEqual(result["final_status"], "recovered")
        self.assertIn("截至2026-07-06 10:45:00未收到同类告警", result["reason"])
        self.assertEqual(workbench.require_alarm(alarm_id)["status"], "recovered")
        window = workbench.fetch_latest_window(alarm_id)
        self.assertEqual(window["end_time"], "2026-07-06 10:45:00")
        self.assertIn("恢复结论：已恢复", workbench.build_report(alarm_id))
        meta = workbench.build_report_meta(alarm_id)
        self.assertEqual(meta["conclusion"], "已恢复")
        self.assertEqual(meta["decision_gate"], "已恢复：可归档，按流程确认是否同步记录表")
        self.assertEqual(meta["risk_judgement"]["是否需要同步记录表"], "建议同步记录表")
        self.assertNotEqual(meta["process_info"]["人工确认时间"], workbench.PENDING_INFO)
        self.assertIn("现场处置后按本事件推荐项执行恢复验证", meta["process_info"]["处理动作"])
        self.assertIn("拨测信令接口", meta["process_info"]["处理动作"])
        self.assertEqual(meta["verification_basis"]["观察结束时间"], "2026-07-06 10:45:00")
        self.assertNotIn("建议最早确认时点", meta["verification_basis"])
        self.assertNotIn(workbench.PENDING_INFO, meta["verification_basis"].values())
        self.assertEqual({item["status"] for item in meta["acceptance_criteria"]}, {"通过"})

    def test_failed_probe_stays_in_probe_retry(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        workbench.mark_manual_recovery_done(alarm_id)

        result = workbench.submit_probe_result(
            alarm_id,
            workbench.ProbeResultCreate(raw_result="端口拨测失败，TcpTestSucceeded=False"),
        )

        self.assertEqual(result["probe_status"], "failed")
        self.assertIn("重新拨测", result["next_step"])
        self.assertEqual(workbench.require_alarm(alarm_id)["status"], "waiting_probe_result")
        meta = workbench.build_report_meta(alarm_id)
        self.assertEqual(meta["conclusion"], "信息不足，待人工确认")
        self.assertIn("上轮拨测失败", meta["conclusion_reason"])
        self.assertEqual(meta["process_info"]["处理结果"], "上轮拨测失败，继续排障并重新拨测")

        retry = workbench.submit_probe_result(alarm_id, workbench.ProbeResultCreate(raw_result="端口拨测正常"))
        self.assertEqual(retry["probe_status"], "passed")
        self.assertEqual(workbench.require_alarm(alarm_id)["status"], "observing")

    def test_repeated_failed_probes_are_kept_in_history(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        workbench.mark_manual_recovery_done(alarm_id)

        workbench.submit_probe_result(alarm_id, workbench.ProbeResultCreate(raw_result="第一次端口拨测失败，TcpTestSucceeded=False"))
        workbench.submit_probe_result(alarm_id, workbench.ProbeResultCreate(raw_result="第二次端口拨测失败，仍然异常"))

        detail = workbench.get_alarm(alarm_id)
        self.assertEqual(detail["alarm"]["status"], "waiting_probe_result")
        self.assertEqual([item["probe_status"] for item in detail["probe_results"]], ["failed", "failed"])
        self.assertIn("第一次端口拨测失败", detail["probe_results"][0]["raw_result"])
        self.assertIn("第二次端口拨测失败", detail["probe_results"][1]["raw_result"])

    def test_legacy_probe_failed_status_is_reopened_for_retry(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        conn = workbench.get_db()
        try:
            conn.execute("UPDATE alarm_events SET status = 'failed' WHERE id = ?", (alarm_id,))
            conn.execute(
                """
                INSERT INTO probe_results (alarm_id, raw_result, probe_item, probe_status, ai_summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (alarm_id, "端口拨测失败", "端口拨测", "failed", "拨测结果包含失败或异常信号", workbench.now_text()),
            )
            conn.commit()
        finally:
            conn.close()

        workbench.init_db()

        self.assertEqual(workbench.require_alarm(alarm_id)["status"], "waiting_probe_result")

    def test_init_db_migrates_legacy_database_to_runtime_path(self):
        old_db_path = workbench.DB_PATH
        old_default_path = workbench.DEFAULT_DB_PATH
        old_legacy_path = workbench.LEGACY_DB_PATH
        legacy_db = Path(self.tmp.name) / ".data" / "workbench.db"
        runtime_db = Path(self.tmp.name) / "runtime_data" / "workbench.db"
        try:
            workbench.LEGACY_DB_PATH = legacy_db
            workbench.DB_PATH = legacy_db
            workbench.init_db()
            workbench.import_sms_text(PORT_ALARM)

            workbench.DEFAULT_DB_PATH = runtime_db
            workbench.DB_PATH = runtime_db
            workbench.init_db()

            self.assertTrue(runtime_db.exists())
            self.assertEqual(len(workbench.list_alarms()["items"]), 1)
        finally:
            workbench.DB_PATH = old_db_path
            workbench.DEFAULT_DB_PATH = old_default_path
            workbench.LEGACY_DB_PATH = old_legacy_path

    def test_alarm_detail_includes_linked_sms_for_html_report(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        detail = workbench.get_alarm(alarm_id)

        self.assertEqual(detail["sms_messages"][0]["linked_alarm_id"], alarm_id)
        self.assertIn("report_meta", detail)
        self.assertIn("risk_level", detail["report_meta"])
        self.assertIn("decision_gate", detail["report_meta"])
        self.assertIn("acceptance_criteria", detail["report_meta"])
        self.assertIn("residual_risks", detail["report_meta"])
        self.assertIn("event_info", detail["report_meta"])
        self.assertIn("process_info", detail["report_meta"])
        self.assertIn("verification_basis", detail["report_meta"])
        self.assertIn("risk_judgement", detail["report_meta"])
        self.assertIn("manual_recommendations", detail["report_meta"])
        self.assertIn("missing_info", detail["report_meta"])
        self.assertIn("recommended_probe_commands", detail)
        self.assertIn("manual_links", detail)
        self.assertIn("report", detail)

    def test_report_page_route_returns_html_file(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        response = workbench.report_page(str(alarm_id))

        self.assertTrue(str(response.path).endswith("report.html"))

    def test_report_page_route_accepts_invalid_frontend_id(self):
        response = workbench.report_page("undefined")

        self.assertTrue(str(response.path).endswith("report.html"))

    def test_api_report_page_route_returns_html_file(self):
        response = workbench.api_report_page("1")

        self.assertTrue(str(response.path).endswith("report.html"))

    def test_report_email_dry_run_builds_message_without_smtp(self):
        self.set_mail_env()
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]

        result = workbench.send_report_email(
            alarm_id,
            workbench.ReportEmailCreate(to="leader@example.com", dry_run=True),
        )

        self.assertFalse(result["sent"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["to"], ["leader@example.com"])
        self.assertIn("系统现场恢复验证报告", result["body"])
        self.assertIn(f"ALARM-{alarm_id}", result["subject"])

    def test_report_email_requires_config_before_live_send(self):
        self.set_mail_env()
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]

        with self.assertRaises(workbench.HTTPException) as ctx:
            workbench.send_report_email(alarm_id, workbench.ReportEmailCreate(to="leader@example.com"))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("WORKBENCH_REPORT_MAIL_SMTP_HOST", ctx.exception.detail)

    def test_report_email_uses_smtp_when_configured(self):
        sent = []

        class FakeSMTP:
            def __init__(self, host, port, timeout):
                self.host = host
                self.port = port
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def login(self, username, password):
                sent.append(("login", username, password))

            def send_message(self, msg, to_addrs):
                sent.append(("send", msg["Subject"], msg["From"], list(to_addrs), msg.get_content()))

        self.set_mail_env(
            SMTP_HOST="smtp.example.com",
            SMTP_PORT="465",
            SMTP_SECURITY="ssl",
            USERNAME="sender@example.com",
            PASSWORD="secret",
            FROM="sender@example.com",
            TO="leader@example.com;ops@example.com",
        )
        old_smtp_ssl = workbench.smtplib.SMTP_SSL
        workbench.smtplib.SMTP_SSL = FakeSMTP
        self.addCleanup(setattr, workbench.smtplib, "SMTP_SSL", old_smtp_ssl)
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]

        result = workbench.send_report_email(alarm_id, workbench.ReportEmailCreate())

        self.assertTrue(result["sent"])
        self.assertEqual(result["to"], ["leader@example.com", "ops@example.com"])
        self.assertEqual(sent[0], ("login", "sender@example.com", "secret"))
        self.assertEqual(sent[1][2], "sender@example.com")
        self.assertEqual(sent[1][3], ["leader@example.com", "ops@example.com"])
        self.assertIn("系统现场恢复验证报告", sent[1][4])

    def test_report_email_auth_failure_returns_clear_http_error(self):
        class AuthFailSMTP:
            def __init__(self, host, port, timeout):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def login(self, username, password):
                raise workbench.smtplib.SMTPAuthenticationError(535, b"Error: authentication failed")

            def send_message(self, msg, to_addrs):
                raise AssertionError("send_message should not run after auth failure")

        self.set_mail_env(
            SMTP_HOST="smtp.example.com",
            SMTP_PORT="465",
            SMTP_SECURITY="ssl",
            USERNAME="sender@example.com",
            PASSWORD="bad",
            FROM="sender@example.com",
            TO="leader@example.com",
        )
        old_smtp_ssl = workbench.smtplib.SMTP_SSL
        workbench.smtplib.SMTP_SSL = AuthFailSMTP
        self.addCleanup(setattr, workbench.smtplib, "SMTP_SSL", old_smtp_ssl)
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]

        with self.assertRaises(workbench.HTTPException) as ctx:
            workbench.send_report_email(alarm_id, workbench.ReportEmailCreate())

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("邮件SMTP认证失败", ctx.exception.detail)
        self.assertIn("authentication failed", ctx.exception.detail)

    def test_workbench_page_uses_onsite_recovery_terms(self):
        html = (workbench.BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")

        for text in ["恢复状态总览", "信息不足", "需继续观察", "未恢复", "已恢复", "拨测验证", "拨测历史", "第 ${index + 1} 次拨测", "观察窗口结论", "观察期同类复发告警", "提交同类复发告警", "手册推荐", "创建验证事件", "继续拨测", "ctrlKey", "if (!currentAlarmId)", "alarmTime", "force_new_event", "eventTime", "告警发生", "填入拨测清单", "loadProbeCommands", "selectManualLink", "截至当前确认无复发", "建议最早确认时点", "自动记录当前确认时间", "观察结束时间", "自动发送邮件", "/report/email", "JSON.parse(text).detail"]:
            self.assertIn(text, html)

    def test_manual_route_uses_first_existing_candidate(self):
        old_candidates = workbench.MANUAL_OPENAPI_NETWORK_CANDIDATES
        missing = Path(self.tmp.name) / "missing.html"
        existing = Path(self.tmp.name) / "manual.html"
        existing.write_text("<html>OPENAPI</html>", encoding="utf-8")
        try:
            workbench.MANUAL_OPENAPI_NETWORK_CANDIDATES = [missing, existing]
            response = workbench.manual_openapi_network()

            self.assertEqual(Path(response.path), existing)
        finally:
            workbench.MANUAL_OPENAPI_NETWORK_CANDIDATES = old_candidates

    def test_manual_asset_route_serves_topology_image(self):
        old_roots = workbench.MANUAL_ASSET_ROOTS
        asset_root = Path(self.tmp.name) / "assets"
        asset_root.mkdir()
        image = asset_root / "topology.png"
        image.write_bytes(b"png")
        try:
            workbench.MANUAL_ASSET_ROOTS = [asset_root]
            response = workbench.manual_asset("topology.png")

            self.assertEqual(Path(response.path), image)
        finally:
            workbench.MANUAL_ASSET_ROOTS = old_roots

    def test_manual_matching_uses_index_for_fault_category(self):
        self.write_manual_index()
        alarm = workbench.create_alarm_from_raw("BOMC 系统名称:[OPENAPI][对象]:数据库_50000端口状态 当前状态:False")
        detail = workbench.get_alarm(alarm["id"])

        self.assertEqual(detail["manual_links"][0]["title"], "OPENAPI-数据库服务器网络服务异常")
        self.assertNotIn("system:", detail["manual_links"][0]["reason"])
        self.assertIn("命中系统：OPENAPI", detail["manual_links"][0]["reason"])

    def test_probe_commands_prefer_matched_manual_commands(self):
        self.write_manual_index()
        alarm = workbench.create_alarm_from_raw("BOMC 系统名称:[OPENAPI][对象]:数据库_50000端口状态 当前状态:False")
        detail = workbench.get_alarm(alarm["id"])

        self.assertEqual(detail["recommended_probe_commands"][0]["title"], "拨测信令接口")
        self.assertIn("END{if(NR==0)print 0}", detail["recommended_probe_commands"][0]["command"])
        self.assertEqual(detail["recommended_probe_commands"][1]["title"], "拨测数据库")
        self.assertIn("manual-db-check 10.252.157.161 50000", detail["recommended_probe_commands"][1]["command"])
        self.assertIn("数据库端口可达", detail["recommended_probe_commands"][1]["pass_hint"])
        self.assertEqual(len(detail["recommended_probe_commands"]), 2)

    def test_signal_interface_alarm_also_uses_database_probe_command(self):
        self.write_manual_index()
        raw_text = "系统名称:[OpenAPI系统**其他日志分析][对象]:XJ_openapi_信令接口拨测-实时接口*状态_27[告警内容]:实时检查,进程-进程日志监控在 [0,2000] 区间外,当前值为11,085[BOMC]"
        alarm = workbench.create_alarm_from_raw(raw_text)
        detail = workbench.get_alarm(alarm["id"])

        self.assertEqual(detail["recommended_probe_commands"][0]["title"], "拨测信令接口")
        self.assertEqual(
            detail["recommended_probe_commands"][0]["command"],
            workbench.openapi_signal_probe_command()["command"],
        )
        self.assertEqual(detail["recommended_probe_commands"][1]["title"], "拨测数据库")
        self.assertEqual(len(detail["recommended_probe_commands"]), 2)

    def test_report_process_action_lists_event_probe_commands(self):
        self.write_manual_index()
        alarm = workbench.create_alarm_from_raw(OPENAPI_DATABASE_NETWORK_ALARMS[1])
        meta = workbench.build_report_meta(alarm["id"])
        action = meta["process_info"]["处理动作"]

        self.assertIn("待现场处置确认后按本事件推荐项执行恢复验证", action)
        self.assertIn("1. 拨测信令接口", action)
        self.assertIn("realUsrAreaInfo", action)
        self.assertIn("2. 拨测数据库", action)
        self.assertIn("manual-db-check 10.252.157.161 50000", action)

    def test_openapi_sample_alarms_are_database_network_probes(self):
        self.write_manual_index()
        for raw_text in OPENAPI_DATABASE_NETWORK_ALARMS:
            with self.subTest(raw_text=raw_text[:30]):
                alarm = workbench.create_alarm_from_raw(raw_text)
                detail = workbench.get_alarm(alarm["id"])
                titles = [item["title"] for item in detail["recommended_probe_commands"]]

                self.assertEqual(detail["manual_links"][0]["title"], "OPENAPI-数据库服务器网络服务异常")
                self.assertEqual(titles[:2], ["拨测信令接口", "拨测数据库"])

    def test_manual_matching_falls_back_to_same_system_guides(self):
        self.write_manual_index()
        alarm = workbench.create_alarm_from_raw("BOMC 系统名称:[OPENAPI][对象]:未知日志异常 当前值:11")
        detail = workbench.get_alarm(alarm["id"])

        self.assertTrue(detail["manual_links"])
        self.assertTrue(all(link["title"].startswith("OPENAPI-") for link in detail["manual_links"]))

    def test_bomc_sms_marker_is_not_system_match(self):
        self.write_manual_index(include_bomc=True)
        alarm = workbench.create_alarm_from_raw("BOMC 系统名称:[OPENAPI][对象]:接口HTTP成功率 当前值为0")
        detail = workbench.get_alarm(alarm["id"])

        titles = [link["title"] for link in detail["manual_links"]]
        self.assertEqual(titles[0], "OPENAPI-数据库服务器网络服务异常")
        self.assertTrue(all(title.startswith("OPENAPI-") for title in titles))

    def test_port_alarm_without_system_gets_category_manual(self):
        self.write_manual_index(include_bomc=True)
        alarm = workbench.create_alarm_from_raw("BOMC 系统:未知系统 端口:8123 当前状态:False")
        detail = workbench.get_alarm(alarm["id"])

        self.assertEqual(detail["manual_links"][0]["title"], "BOMC-30443端口冲突与公共模块无法连接")

    def test_bomc_onsite_text_detects_system_and_specific_probes(self):
        self.write_manual_index(include_bomc=True)
        alarm = workbench.create_alarm_from_raw("BOMC 系统无法打开")
        detail = workbench.get_alarm(alarm["id"])

        self.assertEqual(detail["detected_system"], "BOMC")
        self.assertEqual(detail["recommended_probe_commands"][0]["title"], "BOMC/CAS进程检查")
        self.assertTrue(detail["manual_links"][0]["title"].startswith("BOMC-"))

    def test_probe_commands_can_be_generated_from_selected_system(self):
        result = workbench.api_probe_commands(system="OPENAPI", text="")

        self.assertGreaterEqual(len(result["items"]), 4)
        self.assertEqual(result["items"][0]["title"], "拨测信令接口")
        self.assertIn("tail -f /opt/tomcat/apache-tomcat-7.0.77/logs/catalina.$(date +%Y-%m-%d).log", result["items"][0]["command"])
        self.assertIn("END{if(NR==0)print 0}", result["items"][0]["command"])
        self.assertTrue(any("curl" in item["command"] for item in result["items"]))

    def test_manual_by_id_serves_indexed_html(self):
        self.write_manual_index()
        manual_id = workbench.manual_id("处理手册\\经分域\\OPENAPI\\OPENAPI-主机软死锁.md")
        response = workbench.manual_by_id(manual_id)

        self.assertTrue(str(response.path).endswith("OPENAPI-主机软死锁.html"))

    def test_missing_manual_index_uses_fallback_manual_links(self):
        old_index = manual_search.MANUAL_INDEX_PATH
        manual_search.MANUAL_INDEX_PATH = Path(self.tmp.name) / "missing-manual-index.json"
        self.addCleanup(setattr, manual_search, "MANUAL_INDEX_PATH", old_index)

        alarm = workbench.create_alarm_from_raw("BOMC 系统名称:[OPENAPI][对象]:数据库_50000端口状态 当前状态:False")
        detail = workbench.get_alarm(alarm["id"])

        self.assertTrue(detail["manual_links"])
        self.assertEqual(detail["manual_links"][0]["title"], "OPENAPI-数据库服务器网络服务异常")

    def write_manual_index(self, include_bomc=False):
        old_index = workbench.MANUAL_INDEX_PATH
        old_roots = workbench.MANUAL_HTML_ROOTS
        old_search_index = manual_search.MANUAL_INDEX_PATH
        root = Path(self.tmp.name)
        html_root = root / "导出手册" / "html"
        md_root = root / "处理手册"
        (html_root / "经分域" / "OPENAPI").mkdir(parents=True)
        (html_root / "经分域" / "实时营销").mkdir(parents=True)
        (html_root / "B域" / "BOMC").mkdir(parents=True)
        (md_root / "经分域" / "OPENAPI").mkdir(parents=True)
        (html_root / "经分域" / "OPENAPI" / "OPENAPI-数据库服务器网络服务异常.html").write_text("db", encoding="utf-8")
        (html_root / "经分域" / "OPENAPI" / "OPENAPI-主机软死锁.html").write_text("host", encoding="utf-8")
        (html_root / "经分域" / "实时营销" / "实时营销-Tomcat服务异常.html").write_text("tomcat", encoding="utf-8")
        (html_root / "B域" / "BOMC" / "BOMC-线程池满.html").write_text("bomc", encoding="utf-8")
        (html_root / "B域" / "BOMC" / "BOMC-30443端口冲突与公共模块无法连接.html").write_text("port", encoding="utf-8")
        (md_root / "经分域" / "OPENAPI" / "OPENAPI-数据库服务器网络服务异常.md").write_text(
            """
            # OPENAPI - 数据库服务器网络服务异常

            ## 关键检查命令

            ### 拨测数据库主备和 50000 端口

            ```bash
            manual-db-check 10.252.157.161 50000
            manual-db-check 10.252.157.134 50000
            ```

            - 返回 OK：数据库端口可达。

            ### 查 Tomcat 数据库报错

            ```bash
            manual-log-check db2 timeout 50000
            ```

            - 无持续新增数据库连接异常。

            ### 查信令接口拨测日志

            ```bash
            cd /opt/tomcat/apache-tomcat-7.0.77/bin/logs
            LOG="catalina.$(date +%Y-%m-%d).log"
            PATTERN="com.gmcc.realUsrAreaInfo;ESBURL"
            tail -n500 "$LOG" | grep "$PATTERN" | awk -F ':' '{print $NF}'
            ```

            - 恢复后应回落到 0-2000 区间。
            """,
            encoding="utf-8",
        )
        bomc_system = """
                ,
                {
                  "system": "BOMC",
                  "aliases": ["BOMC"],
                  "guides": [
                    {"path": "处理手册\\\\B域\\\\BOMC\\\\BOMC-30443端口冲突与公共模块无法连接.md", "domain": "B域", "system": "BOMC", "fault_pattern": "30443端口冲突与公共模块无法连接", "classification": "网络"},
                    {"path": "处理手册\\\\B域\\\\BOMC\\\\BOMC-线程池满.md", "domain": "B域", "system": "BOMC", "fault_pattern": "线程池满", "classification": "应用"}
                  ]
                }
        """ if include_bomc else ""
        index_path = root / "manual_index.json"
        index_path.write_text(
            """
            {
              "systems": [
                {
                  "system": "OPENAPI",
                  "aliases": ["OpenAPI系统"],
                  "guides": [
                    {"path": "处理手册\\\\经分域\\\\OPENAPI\\\\OPENAPI-数据库服务器网络服务异常.md", "domain": "经分域", "system": "OPENAPI", "fault_pattern": "数据库服务器网络服务异常", "classification": "数据库"},
                    {"path": "处理手册\\\\经分域\\\\OPENAPI\\\\OPENAPI-主机软死锁.md", "domain": "经分域", "system": "OPENAPI", "fault_pattern": "主机软死锁", "classification": "主机"}
                  ]
                },
                {
                  "system": "实时营销",
                  "aliases": [],
                  "guides": [
                    {"path": "处理手册\\\\经分域\\\\实时营销\\\\实时营销-Tomcat服务异常.md", "domain": "经分域", "system": "实时营销", "fault_pattern": "Tomcat服务异常", "classification": "其他"}
                  ]
                }
                __BOMC_SYSTEM__
              ]
            }
            """.replace("__BOMC_SYSTEM__", bomc_system),
            encoding="utf-8",
        )
        workbench.MANUAL_INDEX_PATH = index_path
        workbench.MANUAL_HTML_ROOTS = [html_root]
        manual_search.MANUAL_INDEX_PATH = index_path
        self.addCleanup(setattr, workbench, "MANUAL_INDEX_PATH", old_index)
        self.addCleanup(setattr, workbench, "MANUAL_HTML_ROOTS", old_roots)
        self.addCleanup(setattr, manual_search, "MANUAL_INDEX_PATH", old_search_index)

    def test_text_report_contains_formal_sections(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        report = workbench.build_report(alarm_id)

        self.assertIn("系统现场恢复验证报告", report)
        self.assertIn("一、恢复结论", report)
        self.assertIn("恢复结论：信息不足，待人工确认", report)
        self.assertIn("处置建议", report)
        self.assertIn("二、事件基础信息", report)
        self.assertIn("告警名称", report)
        self.assertIn("三、现场处理过程", report)
        self.assertIn("是否人工确认：否", report)
        self.assertIn("人工确认时间：尚未确认", report)
        self.assertIn("处理动作：待现场处置确认后按本事件推荐项执行恢复验证", report)
        self.assertIn("拨测信令接口", report)
        self.assertIn("四、恢复验证依据", report)
        self.assertIn("告警是否消失", report)
        self.assertIn("五、风险判断", report)
        self.assertIn("是否需要生成正式故障报告", report)
        self.assertIn("六、手册推荐", report)
        self.assertIn("七、需补齐事项", report)
        self.assertIn("八、下一步建议", report)
        self.assertIn("九、恢复验证闭环依据", report)
        self.assertIn("闭环进度", report)
        self.assertNotIn("责任人：", report)
        self.assertNotIn("故障单关闭条件", report)

    def test_unconfirmed_report_does_not_claim_recovered(self):
        alarm_id = workbench.import_sms_text(PORT_ALARM)["items"][0]["alarm_id"]
        meta = workbench.build_report_meta(alarm_id)
        report = workbench.build_report(alarm_id)

        self.assertEqual(meta["conclusion"], "信息不足，待人工确认")
        self.assertEqual(meta["process_info"]["是否人工确认"], "否")
        self.assertEqual(meta["process_info"]["人工确认时间"], "尚未确认")
        self.assertIn("待现场处置确认后按本事件推荐项执行恢复验证", meta["process_info"]["处理动作"])
        self.assertEqual(meta["verification_basis"]["拨测是否成功"], "尚未拨测")
        self.assertNotIn("建议最早确认时点", meta["verification_basis"])
        self.assertNotIn(workbench.PENDING_INFO, meta["verification_basis"].values())
        self.assertIn("恢复结论：信息不足，待人工确认", report)


if __name__ == "__main__":
    unittest.main()
