from __future__ import annotations

import re
from pathlib import Path

from .db import connect, ensure_schema
from .textutil import json_loads, normalize_text, score_text


USAGE_TERMS = ("怎么吃", "吃法", "服用", "用量", "每日", "每天", "一次", "几粒")
SHIPPING_TERMS = ("发货", "现货", "多久发", "什么时候发", "预售", "到货")
RECOMMEND_TERMS = ("推荐", "适合", "改善", "需要补", "吃什么")
SAFETY_TERMS = ("禁忌", "不能吃", "可以吃吗", "病", "过敏", "孕", "哺乳", "小孩", "儿童", "宝宝")
ACTIVITY_TERMS = ("拼团", "团购", "成团", "优惠券", "秒杀", "活动规则", "活动时间", "限时")
SALES_TERMS = ("起拍", "起订", "起售", "几盒起", "几瓶起", "单盒价", "单瓶价", "价格", "多少钱")
RESEARCH_TERMS = ("论文", "文献", "研究", "证据")
LOGISTICS_EXCEPTION_TERMS = ("异常物流", "清关", "物流异常", "轨迹", "卡住", "停滞")
BRAND_TERMS = ("对标", "品牌", "授权")
CERTIFICATION_TERMS = ("专利", "认证", "原料", "工厂")
PROMOTION_TERMS = ("促单", "活动", "促销")
SUPPLEMENT_SCOPE_BUSINESS_TYPES = ("recommendation_rule", "product_profile", "safety_policy", "research_evidence")
SUPPLEMENT_SCOPE_SOURCE_SHEETS = ("10 补剂推荐", "5 产品常规信息", "7 L0级注意事项", "6 论文表")


def _is_supplement_scope(context: dict | None) -> bool:
    if not context:
        return False
    if str(context.get("scope") or "").strip().lower() in {"supplement", "supplement_recommendation"}:
        return True
    if str(context.get("agent_mode") or "").strip().lower() == "supplement":
        return True
    agent_context = context.get("agent_context")
    return isinstance(agent_context, dict) and str(agent_context.get("reply_source") or "").strip() == "supplement"


def detect_intent(query: str) -> str:
    if any(term in query for term in ACTIVITY_TERMS):
        return "activity_rule"
    if any(term in query for term in SALES_TERMS):
        return "product_sales"
    if any(term in query for term in RESEARCH_TERMS):
        return "research_evidence"
    if any(term in query for term in LOGISTICS_EXCEPTION_TERMS):
        return "logistics_exception"
    if any(term in query for term in BRAND_TERMS):
        return "brand_comparison"
    if any(term in query for term in CERTIFICATION_TERMS):
        return "raw_material_certification"
    if any(term in query for term in PROMOTION_TERMS):
        return "promotion_notice"
    if any(term in query for term in SHIPPING_TERMS):
        return "shipping"
    if any(term in query for term in USAGE_TERMS):
        return "product_usage"
    if any(term in query for term in RECOMMEND_TERMS):
        return "recommendation"
    if any(term in query for term in SAFETY_TERMS):
        return "product_safety"
    return "general"


def _supplement_query_terms(query: str, context: dict | None) -> list[str]:
    terms: list[str] = []
    for value in (query,):
        for token in re.split(r"[\s,，;；、/]+", normalize_text(value)):
            if token and token not in terms:
                terms.append(token)
    if isinstance(context, dict):
        agent_context = context.get("agent_context")
        if isinstance(agent_context, dict):
            for need in agent_context.get("selected_needs") or []:
                text = normalize_text(need)
                if text and text not in terms:
                    terms.append(text)
            known_profile = agent_context.get("known_profile")
            if isinstance(known_profile, dict):
                for value in known_profile.values():
                    text = normalize_text(value)
                    if text and text not in terms:
                        terms.append(text)
    return terms


