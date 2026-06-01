from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib import error, parse, request


PHONE_RE = re.compile(r"(?<!\d)(1\d{10})(?!\d)")
ORDER_ID_RE = re.compile(r"^E20\d{2}[A-Za-z0-9_-]{6,63}$", re.IGNORECASE)
TRACKING_RE = re.compile(r"^[A-Za-z0-9]{8,32}$")


@dataclass(frozen=True)
class ClickHouseConfig:
    host: str = os.environ.get("CH_HOST", "jixusadjiwnnas.uda.cn")
    port: int = int(os.environ.get("CH_PORT", "80"))
    database: str = os.environ.get("CH_DATABASE", "uda")
    user: str = os.environ.get("CH_USER", "query_uda")
    password: str = os.environ.get("CH_PASS", "")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ")
    return str(value)


def _ok(result: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"ok": True, "source": "clickhouse", "result": result, **extra}


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _phone(value: Any) -> str:
    matched = PHONE_RE.search(_safe_text(value))
    return matched.group(1) if matched else ""


def _order_id(value: Any) -> str:
    text = _safe_text(value)
    return text if ORDER_ID_RE.fullmatch(text) else ""


def _tracking_no(value: Any) -> str:
    text = _safe_text(value).replace(" ", "").replace("\t", "").replace("\n", "")
    if not text or _phone(text) == text or _order_id(text):
        return ""
    return text if TRACKING_RE.fullmatch(text) and any(ch.isdigit() for ch in text) else ""


def _quote_sql(value: str) -> str:
    return "'" + _safe_text(value).replace("'", "''") + "'"


def _sql_string_list(values: list[str]) -> str:
    return ",".join(_quote_sql(value) for value in values if _safe_text(value))


def _clickhouse_config() -> ClickHouseConfig:
    return ClickHouseConfig()


