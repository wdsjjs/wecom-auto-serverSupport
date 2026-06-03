import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from csbot.autonomous_worker import build_autonomous_prompt, run_autonomous_worker
from csbot.db import connect, ensure_schema
from csbot.debug_server import run_debug_case


def autonomous_reply() -> dict:
    return {
        "action": "send",
        "reply_text": "女维每日 1 粒，随餐服用。",
        "used_script_sources": [
            {"sheet": "5 产品常规信息", "row": 2, "field": "服用方法", "kb_doc_id": "doc-usage-1"}
        ],
        "used_vector_memories": [],
        "confidence": 0.92,
        "commands_run": [
            {"command": "sqlite3 state.sqlite SELECT ...", "purpose": "查询女维服用方法", "success": True}
        ],
        "conflicts": [],
        "retrieval_summary": "查询 kb_docs 后采用女维服用方法字段作为事实来源。",
        "decision_basis": "知识库有明确服用方法且无冲突，因此直接回复。",
    }


class AutonomousWorkerContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite"
        self.env_patch = mock.patch.dict(
            "os.environ",
            {
                "CSBOT_CODEX_MODEL_CATALOG": str(Path(self.tmp.name) / "catalog.json"),
                "CSBOT_CODEX_HOME": str(Path(self.tmp.name) / "codex-home"),
                "CSBOT_CODEX_ISOLATED_HOME": "0",
                "CSBOT_CODEX_EXTRA_ARGS": "",
                "CSBOT_CODEX_OUTPUT_SCHEMA": "",
                "CSBOT_CODEX_WORKDIR": self.tmp.name,
                "CSBOT_PG_DSN": "",
            },
            clear=False,
        )
        self.env_patch.start()
        self.agents_path = Path(self.tmp.name) / "AGENTS.md"
        self.agents_path.write_text(
            "\n".join(
                [
                    "自定义客服规则：非必要不追问。",
                    "你可以自主选择工具，不强制固定第一步。",
                    "订单、物流、工单、企微用户信息必须调用 `csbot ops`。",
                    "不要阅读项目源码。",
                    "微伴 FAQ 已同步进 kb_docs，business_type=faq，适合客服快捷回复、常见问答、售前售后固定话术。",
                    "最终只能输出 JSON。",
                    "JSON 字段必须包含 action、reply_text、decision_basis。",
                ]
            ),
            encoding="utf-8",
        )
        self._seed_kb_doc()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def _seed_kb_doc(self) -> None:
        conn = connect(self.db)
        try:
            ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO kb_docs
                    (kb_doc_id, kb_version, business_type, product, topic, text, facts_json,
                     source_sheet, source_row, source_field)
                VALUES
                    ('doc-usage-1', 'test-v1', 'product_usage', '女士复合维生素', '服用方法',
                     '女维每日 1 粒，随餐服用。', '{}', '5 产品常规信息', 2, '服用方法')
                """
            )
            conn.execute(
                """
                INSERT INTO kb_docs
                    (kb_doc_id, kb_version, business_type, product, topic, text, facts_json,
                     source_sheet, source_row, source_field)
                VALUES
                    ('doc-paper-1', 'test-v1', 'research_evidence', '婴幼少儿 DHA 藻油', 'DHA 与儿童成长研究',
                     '产品: 婴幼少儿 DHA 藻油\n论文方向: 儿童成长研究\n标题: DHA 与儿童成长研究\n链接: https://example.com/dha-paper',
                     '{"产品":"婴幼少儿 DHA 藻油","论文方向":"儿童成长研究","标题":"DHA 与儿童成长研究","链接":"https://example.com/dha-paper"}',
                     '6 论文表', 8, 'row')
                """
            )
            conn.commit()
        finally:
            conn.close()

    def test_run_autonomous_worker_accepts_mocked_codex_contract(self) -> None:
        reply = autonomous_reply()

        def fake_run(command, **kwargs):
            out_path = Path(command[command.index("--output-last-message") + 1])
            out_path.write_text(json.dumps(reply, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch("csbot.codex_cli.ensure_model_catalog") as catalog_mock:
            catalog_mock.return_value.enabled = True
            catalog_mock.return_value.path = str(Path(self.tmp.name) / "catalog.json")
            catalog_mock.return_value.generated = False
            catalog_mock.return_value.error = ""
            run_patch = mock.patch("csbot.autonomous_worker.subprocess.run", side_effect=fake_run)
            run_mock = run_patch.start()
            result = run_autonomous_worker(
                customer_id="cust-1",
                query="女维怎么吃",
                context={"known_facts": {}},
                db_path=self.db,
                timeout=30,
            )
            run_patch.stop()

        self.assertEqual(result["mode"], "autonomous")
        self.assertEqual(result["reply"], reply)
        self.assertTrue(result["validation"]["ok"])
        self.assertEqual(result["parse_error"], "")
        self.assertIn("danger-full-access", result["command"])
        self.assertEqual(result["command"][result["command"].index("-C") + 1], self.tmp.name)
        self.assertNotIn("--output-schema", result["command"])
        self.assertIn("-c", result["command"])
        self.assertIn("model_catalog_json=", " ".join(result["command"]))
        self.assertEqual(result["codex_cli"]["model_catalog"]["path"], str(Path(self.tmp.name) / "catalog.json"))
        self.assertFalse(result["codex_cli"]["isolated_home"]["enabled"])
        self.assertIn("decision_basis", result["prompt"])
        run_mock.assert_called_once()

    def test_safe_json_parse_extracts_pi_markdown_json_block(self) -> None:
        from csbot.autonomous_worker import safe_json_parse

        wrapped = (
            "Now I have all the data I need.\n\n"
            "```json\n"
            + json.dumps(autonomous_reply(), ensure_ascii=False)
            + "\n```\n"
        )

        reply, parse_error = safe_json_parse(wrapped)

        self.assertEqual(reply, autonomous_reply())
        self.assertEqual(parse_error, "")

    def test_safe_json_parse_ignores_source_object_without_action(self) -> None:
        from csbot.autonomous_worker import safe_json_parse

        reply, parse_error = safe_json_parse(
            '{"sheet":"5 产品常规信息","row":28,"field":"product_profile","kb_doc_id":"5_28"}'
        )

        self.assertIsNone(reply)
        self.assertEqual(parse_error, "codex_output_is_not_json")

    def test_autonomous_worker_can_enable_codex_output_schema(self) -> None:
        reply = autonomous_reply()

        def fake_run(command, **kwargs):
            out_path = Path(command[command.index("--output-last-message") + 1])
            out_path.write_text(json.dumps(reply, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch.dict("os.environ", {"CSBOT_CODEX_OUTPUT_SCHEMA": "1"}, clear=False):
            with mock.patch("csbot.codex_cli.ensure_model_catalog") as catalog_mock:
                catalog_mock.return_value.enabled = True
                catalog_mock.return_value.path = str(Path(self.tmp.name) / "catalog.json")
                catalog_mock.return_value.generated = False
                catalog_mock.return_value.error = ""
                with mock.patch("csbot.autonomous_worker.subprocess.run", side_effect=fake_run):
                    result = run_autonomous_worker(
                        customer_id="cust-1",
                        query="女维怎么吃",
                        context={"known_facts": {}},
                        db_path=self.db,
                        timeout=30,
                    )

        self.assertIn("--output-schema", result["command"])

    def test_autonomous_worker_does_not_require_script_sources(self) -> None:
        reply = autonomous_reply()
        reply["used_script_sources"] = []
        reply["decision_basis"] = "Codex 自主判断 MEM0 和上下文足够回答。"

        def fake_run(command, **kwargs):
            out_path = Path(command[command.index("--output-last-message") + 1])
            out_path.write_text(json.dumps(reply, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch("csbot.codex_cli.ensure_model_catalog") as catalog_mock:
            catalog_mock.return_value.enabled = True
            catalog_mock.return_value.path = str(Path(self.tmp.name) / "catalog.json")
            catalog_mock.return_value.generated = False
            catalog_mock.return_value.error = ""
            run_patch = mock.patch("csbot.autonomous_worker.subprocess.run", side_effect=fake_run)
            run_patch.start()
            result = run_autonomous_worker(
                customer_id="cust-1",
                query="女维怎么吃",
                context={"known_facts": {}},
                db_path=self.db,
                timeout=30,
            )
            run_patch.stop()

        self.assertEqual(result["reply"], reply)
        self.assertTrue(result["validation"]["ok"])

    def test_supplement_send_appends_paper_links_from_database(self) -> None:
        reply = {
            "action": "send",
            "reply_text": "结合您的需求，为您推荐这几款产品组合。接下来，我详细为您介绍下：\n婴幼少儿 DHA 藻油：适合儿童成长相关需求。",
            "used_script_sources": [
                {"sheet": "10 补剂推荐", "row": 2, "field": "row", "kb_doc_id": "doc-rec-1"}
            ],
            "used_vector_memories": [],
            "confidence": 0.9,
            "commands_run": [],
            "conflicts": [],
            "retrieval_summary": "已查询补剂推荐规则。",
            "decision_basis": "命中儿童成长推荐规则。",
        }

        def fake_run(command, **kwargs):
            out_path = Path(command[command.index("--output-last-message") + 1])
            out_path.write_text(json.dumps(reply, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch("csbot.codex_cli.ensure_model_catalog") as catalog_mock:
            catalog_mock.return_value.enabled = True
            catalog_mock.return_value.path = str(Path(self.tmp.name) / "catalog.json")
            catalog_mock.return_value.generated = False
            catalog_mock.return_value.error = ""
            with mock.patch("csbot.autonomous_worker.subprocess.run", side_effect=fake_run):
                result = run_autonomous_worker(
                    customer_id="cust-supplement",
                    query="儿童成长 推荐",
                    context={"agent_mode": "supplement", "agent_context": {"reply_source": "supplement"}},
                    db_path=self.db,
                    timeout=30,
                )

        reply_text = result["reply"]["reply_text"]
        self.assertIn("相关论文参考：", reply_text)
        self.assertIn("DHA 与儿童成长研究：https://example.com/dha-paper", reply_text)
        self.assertIn(
            {"sheet": "6 论文表", "row": 8, "field": "row", "kb_doc_id": "doc-paper-1"},
            result["reply"]["used_script_sources"],
        )
        self.assertTrue(result["validation"]["ok"])

    def test_pi_worker_passes_images_as_cli_attachments(self) -> None:
        reply = autonomous_reply()
        image_path = Path(self.tmp.name) / "customer.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["input"] = kwargs.get("input")
            captured["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(reply, ensure_ascii=False),
                stderr="",
            )

        with mock.patch.dict(
            "os.environ",
            {
                "CSBOT_AUTONOMOUS_PROVIDER": "pi",
                "CSBOT_PI_COMMAND": "/opt/homebrew/bin/pi",
                "CSBOT_PI_PROVIDER": "uda-openai",
                "CSBOT_PI_TEXT_MODEL": "deepseek-v4-flash",
                "CSBOT_PI_IMAGE_MODEL": "qwen3.6-flash",
            },
            clear=False,
        ):
            with mock.patch("csbot.autonomous_worker.subprocess.run", side_effect=fake_run):
                result = run_autonomous_worker(
                    customer_id="cust-1",
                    query="[图片]",
                    context={
                        "known_facts": {},
                        "messages": [
                            {
                                "role": "用户",
                                "text": "[图片]",
                                "media": [{"type": "image", "capture_ok": True, "capture_path": str(image_path)}],
                            }
                        ],
                    },
                    db_path=self.db,
                    timeout=30,
                )

        self.assertEqual(result["reply"], reply)
        self.assertIsNone(captured["input"])
        self.assertIn("--model", captured["command"])
        self.assertEqual(captured["command"][captured["command"].index("--model") + 1], "qwen3.6-flash")
        self.assertTrue(any(arg.startswith("@") and arg.endswith(".md") for arg in captured["command"]))
        self.assertIn(f"@{image_path}", captured["command"])
        self.assertIn("随附图片是当前客户最新消息的真实图片内容", captured["command"][-1])
        self.assertIn("禁止回复看不到、无法查看、请客户描述图片", captured["command"][-1])
        self.assertEqual(result["codex_cli"]["model"], "qwen3.6-flash")
        self.assertTrue(result["codex_cli"]["supports_images"])

    def test_pi_worker_keeps_text_model_without_images(self) -> None:
        reply = autonomous_reply()
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(reply, ensure_ascii=False),
                stderr="",
            )

        with mock.patch.dict(
            "os.environ",
            {
                "CSBOT_AUTONOMOUS_PROVIDER": "pi",
                "CSBOT_PI_COMMAND": "/opt/homebrew/bin/pi",
                "CSBOT_PI_PROVIDER": "uda-openai",
                "CSBOT_PI_TEXT_MODEL": "deepseek-v4-flash",
                "CSBOT_PI_IMAGE_MODEL": "qwen3.6-flash",
            },
            clear=False,
        ):
            with mock.patch("csbot.autonomous_worker.subprocess.run", side_effect=fake_run):
                result = run_autonomous_worker(
                    customer_id="cust-1",
                    query="鱼油怎么吃",
                    context={"known_facts": {}, "messages": [{"role": "用户", "text": "鱼油怎么吃"}]},
                    db_path=self.db,
                    timeout=30,
                )

        self.assertEqual(result["reply"], reply)
        self.assertIsInstance(captured["input"], str)
        self.assertEqual(captured["command"][captured["command"].index("--model") + 1], "deepseek-v4-flash")
        self.assertFalse(any(str(arg).startswith("@") for arg in captured["command"]))
        self.assertFalse(result["codex_cli"]["supports_images"])

    def test_autonomous_worker_records_local_handoff_action(self) -> None:
        reply = {
            "action": "handoff",
            "reply_text": "您好，这个问题我帮您转人工客服确认处理，请您稍等。",
            "used_script_sources": [],
            "used_vector_memories": [],
            "confidence": 0.9,
            "commands_run": [],
            "conflicts": [],
            "retrieval_summary": "客户明确要求转人工。",
            "decision_basis": "客户投诉并要求人工升级。",
        }

        def fake_run(command, **kwargs):
            out_path = Path(command[command.index("--output-last-message") + 1])
            out_path.write_text(json.dumps(reply, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch("csbot.codex_cli.ensure_model_catalog") as catalog_mock:
            catalog_mock.return_value.enabled = True
            catalog_mock.return_value.path = str(Path(self.tmp.name) / "catalog.json")
            catalog_mock.return_value.generated = False
            catalog_mock.return_value.error = ""
            with mock.patch("csbot.autonomous_worker.subprocess.run", side_effect=fake_run):
                result = run_autonomous_worker(
                    customer_id="cust-1",
                    query="我要投诉，给我转人工",
                    context={"conversation_title": "刘裕鑫"},
                    db_path=self.db,
                    timeout=30,
                )

        self.assertEqual(result["reply"], reply)
        self.assertEqual(result["handoff"]["reason"], "handled_by_wecom_review")
        self.assertFalse(result["handoff"]["notified"])
        self.assertEqual(result["handoff"]["customer_id"], "cust-1")
        self.assertEqual(result["handoff"]["query"], "我要投诉，给我转人工")

    def test_autonomous_worker_timeout_returns_handoff_reply(self) -> None:
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1), output="", stderr="timeout")

        with mock.patch("csbot.codex_cli.ensure_model_catalog") as catalog_mock:
            catalog_mock.return_value.enabled = True
            catalog_mock.return_value.path = str(Path(self.tmp.name) / "catalog.json")
            catalog_mock.return_value.generated = False
            catalog_mock.return_value.error = ""
            with mock.patch("csbot.autonomous_worker.subprocess.run", side_effect=fake_run):
                result = run_autonomous_worker(
                    customer_id="cust-timeout",
                    query="我需要增肌，请问你推荐什么产品？",
                    context={"conversation_title": "墨雨"},
                    db_path=self.db,
                    timeout=1,
                )

        self.assertEqual(result["parse_error"], "codex_timeout")
        self.assertTrue(result["validation"]["ok"])
        self.assertEqual(result["reply"]["action"], "handoff")
        self.assertIn("转人工", result["reply"]["reply_text"])
        self.assertEqual(result["handoff"]["reason"], "handled_by_wecom_review")
        self.assertFalse(result["handoff"]["notified"])

    def test_autonomous_worker_records_local_ai_problem_handoff(self) -> None:
        reply = {
            "action": "handoff",
            "reply_text": "您好，这个问题我帮您转人工客服确认处理，请您稍等。",
            "used_script_sources": [],
            "used_vector_memories": [],
            "confidence": 0.9,
            "commands_run": [],
            "conflicts": [],
            "retrieval_summary": "模型保守判断转人工。",
            "decision_basis": "model_uncertain",
        }

        def fake_run(command, **kwargs):
            out_path = Path(command[command.index("--output-last-message") + 1])
            out_path.write_text(json.dumps(reply, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch("csbot.codex_cli.ensure_model_catalog") as catalog_mock:
            catalog_mock.return_value.enabled = True
            catalog_mock.return_value.path = str(Path(self.tmp.name) / "catalog.json")
            catalog_mock.return_value.generated = False
            catalog_mock.return_value.error = ""
            with mock.patch("csbot.autonomous_worker.subprocess.run", side_effect=fake_run):
                result = run_autonomous_worker(
                    customer_id="cust-1",
                    query="鱼油怎么吃",
                    context={"conversation_title": "刘裕鑫"},
                    db_path=self.db,
                    timeout=30,
                )

        self.assertEqual(result["reply"], reply)
        self.assertEqual(result["handoff"]["reason"], "handled_by_wecom_review")
        self.assertFalse(result["handoff"]["notified"])

    def test_debug_autonomous_does_not_force_fixed_retrieve(self) -> None:
        codex_result = {
            "skipped": False,
            "mode": "autonomous",
            "reply": autonomous_reply(),
            "validation": {"ok": True, "reason": ""},
        }
        with mock.patch("csbot.debug_server.retrieve") as retrieve_mock:
            with mock.patch("csbot.debug_server.run_autonomous_worker", return_value=codex_result) as worker_mock:
                result = run_debug_case(
                    {
                        "mode": "autonomous",
                        "customer_id": "cust-1",
                        "query": "女维怎么吃",
                        "context": {"known_facts": {}},
                        "run_codex": True,
                        "db_path": str(self.db),
                    }
                )

        retrieve_mock.assert_not_called()
        worker_mock.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "autonomous")
        self.assertEqual(result["request"]["mode"], "autonomous")
        self.assertEqual(result["retrieval"]["mode"], "autonomous")
        self.assertEqual(result["retrieval_trace"]["mode"], "autonomous")
        self.assertEqual(result["retrieval_trace"]["answer_basis"], "autonomous_worker")
        self.assertEqual(result["codex"], codex_result)

    def test_autonomous_prompt_stays_compact(self) -> None:
        prompt = build_autonomous_prompt(
            customer_id="cust-1",
            query="女维怎么吃",
            context={"known_facts": {}},
            db_path=self.db,
            mem0_url="http://127.0.0.1:8888",
        )

        self.assertLess(len(prompt), 4500)
        self.assertIn("自定义客服规则：非必要不追问。", prompt)
        self.assertIn("不强制固定第一步", prompt)
        self.assertIn("mem search --query", prompt)
        self.assertIn("csbot ops", prompt)
        self.assertIn("ops handoff", prompt)
        self.assertIn("订单、物流、工单、企微用户信息必须调用 `csbot ops`", prompt)
        self.assertIn("不要阅读项目源码", prompt)
        self.assertIn('"workdir"', prompt)
        self.assertIn(f'"agent_rules_file": "{self.agents_path}"', prompt)
        self.assertIn("-m csbot --db", prompt)
        self.assertIn("kb_docs(kb_doc_id", prompt)
        self.assertIn("微伴 FAQ", prompt)
        self.assertNotIn('"not_null"', prompt)
        self.assertNotIn('"primary_key"', prompt)

    def test_autonomous_prompt_reads_agents_md_from_workdir(self) -> None:
        agents_path = Path(self.tmp.name) / "AGENTS.md"
        agents_path.write_text(
            "自定义客服规则：优先查询微伴 FAQ。\n最终只能输出 JSON。",
            encoding="utf-8",
        )

        prompt = build_autonomous_prompt(
            customer_id="cust-1",
            query="拼团规则",
            context={"known_facts": {}},
            db_path=self.db,
            mem0_url="http://127.0.0.1:8888",
        )

        self.assertIn("自定义客服规则：优先查询微伴 FAQ。", prompt)
        self.assertIn(f'"agent_rules_file": "{agents_path}"', prompt)
        self.assertIn('"latest_message": "拼团规则"', prompt)

    def test_autonomous_prompt_requires_agents_md(self) -> None:
        self.agents_path.unlink()

        with self.assertRaises(FileNotFoundError):
            build_autonomous_prompt(
                customer_id="cust-1",
                query="拼团规则",
                context={"known_facts": {}},
                db_path=self.db,
                mem0_url="http://127.0.0.1:8888",
            )

    def test_codex_reply_schema_keeps_autonomous_extensions_compatible(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "codex_reply.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertTrue(schema["additionalProperties"])
        for field in ("commands_run", "retrieval_summary", "decision_basis"):
            self.assertIn(field, schema["properties"])


if __name__ == "__main__":
    unittest.main()