def _supplement_doc_score(query: str, row, terms: list[str]) -> float:
    score = _doc_score(query, row)
    facts = json_loads(row["facts_json"], {})
    if not isinstance(facts, dict):
        facts = {}
    text = row["text"] or ""
    if row["source_sheet"] in SUPPLEMENT_SCOPE_SOURCE_SHEETS and row["business_type"] != "research_evidence":
        score += 0.2
    if row["business_type"] == "recommendation_rule":
        score += 0.15
    if row["business_type"] == "research_evidence":
        if any(term in query for term in RESEARCH_TERMS):
            score += 0.2
    if row["product"] and row["product"] in query:
        score += 0.35
    for term in terms:
        if not term:
            continue
        if term in str(facts.get("需求点") or ""):
            score += 0.35
        if term in str(facts.get("挖需结果") or ""):
            score += 0.25
        if term in str(facts.get("推荐产品") or "") or term in str(facts.get("产品常用名") or ""):
            score += 0.3
        if row["business_type"] == "research_evidence" and (
            term in str(facts.get("产品") or "")
            or term in str(facts.get("产品全称") or "")
            or term in str(facts.get("标题") or facts.get("论文名称") or "")
            or term in str(facts.get("论文方向") or "")
        ):
            score += 0.2
        if term in text:
            score += 0.05
    if row["business_type"] == "research_evidence" and score > 0 and normalize_text(
        facts.get("链接") or facts.get("论文链接") or facts.get("URL")
    ):
        score += 0.06
    if str(facts.get("是否为兜底推荐产品") or "").strip():
        score += 0.05
    priority = str(facts.get("相同挖需结果下的优先级") or "").strip()
    if priority == "高":
        score += 0.06
    elif priority == "中":
        score += 0.03
    return score


def _supplement_facts(row) -> dict[str, str]:
    facts = json_loads(row["facts_json"], {})
    if not isinstance(facts, dict):
        return {}
    if row["business_type"] == "recommendation_rule":
        keys = [
            "需求点",
            "挖需铺垫",
            "挖需问题",
            "挖需结果",
            "推荐产品",
            "相同挖需结果下的优先级",
            "是否为兜底推荐产品",
            "推荐产品介绍",
            "推荐后免责话术",
            "备注",
            "第一段话术",
        ]
    elif row["business_type"] == "product_profile":
        keys = [
            "产品常用名",
            "产品全称",
            "别称",
            "适用年龄段",
            "服用方法",
            "使用禁忌",
            "产品之间搭配禁忌",
            "主要成分及含量",
            "产品规格",
            "1v1商品链接",
        ]
    elif row["business_type"] == "research_evidence":
        keys = [
            "产品",
            "产品常用名",
            "产品全称",
            "论文方向",
            "论文名称",
            "标题",
            "链接",
            "论文链接",
            "URL",
            "DOI",
            "备注",
        ]
    else:
        keys = ["分类", "问题", "回复话术", "备注"]
    return {key: normalize_text(facts.get(key)) for key in keys if normalize_text(facts.get(key))}


def _script_search_supplement(query: str, db_path: str | Path, context: dict | None = None, *, limit: int = 8) -> dict:
    query = normalize_text(query)
    terms = _supplement_query_terms(query, context)
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        type_placeholders = ",".join("?" for _ in SUPPLEMENT_SCOPE_BUSINESS_TYPES)
        sheet_placeholders = ",".join("?" for _ in SUPPLEMENT_SCOPE_SOURCE_SHEETS)
        rows = conn.execute(
            f"""
            SELECT * FROM kb_docs
            WHERE business_type IN ({type_placeholders})
              AND source_sheet IN ({sheet_placeholders})
            """,
            [*SUPPLEMENT_SCOPE_BUSINESS_TYPES, *SUPPLEMENT_SCOPE_SOURCE_SHEETS],
        ).fetchall()
        scored = sorted(
            ((row, _supplement_doc_score(query, row, terms)) for row in rows),
            key=lambda item: item[1],
            reverse=True,
        )
        hits = []
        for row, score in scored:
            if score <= 0 and terms:
                continue
            hits.append(
                {
                    "type": "supplement_recommendation",
                    "business_type": row["business_type"],
                    "product": row["product"],
                    "topic": row["topic"],
                    "facts": _supplement_facts(row),
                    "source": _source(row),
                    "score": round(float(score), 4),
                }
            )
            if len(hits) >= limit:
                break
    finally:
        conn.close()
    return {
        "intent": "supplement_recommendation",
        "scope": "supplement",
        "hits": hits,
        "needs_clarification": not any(hit["business_type"] == "recommendation_rule" for hit in hits),
        "conflicts": [],
    }