def _query(sql: str, *, parameters: dict[str, Any] | None = None, timeout: int = 20) -> list[dict[str, Any]]:
    cfg = _clickhouse_config()
    rendered = sql
    for key, value in (parameters or {}).items():
        rendered = rendered.replace(f"%({key})s", _quote_sql(str(value)))
    body = (rendered.rstrip().rstrip(";") + "\nFORMAT JSONEachRow").encode("utf-8")
    req = request.Request(
        cfg.url + "?" + parse.urlencode({"database": cfg.database}),
        data=body,
        method="POST",
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    credentials = ("%s:%s" % (cfg.user, cfg.password)).encode("utf-8")
    import base64

    req.add_header("Authorization", "Basic " + base64.b64encode(credentials).decode("ascii"))
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"clickhouse_http_{exc.code}: {detail}") from exc
    if not raw.strip():
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def query_order_items(order_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    order_ids_sql = _sql_string_list(list(dict.fromkeys(order_ids)))
    if not order_ids_sql:
        return {}
    rows = _query(
        f"""
        SELECT
            order_id,
            game_name,
            sum(toInt64OrZero(toString(jst_num))) AS quantity,
            any(jst_sku_id) AS sku_id,
            any(goods_url) AS goods_url
        FROM uda.youzan_detail_orders
        WHERE order_id IN ({order_ids_sql})
          AND order_id != ''
          AND game_name != ''
        GROUP BY order_id, game_name
        ORDER BY order_id, game_name
        """
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(_safe_text(row.get("order_id")), []).append(
            {
                "product_name": _safe_text(row.get("game_name")),
                "quantity": int(row.get("quantity") or 0),
                "sku_id": _safe_text(row.get("sku_id")),
                "goods_url": _safe_text(row.get("goods_url")),
            }
        )
    return result


def query_youzan_logistics_summary(order_ids: list[str]) -> dict[str, dict[str, Any]]:
    order_ids_sql = _sql_string_list(list(dict.fromkeys(order_ids)))
    if not order_ids_sql:
        return {}
    rows = _query(
        f"""
        SELECT
            order_id,
            any(logistics_name) AS logistics_name,
            any(logistics_no) AS logistics_no,
            argMax(arrayElement(logistics_detail, 1), parseDateTimeBestEffortOrNull(logistics_detail_time)) AS latest_detail,
            max(logistics_detail_time) AS latest_logistics_detail_time,
            any(pay_time) AS pay_time,
            any(jst_send_time) AS send_time,
            any(status) AS order_status,
            any(jst_status) AS jst_status
        FROM uda.youzan_detail_orders
        WHERE order_id IN ({order_ids_sql})
          AND order_id != ''
          AND NOT empty(logistics_detail)
        GROUP BY order_id
        """
    )
    return {
        _safe_text(row.get("order_id")): {
            "logistics_name": row.get("logistics_name"),
            "logistics_no": row.get("logistics_no"),
            "latest_detail": row.get("latest_detail"),
            "logistics_detail_time": row.get("latest_logistics_detail_time"),
            "pay_time": row.get("pay_time"),
            "send_time": row.get("send_time"),
            "order_status": row.get("order_status"),
            "jst_status": row.get("jst_status"),
        }
        for row in rows
        if _safe_text(row.get("order_id"))
    }


def _attach_order_details(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order_ids = [_safe_text(order.get("order_id")) for order in orders if _safe_text(order.get("order_id"))]
    item_map = query_order_items(order_ids)
    logistics_map = query_youzan_logistics_summary(order_ids)
    for order in orders:
        order_id = _safe_text(order.get("order_id"))
        items = item_map.get(order_id) or []
        if items:
            order["items"] = items
            order["product_names"] = [item["product_name"] for item in items if item.get("product_name")]
        logistics = logistics_map.get(order_id) or {}
        for key in ("logistics_name", "logistics_no", "latest_detail", "logistics_detail_time"):
            if logistics.get(key) and not order.get(key):
                order[key] = logistics[key]
    return orders


def order_query(*, identifier: str, id_type: str = "phone", context: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = _order_id(identifier) if id_type == "order_id" else _phone(identifier)
    if not normalized:
        return _ok(
            {
                "status": "needs_followup",
                "reply_text": "请把订单号或下单手机号发我一下，我帮您查订单状态。",
                "missing_fields": ["order_id", "buyer_phone"],
            }
        )
    where_clause = "order_id = %(id)s" if id_type == "order_id" else "buyer_phone = %(id)s"
    rows = _query(
        f"""
        SELECT *
        FROM (
            SELECT
                order_id,
                any(status) AS order_status,
                multiIf(any(jst_send_time) != '', '已发货', any(status) != '', any(status), any(jst_status)) AS delivery_status,
                any(pay_time) AS pay_time_value,
                any(jst_send_time) AS send_time,
                any(jst_end_time) AS end_time,
                any(nickname) AS nickname,
                any(buyer_phone) AS buyer_phone_value,
                any(receiver_province) AS receiver_province,
                any(receiver_city) AS receiver_city,
                sum(pay_price) AS pay_price,
                sum(refund_price) AS refund_price,
                max(parseDateTimeBestEffortOrNull(toString(pay_time))) AS pay_time_sort,
                max(update_time) AS update_time_sort
            FROM uda.youzan_detail_orders
            WHERE {where_clause} AND order_id != ''
            GROUP BY order_id
        )
        ORDER BY pay_time_sort DESC, update_time_sort DESC
        LIMIT 5
        """,
        parameters={"id": normalized},
    )
    for row in rows:
        if row.get("buyer_phone_value") and not row.get("buyer_phone"):
            row["buyer_phone"] = row.pop("buyer_phone_value")
        if row.get("pay_time_value") and not row.get("pay_time"):
            row["pay_time"] = row.pop("pay_time_value")
    orders = _attach_order_details(rows)
    if not orders:
        return _ok({"status": "not_found", "message": "未找到相关订单", "orders": [], "count": 0})
    return _ok({"status": "success", "orders": orders, "count": len(orders), "reply_text": _order_reply(orders)})


def wecom_user_lookup(*, external_user_id: str) -> dict[str, Any]:
    external_user_id = _safe_text(external_user_id)
    if not external_user_id:
        return _ok({"status": "needs_followup", "missing_fields": ["external_user_id"], "reply_text": "缺少企微用户ID。"})
    rows = _query(
        """
        SELECT
            external_user_id,
            anyIf(phone_number, length(phone_number) > 0) AS resolved_phone,
            anyIf(yz_open_id, length(yz_open_id) > 0) AS resolved_yz_open_id
        FROM uda.channel_customer
        WHERE external_user_id = %(external_user_id)s
          AND external_user_id != ''
        GROUP BY external_user_id
        LIMIT 1
        """,
        parameters={"external_user_id": external_user_id},
    )
    phone = _phone(rows[0].get("resolved_phone")) if rows else ""
    yz_open_id = _safe_text(rows[0].get("resolved_yz_open_id")) if rows else ""
    if not phone and yz_open_id:
        fallback = _query(
            """
            SELECT buyer_phone AS resolved_phone
            FROM uda.youzan_detail_orders
            WHERE yz_open_id = %(yz_open_id)s
              AND length(buyer_phone) > 0
            ORDER BY parseDateTimeBestEffortOrNull(toString(pay_time)) DESC, update_time DESC
            LIMIT 1
            """,
            parameters={"yz_open_id": yz_open_id},
        )
        phone = _phone(fallback[0].get("resolved_phone")) if fallback else ""
    return _ok(
        {
            "status": "success" if phone else "not_found",
            "external_user_id": external_user_id,
            "buyer_phone": phone,
            "found": bool(phone),
        }
    )


def logistics_query(
    *,
    order_id: str = "",
    tracking_no: str = "",
    buyer_phone: str = "",
    external_user_id: str = "",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_order_id = _order_id(order_id)
    resolved_tracking_no = _tracking_no(tracking_no)
    resolved_phone = _phone(buyer_phone)
    if not resolved_phone and external_user_id:
        resolved_phone = _safe_text(wecom_user_lookup(external_user_id=external_user_id)["result"].get("buyer_phone"))
    latest_order: dict[str, Any] = {}
    order_result: dict[str, Any] = {}
    if not resolved_order_id and resolved_phone:
        order_result = order_query(identifier=resolved_phone, id_type="phone").get("result") or {}
        orders = order_result.get("orders") or []
        if orders:
            latest_order = orders[0]
            resolved_order_id = _safe_text(latest_order.get("order_id"))
    if not (resolved_order_id or resolved_tracking_no):
        return _ok(
            {
                "status": "needs_followup",
                "reply_text": "请把订单号、运单号或下单手机号发我一下，我帮您查物流。",
                "missing_fields": ["buyer_phone", "order_id", "tracking_no"],
                "order_query": order_result,
            }
        )
    result = (
        query_logistics_result_by_tracking(resolved_tracking_no)
        if resolved_tracking_no and not resolved_order_id
        else query_logistics_result(resolved_order_id)
    )
    if latest_order and not result.get("order_summary"):
        result["order_summary"] = latest_order
    result["reply_text"] = _logistics_reply(result, resolved_order_id or resolved_tracking_no)
    result["status"] = "success" if result.get("tracking") or result.get("order_summary") else "not_found"
    return _ok(result)


def query_logistics_result_by_tracking(tracking_no: str) -> dict[str, Any]:
    tracking_no = _tracking_no(tracking_no)
    if not tracking_no:
        return {"order_summary": {}, "items": [], "tracking": []}
    rows = _query(
        """
        SELECT orderCode, billCode, transferCode, logisticsName, state, describeStr, deliveryTime, update_time
        FROM uda.bdt_orders
        WHERE billCode = %(tracking_no)s OR transferCode = %(tracking_no)s
        ORDER BY update_time DESC
        LIMIT 1
        """,
        parameters={"tracking_no": tracking_no},
    )
    if rows and _safe_text(rows[0].get("orderCode")):
        return query_logistics_result(_safe_text(rows[0].get("orderCode")))
    tracking = _query(
        """
        SELECT billCode, transferCode, scanType, scanDate, describeStr, location
        FROM uda.bdt_order_logistics
        WHERE billCode = %(tracking_no)s OR transferCode = %(tracking_no)s
        ORDER BY scanDate DESC
        LIMIT 20
        """,
        parameters={"tracking_no": tracking_no},
    )
    return {"order_summary": {"tracking_no": tracking_no}, "items": [], "tracking": tracking}


def query_logistics_result(order_id: str) -> dict[str, Any]:
    order_id = _safe_text(order_id)
    if not order_id:
        return {"order_summary": {}, "items": [], "tracking": []}
    rows = _query(
        """
        SELECT orderCode, billCode, transferCode, logisticsName, state, describeStr, deliveryTime, update_time
        FROM uda.bdt_orders
        WHERE orderCode = %(oid)s
        ORDER BY update_time DESC
        LIMIT 1
        """,
        parameters={"oid": order_id},
    )
    latest = rows[0] if rows else {}
    order_summary = {"order_id": order_id, **latest}
    detail = query_youzan_logistics_summary([order_id]).get(order_id) or {}
    for key in ("logistics_name", "logistics_no", "latest_detail", "logistics_detail_time", "pay_time", "send_time", "order_status", "jst_status"):
        if detail.get(key) and not order_summary.get(key):
            order_summary[key] = detail[key]
    items = query_order_items([order_id]).get(order_id, [])
    tracking: list[dict[str, Any]] = []
    bill_code = _safe_text(latest.get("billCode"))
    transfer_code = _safe_text(latest.get("transferCode"))
    if bill_code or transfer_code:
        filters: list[str] = []
        params: dict[str, Any] = {}
        if bill_code:
            filters.append("billCode = %(bill_code)s")
            params["bill_code"] = bill_code
        if transfer_code:
            filters.append("transferCode = %(transfer_code)s")
            params["transfer_code"] = transfer_code
        tracking = _query(
            f"""
            SELECT billCode, transferCode, scanType, scanDate, describeStr, location
            FROM uda.bdt_order_logistics
            WHERE {' OR '.join(filters)}
            ORDER BY scanDate DESC
            LIMIT 20
            """,
            parameters=params,
        )
    if not tracking and detail.get("latest_detail"):
        tracking = [
            {
                "billCode": detail.get("logistics_no"),
                "transferCode": detail.get("logistics_no"),
                "scanType": "物流更新",
                "scanDate": detail.get("logistics_detail_time"),
                "describeStr": detail.get("latest_detail"),
                "expressName": detail.get("logistics_name"),
                "source": "youzan_detail_orders",
            }
        ]
    return {"order_summary": order_summary, "items": items, "tracking": tracking}


def ticket_draft(
    *,
    context: dict[str, Any] | None = None,
    order_id: str = "",
    latest_logistics_time: str = "",
) -> dict[str, Any]:
    context = context or {}
    messages = context.get("prepared_messages") or []
    latest_customer = ""
    for message in reversed(messages):
        if message.get("isCustomer") or message.get("is_customer"):
            latest_customer = _safe_text(message.get("content"))
            break
    category = "售后投诉" if any(word in latest_customer for word in ("投诉", "退款", "退货")) else "人工协助"
    draft = {
        "ticket_category": category,
        "demand_description": latest_customer or _safe_text(context.get("latest_message")) or "待补充客户诉求",
        "order_number": _order_id(order_id) or _safe_text((context.get("order_context") or {}).get("order_id")),
        "latest_logistics_time": latest_logistics_time,
        "customer_display_name": _safe_text(context.get("customer_display_name")),
        "external_user_id": _safe_text(context.get("external_user_id")),
    }
    return _ok({"status": "success", "reply_text": f"已生成工单草稿：{category}。{draft['demand_description']}", "draft": draft})


def handoff_notify(
    *,
    customer_id: str,
    query: str,
    reason: str,
    context: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    context = context or {}
    preview = {
        "title": "客服机器人转人工",
        "customer_id": customer_id,
        "customer_name": _customer_name(context),
        "query": query,
        "reason": reason,
        "context": context,
    }
    enabled = os.environ.get("CSBOT_FEISHU_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    webhook = os.environ.get("CSBOT_FEISHU_WEBHOOK", "").strip()
    if dry_run or not enabled or not webhook:
        return {
            "ok": True,
            "notified": False,
            "dry_run": dry_run,
            "enabled": enabled,
            "reason": "dry_run_or_feishu_disabled",
            "message_preview": preview,
        }
    body = json.dumps({"msg_type": "text", "content": {"text": _handoff_text(preview)}}, ensure_ascii=False).encode()
    req = request.Request(webhook, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=10) as resp:
            response_text = resp.read().decode("utf-8", errors="replace")
        return {
            "ok": True,
            "notified": True,
            "dry_run": False,
            "status": getattr(resp, "status", 200),
            "message_preview": preview,
            "response": response_text[-2000:],
        }
    except Exception as exc:
        return {"ok": False, "notified": False, "dry_run": False, "error": str(exc), "message_preview": preview}


def _order_reply(orders: list[dict[str, Any]]) -> str:
    first = orders[0]
    return (
        f"查到最近订单 {first.get('order_id')}，订单状态：{first.get('order_status') or '暂无'}，"
        f"发货状态：{first.get('delivery_status') or '暂无'}。"
    )


def _logistics_reply(result: dict[str, Any], identifier: str) -> str:
    tracking = result.get("tracking") or []
    summary = result.get("order_summary") or {}
    if tracking:
        latest = tracking[0]
        return (
            f"查到 {identifier} 的最新物流："
            f"{latest.get('scanDate') or ''} {latest.get('describeStr') or latest.get('scanType') or ''}"
        ).strip()
    if summary:
        return f"查到订单 {summary.get('order_id') or identifier}，暂时没有最新物流轨迹。"
    return "暂时没有查到物流信息，请确认订单号、运单号或下单手机号是否正确。"


def _handoff_text(preview: dict[str, Any]) -> str:
    return (
        "客服机器人转人工\n"
        f"客户ID：{preview.get('customer_id') or ''}\n"
        f"客户名称：{preview.get('customer_name') or ''}\n"
        f"原因：{preview.get('reason') or ''}\n"
        f"客户消息：{preview.get('query') or ''}"
    )


def _customer_name(context: dict[str, Any]) -> str:
    for key in ("customer_display_name", "customer_name", "name", "nickname", "conversation_title"):
        value = _safe_text(context.get(key))
        if value:
            return value
    customer = context.get("customer") if isinstance(context.get("customer"), dict) else {}
    for key in ("display_name", "name", "nickname"):
        value = _safe_text(customer.get(key))
        if value:
            return value
    return ""
