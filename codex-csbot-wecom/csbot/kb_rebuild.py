from __future__ import annotations

import json

from .db import connect, ensure_schema
from .feishu_sync import FEISHU_TABLES
from .textutil import first_non_empty, json_dumps, json_loads, normalize_text, stable_id


TABLE_LABELS = {pg_table: label for _, label, pg_table in FEISHU_TABLES}


def _split_aliases(*values: str) -> list[str]:
    aliases: list[str] = []
    for value in values:
        for part in normalize_text(value).replace("\n", "；").replace("|", "；").replace("、", "；").split("；"):
            text = normalize_text(part)
            if text and text not in aliases:
                aliases.append(text)
    return aliases


def _insert_doc(conn, *, kb_version: str, business_type: str, product: str, topic: str, facts: dict, source_sheet: str, source_row: int, source_field: str) -> str:
    text_parts = [business_type, product, topic]
    text_parts.extend(f"{key}: {value}" for key, value in facts.items() if normalize_text(value))
    text = "\n".join(part for part in text_parts if part)
    kb_doc_id = stable_id(kb_version, business_type, source_sheet, source_field, product, topic)
    if getattr(conn, "is_pg", False):
        conn.execute(
            """
            INSERT INTO kb_docs
                (kb_doc_id, kb_version, business_type, product, topic, text, facts_json,
                 source_sheet, source_row, source_field)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (kb_doc_id) DO UPDATE SET
                kb_version = EXCLUDED.kb_version,
                business_type = EXCLUDED.business_type,
                product = EXCLUDED.product,
                topic = EXCLUDED.topic,
                text = EXCLUDED.text,
                facts_json = EXCLUDED.facts_json,
                source_sheet = EXCLUDED.source_sheet,
                source_row = EXCLUDED.source_row,
                source_field = EXCLUDED.source_field
            """,
            (kb_doc_id, kb_version, business_type, product, topic, text, json_dumps(facts), source_sheet, source_row, source_field),
        )
    else:
        conn.execute(
            """
            INSERT OR REPLACE INTO kb_docs
                (kb_doc_id, kb_version, business_type, product, topic, text, facts_json,
                 source_sheet, source_row, source_field)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (kb_doc_id, kb_version, business_type, product, topic, text, json_dumps(facts), source_sheet, source_row, source_field),
        )
    return kb_doc_id


def _insert_aliases(conn, aliases: list[str], product: str, kb_doc_id: str) -> None:
    for alias in aliases:
        if getattr(conn, "is_pg", False):
            conn.execute(
                """
                INSERT INTO kb_aliases (alias, product, kb_doc_id, weight)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (alias, product, kb_doc_id) DO NOTHING
                """,
                (alias, product, kb_doc_id, 1.0),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO kb_aliases (alias, product, kb_doc_id, weight) VALUES (?, ?, ?, ?)",
                (alias, product, kb_doc_id, 1.0),
            )


def _feishu_business_type(pg_table: str) -> str:
    return {
        "feishu_product_shipment_status": "shipping_status",
        "feishu_promotion_activities": "promotion_notice",
        "feishu_limited_notices": "activity_rule",
        "feishu_product_test_reports": "quality_compliance",
        "feishu_product_basic_info": "product_profile",
        "feishu_papers": "research_evidence",
        "feishu_l0_precautions": "safety_policy",
        "feishu_logistics_faq": "logistics_exception",
        "feishu_supplement_recommendations": "recommendation_rule",
        "feishu_product_certifications": "raw_material_certification",
        "feishu_shipping_status_templates": "shipping_template",
    }.get(pg_table, "general")


def _first(data: dict, *keys: str) -> str:
    return first_non_empty(data.get(key, "") for key in keys)


def _feishu_product_topic(pg_table: str, data: dict) -> tuple[str, str]:
    if pg_table == "feishu_product_basic_info":
        return _first(data, "产品全称", "产品常用名", "产品"), "产品常规信息"
    if pg_table == "feishu_product_shipment_status":
        return _first(data, "产品全称", "产品常用名", "产品"), "发货状态"
    if pg_table == "feishu_product_test_reports":
        product = _first(data, "产品全称", "产品常用名", "产品")
        batch = _first(data, "批次号", "合同编号")
        return product, f"产品检测报告 {batch}".strip()
    if pg_table == "feishu_supplement_recommendations":
        return _first(data, "推荐产品", "产品"), _first(data, "需求点", "挖需结果", "补剂推荐")
    if pg_table == "feishu_product_certifications":
        return _first(data, "产品全称", "产品常用名", "产品", "原料名称", "原料", "对标品牌", "品牌", "列1"), _first(data, "认证类型", "专利信息", "原料专利认证")
    if pg_table == "feishu_papers":
        return _first(data, "产品全称", "产品常用名", "产品", "论文方向"), _first(data, "论文名称", "标题", "论文方向", "论文表")
    if pg_table == "feishu_logistics_faq":
        return _first(data, "问题类型", "分类", "物流问题表"), _first(data, "问题", "标题", "物流问题")
    if pg_table == "feishu_shipping_status_templates":
        return _first(data, "情况", "发货状态", "发货状态通用话术库"), _first(data, "情况", "发货时间", "发货状态通用话术")
    if pg_table in {"feishu_promotion_activities", "feishu_limited_notices"}:
        return _first(data, "通知名称", "活动名称", "标题"), _first(data, "通知名称", "活动名称", "促销活动")
    return _first(data, "产品全称", "产品常用名", "产品", "标题", "问题", "通知名称") or TABLE_LABELS.get(pg_table, pg_table), _first(data, "标题", "问题", "分类") or TABLE_LABELS.get(pg_table, pg_table)


def _normalized_facts(pg_table: str, data: dict) -> dict[str, str]:
    facts = {key: normalize_text(value) for key, value in data.items() if normalize_text(value)}
    if pg_table == "feishu_product_basic_info":
        facts.update(
            {
                "产品常用名": _first(data, "产品常用名"),
                "产品全称": _first(data, "产品全称", "产品常用名"),
                "别称": _first(data, "别称"),
                "适用年龄段": _first(data, "适用年龄段"),
                "服用方法": _first(data, "服用方法（含时间）", "服用方法_含时间_", "服用方法"),
                "使用禁忌": _first(data, "使用禁忌"),
                "产品之间搭配禁忌": _first(data, "产品之间搭配禁忌"),
                "主要成分及含量": _first(data, "主要成分及含量（每份）", "主要成分及含量_每份_"),
                "产品规格": _first(data, "产品规格"),
                "起拍数量": _first(data, "起拍数量"),
            }
        )
    elif pg_table == "feishu_product_shipment_status":
        facts.update(
            {
                "产品常用名": _first(data, "产品常用名"),
                "产品全称": _first(data, "产品全称", "产品常用名"),
                "发货状态": _first(data, "发货状态"),
                "更新发货时间": _first(data, "更新发货时间", "发货时间"),
                "自定义话术内容": _first(data, "自定义话术内容", "回复话术", "话术"),
            }
        )
    elif pg_table == "feishu_supplement_recommendations":
        facts.update(
            {
                "需求点": _first(data, "需求点"),
                "挖需结果": _first(data, "挖需结果"),
                "推荐产品": _first(data, "推荐产品"),
                "推荐产品介绍": _first(data, "推荐产品介绍"),
                "备注": _first(data, "备注（不发出）", "备注"),
            }
        )
    return facts


def rebuild_kb_docs_from_sources(*, kb_version: str = "pg", db_path=None) -> dict[str, int]:
    conn = connect(db_path)
    counts: dict[str, int] = {}
    try:
        ensure_schema(conn)
        conn.execute("DELETE FROM kb_meta")
        conn.execute("DELETE FROM kb_docs")
        conn.execute("DELETE FROM kb_aliases")

        rows = conn.execute(
            "SELECT * FROM feishu_sync_records ORDER BY pg_table, record_id"
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            data = json_loads(row["fields_json"], {})
            if not isinstance(data, dict):
                continue
            pg_table = row["pg_table"]
            business_type = _feishu_business_type(pg_table)
            product, topic = _feishu_product_topic(pg_table, data)
            facts = _normalized_facts(pg_table, data)
            facts["_source"] = "feishu"
            facts["_feishu_record_id"] = row["record_id"]
            facts["_feishu_table"] = pg_table
            kb_doc_id = _insert_doc(
                conn,
                kb_version=kb_version,
                business_type=business_type,
                product=product,
                topic=topic,
                facts=facts,
                source_sheet=row["table_label"],
                source_row=index,
                source_field=row["record_id"],
            )
            aliases = _split_aliases(product, topic, _first(data, "产品常用名"), _first(data, "产品全称"), _first(data, "别称"), _first(data, "推荐产品"))
            _insert_aliases(conn, aliases, product, kb_doc_id)
            counts[business_type] = counts.get(business_type, 0) + 1

        faq_rows = conn.execute("SELECT * FROM weiban_customer_service_faq ORDER BY weiban_id").fetchall()
        for index, row in enumerate(faq_rows, start=1):
            fuzzy = json_loads(row["fuzzy_keywords"], [])
            exact = json_loads(row["exact_keywords"], [])
            if not isinstance(fuzzy, list):
                fuzzy = []
            if not isinstance(exact, list):
                exact = []
            facts = {
                "标题": row["title"],
                "摘要": row["summary"] or "",
                "正文": row["body"] or "",
                "模糊关键词": "；".join(str(item) for item in fuzzy),
                "精确关键词": "；".join(str(item) for item in exact),
                "分组": row["group_name"] or "",
                "父分组": row["parent_group_name"] or "",
                "_source": "weiban",
                "_weiban_id": str(row["weiban_id"]),
                "_biz_key": row["biz_key"],
            }
            product = row["group_name"] or row["parent_group_name"] or "微伴FAQ"
            topic = row["title"]
            kb_doc_id = _insert_doc(
                conn,
                kb_version=kb_version,
                business_type="faq",
                product=product,
                topic=topic,
                facts=facts,
                source_sheet="微伴FAQ",
                source_row=int(row["weiban_id"]),
                source_field=row["biz_key"],
            )
            _insert_aliases(conn, _split_aliases(product, topic, *[str(item) for item in fuzzy], *[str(item) for item in exact]), product, kb_doc_id)
            counts["faq"] = counts.get("faq", 0) + 1

        if getattr(conn, "is_pg", False):
            conn.execute(
                "INSERT INTO kb_meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                ("current_version", kb_version),
            )
        else:
            conn.execute("INSERT OR REPLACE INTO kb_meta (key, value) VALUES (?, ?)", ("current_version", kb_version))
        conn.commit()
        return counts
    finally:
        conn.close()