def _business_types_for_intent(intent: str) -> list[str]:
    if intent == "shipping":
        return ["shipping_status"]
    if intent == "recommendation":
        return ["recommendation_rule", "product_profile"]
    if intent in {"product_usage", "product_safety", "product_sales"}:
        return ["product_profile"]
    if intent == "activity_rule":
        return ["activity_rule"]
    if intent in {"research_evidence", "logistics_exception", "brand_comparison", "raw_material_certification", "promotion_notice"}:
        return [intent]
    return [
        "product_profile",
        "shipping_status",
        "recommendation_rule",
        "quality_compliance",
        "activity_rule",
        "promotion_notice",
        "research_evidence",
        "safety_policy",
        "raw_material_certification",
        "shipping_template",
        "logistics_exception",
        "brand_comparison",
        "faq",
    ]


def _known_product_from_context(context: dict | None) -> str:
    if not context:
        return ""
    for key in ("known_product", "current_product", "product"):
        value = normalize_text(context.get(key))
        if value:
            return value
    facts = context.get("known_facts")
    if isinstance(facts, dict):
        return normalize_text(facts.get("product") or facts.get("current_product"))
    return ""


def _rows_for_products(conn, products: set[str], business_types: list[str]) -> list:
    if not products:
        return []
    type_placeholders = ",".join("?" for _ in business_types)
    clauses = []
    params: list[str] = []
    for product in products:
        clauses.append("(product = ? OR text LIKE ?)")
        params.extend([product, f"%{product}%"])
    return conn.execute(
        f"""
        SELECT * FROM kb_docs
        WHERE ({' OR '.join(clauses)}) AND business_type IN ({type_placeholders})
        """,
        [*params, *business_types],
    ).fetchall()


def _alias_matches(conn, query: str, context: dict | None) -> set[str]:
    products: set[str] = set()
    context_product = _known_product_from_context(context)
    if context_product:
        products.add(context_product)
    category_terms = [term for term in ("鱼油", "藻油", "DHA", "EPA") if term in query]
    for row in conn.execute("SELECT alias, product FROM kb_aliases").fetchall():
        alias = row["alias"]
        if alias and alias in query:
            products.add(row["product"])
        elif category_terms and any(term in alias or term in row["product"] for term in category_terms):
            products.add(row["product"])
    return products


def _doc_score(query: str, row) -> float:
    product_bonus = 0.35 if row["product"] and row["product"] in query else 0.0
    return product_bonus + score_text(query, row["text"])


def _doc_score_with_products(query: str, row, products: set[str]) -> float:
    score = _doc_score(query, row)
    text = row["text"] or ""
    if products and any(product and product in text for product in products):
        score += 0.25
    return score


def _source(row) -> dict:
    return {
        "sheet": row["source_sheet"],
        "row": row["source_row"],
        "field": row["source_field"] or "",
        "kb_doc_id": row["kb_doc_id"],
    }


