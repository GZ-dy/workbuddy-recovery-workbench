import unittest

from rules import focus_items, is_same_alarm, judge_probe_result, parse_alarm, recommended_probe_commands, recommended_probe_items


class RuleTests(unittest.TestCase):
    def test_port_alarm_generates_expected_probe_items(self):
        alarm = parse_alarm("BOMC 系统:OpenAPI系统 端口:8080 当前状态:False")
        self.assertEqual(alarm["alarm_type"], "端口异常")
        self.assertEqual(alarm["system_name"], "OpenAPI系统")
        self.assertIn("端口拨测", recommended_probe_items(alarm["alarm_type"]))
        self.assertIn("DB2拨测", recommended_probe_items(alarm["alarm_type"]))

    def test_probe_result_pass_and_fail(self):
        self.assertEqual(judge_probe_result("DB2拨测：OK")[0], "passed")
        self.assertEqual(judge_probe_result("端口拨测：连接失败")[0], "failed")

    def test_probe_result_accepts_simple_no_abnormal_phrases(self):
        for text in ["拨测正常", "没有告警触发", "未触发告警", "无异常", "未发现异常", "端口TcpTestSucceeded=True"]:
            self.assertEqual(judge_probe_result(text)[0], "passed")

    def test_probe_result_does_not_treat_unrecovered_as_passed(self):
        for text in ["未恢复", "没有恢复", "不正常", "仍有告警触发"]:
            self.assertEqual(judge_probe_result(text)[0], "failed")

    def test_same_alarm_uses_fingerprint(self):
        first = parse_alarm("系统:OpenAPI系统 端口:8080 当前状态:False")
        second = parse_alarm("系统:OpenAPI系统 端口:8080 当前状态:False")
        self.assertTrue(is_same_alarm(first, second))

    def test_bracketed_system_name_stops_before_next_label(self):
        alarm = parse_alarm(
            "系统名称:[OpenAPI系统**其他日志分析][对象]:XJ_openapi_接口[告警内容]:实时检查,当前值为11"
        )
        self.assertEqual(alarm["system_name"], "OpenAPI系统**其他日志分析")
        self.assertEqual(alarm["object_name"], "XJ_openapi_接口")

    def test_port_alarm_recommends_probe_commands(self):
        alarm = parse_alarm("BOMC 系统:OpenAPI系统 端口:8080 当前状态:False")
        commands = recommended_probe_commands(alarm)

        self.assertEqual(commands[0]["title"], "拨测信令接口")
        self.assertIn("Test-NetConnection", commands[1]["command"])
        self.assertIn("8080", commands[1]["command"])

    def test_database_alarm_recommends_db_commands(self):
        alarm = parse_alarm("BOMC 系统:DB2数据库 数据库异常 当前值:error")
        commands = recommended_probe_commands(alarm)

        self.assertEqual(alarm["alarm_type"], "数据库异常")
        self.assertIn("db2 connect", commands[1]["command"])

    def test_legacy_database_alarm_type_recommends_db_commands(self):
        commands = recommended_probe_commands({"alarm_type": "数据库网络异常", "object_name": "10.1.1.1", "raw_text": ""})

        self.assertIn("db2 connect", commands[1]["command"])

    def test_probe_commands_do_not_treat_timestamp_as_port(self):
        alarm = parse_alarm("(业务支持网管系统):2026-04-22 11:44:44 HTTP成功率监控 当前值为0")
        commands = recommended_probe_commands(alarm)

        self.assertNotIn("-Port 2026", commands[2]["command"])
        self.assertIn("-Port 443", commands[2]["command"])

    def test_application_fault_uses_bomc_probe_commands(self):
        alarm = parse_alarm("BOMC 系统无法打开")
        commands = recommended_probe_commands(alarm)

        self.assertEqual(alarm["alarm_type"], "应用服务异常")
        self.assertIn("BOMC/CAS", commands[0]["title"])
        self.assertIn("cas", commands[0]["command"].lower())

    def test_application_fault_uses_openapi_probe_commands(self):
        alarm = parse_alarm("OPENAPI 系统无法打开")
        commands = recommended_probe_commands(alarm)

        self.assertEqual(alarm["alarm_type"], "应用服务异常")
        self.assertEqual(commands[0]["title"], "拨测信令接口")
        self.assertIn("realUsrAreaInfo", commands[0]["command"])
        self.assertIn("END{if(NR==0)print 0}", commands[0]["command"])
        self.assertIn("openapi", commands[1]["command"].lower())

    def test_host_database_and_middleware_probe_items_are_different(self):
        host_alarm = parse_alarm("OPENAPI 主机软死锁 内存使用率过高")
        db_alarm = parse_alarm("OPENAPI DB2数据库连接异常 当前值:error")
        middleware_alarm = parse_alarm("实时营销 Tomcat中间件线程池满 页面无法打开")

        self.assertIn("主机连通性检查", recommended_probe_items(host_alarm))
        self.assertIn("数据库端口连通", recommended_probe_items(db_alarm))
        self.assertIn("中间件实例状态", recommended_probe_items(middleware_alarm))
        self.assertIn("CPU/内存/磁盘/负载是否恢复", focus_items(host_alarm))
        self.assertIn("数据库实例和监听是否正常", focus_items(db_alarm))
        self.assertIn("中间件实例是否恢复", focus_items(middleware_alarm))

    def test_resource_specific_probe_commands_are_different(self):
        host_commands = recommended_probe_commands(parse_alarm("OPENAPI 主机软死锁 内存使用率过高"))
        db_commands = recommended_probe_commands(parse_alarm("OPENAPI DB2数据库连接异常 当前值:error"))
        middleware_commands = recommended_probe_commands(parse_alarm("实时营销 Tomcat中间件线程池满 页面无法打开"))

        self.assertEqual(host_commands[0]["title"], "拨测信令接口")
        self.assertEqual(host_commands[1]["title"], "主机连通性")
        self.assertEqual(db_commands[0]["title"], "拨测信令接口")
        self.assertEqual(db_commands[1]["title"], "数据库端口连通")
        self.assertEqual(middleware_commands[0]["title"], "中间件服务状态")


if __name__ == "__main__":
    unittest.main()
