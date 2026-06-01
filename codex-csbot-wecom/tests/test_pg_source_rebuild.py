import tempfile
import unittest
from pathlib import Path

from csbot.db import connect, ensure_schema
from csbot.feishu_sync import sync_feishu_tables
from csbot.kb_rebuild import rebuild_kb_docs_from_sources
from csbot.script_search import script_search
from csbot.textutil import json_dumps


class PgSourceRebuildCompatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite"
        conn = connect(self.db)
        try:
            ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO feishu_sync_records
                    (table_id, table_label, pg_table, record_id, fields_json, content_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "tblrwmlB4kEF4dIR",
                    "5 产品常规信息",
                    "feishu_product_basic_info",
                    "rec_fish_oil",
                    json_dumps(
                        {
                            "产品常用名": "鱼油",
                            "产品全称": "鱼油",
                            "别称": "深海鱼油",
                            "起拍数量": "4",
                            "不拼团起拍价格": "276",
                            "拼团起拍价格": "236",
                        }
                    ),
                    "hash-1",
                ),
            )
            conn.execute(
                """
                INSERT INTO weiban_customer_service_faq
                    (weiban_id, group_id, group_name, parent_group_name, weiban_collection_id,
                     content_type, title, summary, body, fuzzy_keywords, exact_keywords,
                     image_url, image_size, local_image_path, file_name, biz_key, risk_level,
                     item_json, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1001,
                    10,
                    "售后FAQ",
                    "",
                    20,
                    "text",
                    "售后FAQ | 退款 | 怎么退款",
                    "退款需要转人工登记。",
                    "退款需要转人工登记。",
                    json_dumps(["退款"]),
                    json_dumps(["怎么退款"]),
                    "",
                    "",
                    "",
                    "refund.md",
                    "weiban://quick_reply/10/1001",
                    "medium",
                    json_dumps({"source": "weiban"}),
                    "hash-2",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rebuild_from_feishu_and_weiban_sources(self) -> None:
        counts = rebuild_kb_docs_from_sources(kb_version="test-pg", db_path=self.db)

        self.assertEqual(counts["product_profile"], 1)
        self.assertEqual(counts["faq"], 1)

        sales = script_search("鱼油起拍数量", self.db)
        self.assertEqual(sales["hits"][0]["facts"]["起拍数量"], "4")
        self.assertEqual(sales["hits"][0]["source"]["sheet"], "5 产品常规信息")
        self.assertEqual(sales["hits"][0]["source"]["field"], "rec_fish_oil")

        faq = script_search("怎么退款", self.db)
        self.assertTrue(any(hit["business_type"] == "faq" for hit in faq["hits"]))

    def test_feishu_sync_writes_named_source_table(self) -> None:
        class FakeClient:
            def list_records(self, table_id):
                return [
                    {
                        "record_id": "rec_1",
                        "fields": {
                            "产品常用名": "鱼油",
                            "产品全称": "鱼油",
                            "起拍数量": "4",
                        },
                    }
                ]

        import csbot.feishu_sync as feishu_sync

        original = feishu_sync.FeishuClient.from_env
        feishu_sync.FeishuClient.from_env = classmethod(lambda cls: FakeClient())
        try:
            result = sync_feishu_tables(table_number=5, db_path=self.db)
        finally:
            feishu_sync.FeishuClient.from_env = original

        self.assertEqual(result["rows"], 1)
        conn = connect(self.db)
        try:
            row = conn.execute("SELECT record_id FROM feishu_product_basic_info").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["record_id"], "rec_1")


if __name__ == "__main__":
    unittest.main()