def _facts_for_intent(row, intent: str) -> dict[str, str]:
    facts = json_loads(row["facts_json"], {})
    if not isinstance(facts, dict):
        return {}
    if intent == "shipping":
        keys = ["发货状态", "更新发货时间", "自定义话术内容", "产品常用名", "产品全称"]
    elif intent == "product_usage":
        keys = ["适用年龄段", "服用方法", "使用禁忌", "产品之间搭配禁忌", "产品常用名", "产品全称"]
    elif intent == "product_safety":
        keys = ["适用年龄段", "使用禁忌", "产品之间搭配禁忌", "服用方法", "产品常用名", "产品全称"]
    elif intent == "product_sales":
        keys = [
            "产品常用名",
            "产品全称",
            "不拼团单盒价格",
            "拼团单盒价格",
            "起拍数量",
            "不拼团起拍价格",
            "拼团起拍价格",
            "1v1商品链接",
        ]
    elif intent == "recommendation":
        keys = [
            "需求点",
            "挖需铺垫",
            "挖需问题",
            "挖需结果",
            "推荐产品",
            "相同挖需结果下的优先级",
            "是否为兜底推荐产品",
            "推荐产品介绍",
            "推荐后免责话术",
            "备注",
            "第一段话术",
        ]
    elif intent == "activity_rule":
        keys = ["通知名称", "通知话术", "通知开始时间", "通知结束时间"]
    else:
        keys = list(facts.keys())
    return {key: normalize_text(facts.get(key)) for key in keys if normalize_text(facts.get(key))}


def _has_conflict(hits: list[dict], intent: str, query: str = "") -> bool:
    if intent == "product_sales":
        if any(term in query for term in ("起拍", "起订", "起售", "几盒起", "几瓶起")) and not any(
            term in query for term in ("价格", "多少钱", "单盒价", "单瓶价")
        ):
            quantities = {hit["facts"].get("起拍数量", "") for hit in hits if hit["facts"].get("起拍数量", "")}
            return len(quantities) > 1
        price_signatures = {
            "|".join([hit["facts"].get("不拼团单盒价格", ""), hit["facts"].get("拼团单盒价格", "")])
            for hit in hits
        }
        return len(price_signatures) > 1
    if len({hit["product"] for hit in hits}) > 1:
        return True
    if intent == "shipping":
        status_values = {hit["facts"].get("发货状态", "") + "|" + hit["facts"].get("自定义话术内容", "") for hit in hits}
        return len(status_values) > 1
    return False


def script_search(query: str, db_path: str | Path, context: dict | None = None, *, limit: int = 5) -> dict:
    query = normalize_text(query)
    if _is_supplement_scope(context):
        return _script_search_supplement(query, db_path, context, limit=max(limit, 8))
    intent = detect_intent(query)
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        business_types = _business_types_for_intent(intent)
        products = _alias_matches(conn, query, context)
        rows = _rows_for_products(conn, products, business_types)
        if rows and intent == "product_sales" and products:
            rows = [row for row in rows if row["product"] in products]
        if not rows:
            type_placeholders = ",".join("?" for _ in business_types)
            rows = conn.execute(
                f"SELECT * FROM kb_docs WHERE business_type IN ({type_placeholders})",
                business_types,
            ).fetchall()

        scored = sorted(
            ((row, _doc_score_with_products(query, row, products)) for row in rows),
            key=lambda item: item[1],
            reverse=True,
        )
        hits = []
        for row, score in scored:
            if score <= 0 and not products:
                continue
            hits.append(
                {
                    "type": intent,
                    "business_type": row["business_type"],
                    "product": row["product"],
                    "topic": row["topic"],
                    "facts": _facts_for_intent(row, intent),
                    "source": _source(row),
                    "score": round(float(score), 4),
                }
            )
            if len(hits) >= limit:
                break
    finally:
        conn.close()

    # Keep all exact matched product variants for ambiguous short queries like "鱼油发货时间".
    if products and intent == "shipping":
        by_product: dict[str, dict] = {}
        for hit in hits:
            by_product.setdefault(hit["product"], hit)
        hits = list(by_product.values())

    return {
        "intent": intent,
        "hits": hits,
        "needs_clarification": _has_conflict(hits, intent, query),
        "conflicts": ["multiple_products_or_statuses"] if _has_conflict(hits, intent, query) else [],
    }


def extract_child_age(text: str) -> str:
    match = re.search(r"(\d+(?:\.\d+)?)\s*岁", text)
    if match:
        return f"{match.group(1)}岁"
    return ""
