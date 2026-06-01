import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from csbot.cli import main
from csbot.ops_gateway import handoff_notify, logistics_query, order_query, ticket_draft, wecom_user_lookup


class OpsGatewayTest(unittest.TestCase):
    def test_order_query_uses_local_clickhouse_query(self) -> None:
        rows = [
            {"order_id": "E202601010001", "order_status": "已支付", "delivery_status": "已发货", "pay_price": 99}
        ]
        with mock.patch("csbot.ops_gateway._query", side_effect=[rows, [], []]) as query:
            result = order_query(identifier="E202601010001", id_type="order_id")

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "clickhouse")
        self.assertEqual(result["result"]["count"], 1)
        self.assertEqual(result["result"]["orders"][0]["order_id"], "E202601010001")
        self.assertIn("youzan_detail_orders", query.call_args_list[0].args[0])

    def test_logistics_query_can_resolve_external_user_id_locally(self) -> None:
        with mock.patch("csbot.ops_gateway.wecom_user_lookup", return_value={"result": {"buyer_phone": "13800138000"}}):
            with mock.patch("csbot.ops_gateway.order_query") as order:
                order.return_value = {
                    "result": {"orders": [{"order_id": "E202601010001", "delivery_status": "已发货"}]}
                }
                with mock.patch("csbot.ops_gateway.query_logistics_result") as logistics:
                    logistics.return_value = {"order_summary": {"order_id": "E202601010001"}, "tracking": [], "items": []}
                    result = logistics_query(external_user_id="wm-1")

        self.assertTrue(result["ok"])
        order.assert_called_once_with(identifier="13800138000", id_type="phone")
        logistics.assert_called_once_with("E202601010001")

    def test_wecom_user_lookup_uses_channel_customer(self) -> None:
        with mock.patch("csbot.ops_gateway._query", return_value=[{"resolved_phone": "13800138000", "resolved_yz_open_id": ""}]) as query:
            result = wecom_user_lookup(external_user_id="wm-1")

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["buyer_phone"], "13800138000")
        self.assertIn("uda.channel_customer", query.call_args.args[0])

    def test_ticket_draft_is_local(self) -> None:
        result = ticket_draft(
            context={"prepared_messages": [{"isCustomer": True, "content": "我要投诉物流"}]},
            order_id="E202601010001",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "clickhouse")
        self.assertEqual(result["result"]["draft"]["ticket_category"], "售后投诉")

    def test_handoff_defaults_to_dry_run_without_feishu_send(self) -> None:
        with mock.patch.dict("os.environ", {"CSBOT_FEISHU_ENABLED": "1", "CSBOT_FEISHU_WEBHOOK": "http://feishu"}, clear=False):
            with mock.patch("csbot.ops_gateway.request.urlopen") as urlopen:
                result = handoff_notify(
                    customer_id="cust",
                    query="我要投诉",
                    reason="投诉",
                    context={"customer_display_name": "张三"},
                )

        self.assertTrue(result["ok"])
        self.assertFalse(result["notified"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["message_preview"]["customer_name"], "张三")
        urlopen.assert_not_called()

    def test_handoff_feishu_text_includes_customer_name(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return b'{"StatusCode":0}'

        with mock.patch.dict(
            "os.environ",
            {"CSBOT_FEISHU_ENABLED": "1", "CSBOT_FEISHU_WEBHOOK": "http://feishu"},
            clear=False,
        ):
            with mock.patch("csbot.ops_gateway.request.urlopen", return_value=FakeResponse()) as urlopen:
                result = handoff_notify(
                    customer_id="cust",
                    query="我要投诉",
                    reason="投诉",
                    context={"customer_name": "李四"},
                    dry_run=False,
                )

        self.assertTrue(result["ok"])
        self.assertTrue(result["notified"])
        request_obj = urlopen.call_args.args[0]
        body = json.loads(request_obj.data.decode("utf-8"))
        self.assertIn("客户名称：李四", body["content"]["text"])


class OpsCliTest(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> dict:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_ops_order_cli(self) -> None:
        with mock.patch("csbot.cli.order_query", return_value={"ok": True, "result": {"count": 1}}) as fn:
            result = self._run_cli(["ops", "order", "--identifier", "13800138000", "--id-type", "phone"])

        self.assertTrue(result["ok"])
        fn.assert_called_once_with(identifier="13800138000", id_type="phone", context={})

    def test_ops_handoff_cli_is_dry_run_by_default(self) -> None:
        with mock.patch("csbot.cli.handoff_notify", return_value={"ok": True, "notified": False}) as fn:
            result = self._run_cli(
                ["ops", "handoff", "--customer-id", "cust", "--query", "退款", "--reason", "退款诉求"]
            )

        self.assertTrue(result["ok"])
        fn.assert_called_once_with(
            customer_id="cust",
            query="退款",
            reason="退款诉求",
            context={},
            dry_run=True,
        )


if __name__ == "__main__":
    unittest.main()
