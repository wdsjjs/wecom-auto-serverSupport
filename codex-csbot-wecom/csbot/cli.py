from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .codex_cli import codex_catalog_config_args, ensure_isolated_codex_home, resolve_codex_command
from .config import DEFAULT_DB_PATH, DEFAULT_KB_XLSX, resolve_codex_workdir, resolve_db_path, resolve_pg_dsn, using_pg
from .db import connect, ensure_schema
from .feishu_sync import sync_feishu_tables
from .kb_import import import_workbook
from .kb_rebuild import rebuild_kb_docs_from_sources
from .knowledge_sync import DEFAULT_MAX_AGE_SECONDS, knowledge_sync_status, sync_all_parallel, sync_if_stale
from .mem0_client import Mem0Client
from .ops_gateway import handoff_notify, logistics_query, order_query, ticket_draft, wecom_user_lookup
from .retrieve import retrieve
from .script_search import script_search
from .vector_store import add_memory, import_kb_docs_as_memories, search_memories
from .weiban_sync import sync_weiban_faq
from .worker_router import claim_worker, complete_worker, heartbeat_worker, list_workers, register_worker


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _compact_sync_result(value: dict) -> dict:
    def compact_status(status: dict) -> dict:
        return {
            key: status.get(key)
            for key in ("ok", "fresh", "reason", "last_success_at", "age_seconds", "max_age_seconds")
            if key in status
        }

    def compact_task(task: dict) -> dict:
        result = task.get("result") or {}
        compacted = {
            key: task.get(key)
            for key in ("ok", "source", "attempts", "elapsed_ms", "error")
            if key in task
        }
        if isinstance(result, dict):
            compacted["rows"] = result.get("rows")
            compacted["groups"] = result.get("groups")
            compacted["skipped_groups"] = len(result.get("skipped_groups") or []) if "skipped_groups" in result else None
        return compacted

    compacted = {
        key: value.get(key)
        for key in ("ok", "skipped", "reason")
        if key in value
    }
    if isinstance(value.get("status"), dict):
        compacted["status"] = compact_status(value["status"])
    sync = value.get("sync")
    if isinstance(sync, dict):
        compacted["sync"] = {
            key: sync.get(key)
            for key in ("ok", "dry_run", "parallel", "knowledge_backend", "mem0_skipped", "elapsed_ms")
            if key in sync
        }
        compacted["sync"]["sources"] = {
            source: compact_task(task)
            for source, task in (sync.get("sources") or {}).items()
            if isinstance(task, dict)
        }
        compacted["sync"]["followups"] = {
            source: compact_task(task)
            for source, task in (sync.get("followups") or {}).items()
            if isinstance(task, dict)
        }
    return compacted


def _load_context(raw: str | None) -> dict:
    if not raw:
        return {}
    return json.loads(raw)


def _db_arg(args: argparse.Namespace):
    return resolve_db_path(args.db)


def _mask_dsn(dsn: str) -> str:
    if not dsn:
        return ""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "<set>"
    if not parts.scheme or not parts.netloc:
        return "<set>"
    user_host = parts.netloc
    if "@" in user_host:
        userinfo, hostinfo = user_host.rsplit("@", 1)
        username = userinfo.split(":", 1)[0]
        user_host = f"{username}:<redacted>@{hostinfo}" if username else f"<redacted>@{hostinfo}"
    return urlunsplit((parts.scheme, user_host, parts.path, parts.query, parts.fragment))


def cmd_doctor(args: argparse.Namespace) -> int:
    db_path = _db_arg(args)
    codex_command = resolve_codex_command()
    _, catalog_status = codex_catalog_config_args(codex_command)
    isolated_home = ensure_isolated_codex_home()
    pg_dsn = resolve_pg_dsn()
    checks = {
        "ok": True,
        "knowledge_backend": "postgres" if pg_dsn else "sqlite",
        "pg_enabled": bool(pg_dsn),
        "pg_dsn": _mask_dsn(pg_dsn),
        "sqlite_compat_db_path": str(db_path),
        "kb_xlsx": str(Path(args.xlsx or DEFAULT_KB_XLSX).expanduser()),
        "kb_xlsx_exists": Path(args.xlsx or DEFAULT_KB_XLSX).expanduser().exists(),
        "mem0_url": os.environ.get("CSBOT_MEM0_URL", ""),
        "mem0_api_key": "<set>" if os.environ.get("CSBOT_MEM0_API_KEY") else "<unset>",
        "feishu_config": {
            "app_id": "<set>" if os.environ.get("FEISHU_APP_ID") else "<unset>",
            "app_secret": "<set>" if os.environ.get("FEISHU_APP_SECRET") else "<unset>",
            "app_token": "<set>" if os.environ.get("FEISHU_APP_TOKEN") else "<unset>",
        },
        "weiban_config": {
            "base_url": os.environ.get("WEIBAN_BASE_URL", ""),
            "corp_id": "<set>" if os.environ.get("WEIBAN_CORP_ID") else "<unset>",
            "secret": "<set>" if os.environ.get("WEIBAN_SECRET") else "<unset>",
        },
        "codex_command": codex_command,
        "codex_workdir": str(resolve_codex_workdir()),
        "codex_model": os.environ.get("CSBOT_CODEX_MODEL", ""),
        "codex_reasoning_effort": os.environ.get("CSBOT_CODEX_REASONING_EFFORT", "low"),
        "codex_model_catalog": {
            "enabled": catalog_status.enabled,
            "path": catalog_status.path,
            "generated": catalog_status.generated,
            "error": catalog_status.error,
        },
        "codex_isolated_home": {
            "enabled": isolated_home.enabled,
            "path": isolated_home.path,
            "generated": isolated_home.generated,
            "error": isolated_home.error,
        },
        "embedding_dims": 1024,
    }
    _print_json(checks)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    db_path = _db_arg(args)
    counts = import_workbook(args.xlsx, db_path, kb_version=args.kb_version)
    vector_count = import_kb_docs_as_memories(db_path, progress=args.progress) if args.vector else 0
    _print_json(
        {
            "ok": True,
            "knowledge_backend": "postgres" if using_pg() else "sqlite",
            "sqlite_compat_db_path": str(db_path),
            "counts": counts,
            "vector_memories": vector_count,
        }
    )
    return 0


def cmd_feishu_sync(args: argparse.Namespace) -> int:
    result = sync_feishu_tables(dry_run=args.dry_run, table_number=args.table, db_path=_db_arg(args))
    _print_json(result)
    return 0


def cmd_weiban_sync(args: argparse.Namespace) -> int:
    result = sync_weiban_faq(dry_run=args.dry_run, db_path=_db_arg(args))
    _print_json(result)
    return 0


def cmd_kb_rebuild(args: argparse.Namespace) -> int:
    db_path = _db_arg(args)
    counts = rebuild_kb_docs_from_sources(kb_version=args.kb_version, db_path=db_path)
    vector_count = import_kb_docs_as_memories(db_path, progress=args.progress) if args.vector else 0
    _print_json({"ok": True, "kb_version": args.kb_version, "counts": counts, "vector_memories": vector_count})
    return 0


def cmd_sync_all(args: argparse.Namespace) -> int:
    result = sync_all_parallel(
        dry_run=args.dry_run,
        db_path=_db_arg(args),
        kb_version=args.kb_version,
        skip_mem0=args.skip_mem0,
        progress=args.progress,
        attempts=args.attempts,
        retry_delay_seconds=args.retry_delay,
    )
    _print_json(result)
    return 0 if result.get("ok") else 1


def cmd_sync_status(args: argparse.Namespace) -> int:
    result = knowledge_sync_status(db_path=_db_arg(args), max_age_seconds=args.max_age_seconds)
    ok = bool(result.get("ok"))
    if args.compact:
        _print_json(_compact_sync_result({"status": result}))
    else:
        _print_json(result)
    return 0 if ok else 1


def cmd_sync_if_stale(args: argparse.Namespace) -> int:
    result = sync_if_stale(
        db_path=_db_arg(args),
        max_age_seconds=args.max_age_seconds,
        kb_version=args.kb_version,
        skip_mem0=args.skip_mem0,
        progress=args.progress,
        dry_run=args.dry_run,
        force=args.force,
    )
    _print_json(_compact_sync_result(result) if args.compact else result)
    if result.get("ok"):
        return 0
    return 0 if args.non_blocking else 1


def cmd_search(args: argparse.Namespace) -> int:
    result = script_search(args.query, _db_arg(args), _load_context(args.context_json))
    _print_json(result)
    return 0


def cmd_retrieve(args: argparse.Namespace) -> int:
    result = retrieve(
        customer_id=args.customer_id,
        query=args.query,
        db_path=_db_arg(args),
        context=_load_context(args.context_json),
    )
    _print_json(result)
    return 0


def cmd_autonomous_reply(args: argparse.Namespace) -> int:
    from .autonomous_worker import run_autonomous_worker

    result = run_autonomous_worker(
        customer_id=args.customer_id,
        query=args.query,
        context=_load_context(args.context_json),
        db_path=_db_arg(args),
        timeout=args.timeout,
    )
    _print_json({"ok": True, "mode": "autonomous", "codex": result})
    return 0


def cmd_mem_add(args: argparse.Namespace) -> int:
    metadata = _load_context(args.metadata_json)
    memory_id = add_memory(_db_arg(args), customer_id=args.customer_id, text=args.text, metadata=metadata)
    _print_json({"ok": True, "id": memory_id})
    return 0


def cmd_mem_search(args: argparse.Namespace) -> int:
    hits = search_memories(_db_arg(args), customer_id=args.customer_id or "", query=args.query)
    _print_json({"ok": True, "hits": hits})
    return 0


def _require_mem0_client() -> Mem0Client:
    client = Mem0Client.from_env()
    if client is None:
        raise SystemExit("CSBOT_MEM0_URL is not set")
    return client


def cmd_mem_configure(args: argparse.Namespace) -> int:
    _print_json(_require_mem0_client().configure())
    return 0


def cmd_mem_list(args: argparse.Namespace) -> int:
    _print_json({"ok": True, "hits": _require_mem0_client().list_memories(customer_id=args.customer_id)})
    return 0


def cmd_mem_delete(args: argparse.Namespace) -> int:
    _print_json({"ok": True, "result": _require_mem0_client().delete_memory(args.memory_id)})
    return 0


def cmd_ops_order(args: argparse.Namespace) -> int:
    _print_json(
        order_query(
            identifier=args.identifier,
            id_type=args.id_type,
            context=_load_context(args.context_json),
        )
    )
    return 0


def cmd_ops_logistics(args: argparse.Namespace) -> int:
    _print_json(
        logistics_query(
            order_id=args.order_id or "",
            tracking_no=args.tracking_no or "",
            buyer_phone=args.buyer_phone or "",
            external_user_id=args.external_user_id or "",
            context=_load_context(args.context_json),
        )
    )
    return 0


def cmd_ops_wecom_user(args: argparse.Namespace) -> int:
    _print_json(wecom_user_lookup(external_user_id=args.external_user_id))
    return 0


def cmd_ops_ticket_draft(args: argparse.Namespace) -> int:
    _print_json(
        ticket_draft(
            context=_load_context(args.context_json),
            order_id=args.order_id or "",
            latest_logistics_time=args.latest_logistics_time or "",
        )
    )
    return 0


def cmd_ops_handoff(args: argparse.Namespace) -> int:
    _print_json(
        handoff_notify(
            customer_id=args.customer_id,
            query=args.query,
            reason=args.reason,
            context=_load_context(args.context_json),
            dry_run=args.dry_run,
        )
    )
    return 0


def cmd_queue_list(args: argparse.Namespace) -> int:
    db_path = _db_arg(args)
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        rows = conn.execute("SELECT * FROM reply_audit ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()
    finally:
        conn.close()
    _print_json({"ok": True, "items": [dict(row) for row in rows]})
    return 0


def cmd_worker_register(args: argparse.Namespace) -> int:
    _print_json({"ok": True, "worker": register_worker(_db_arg(args), args.worker_id)})
    return 0


def cmd_worker_heartbeat(args: argparse.Namespace) -> int:
    worker = heartbeat_worker(
        _db_arg(args),
        args.worker_id,
        status=args.status,
        job_id=args.job_id,
        lease_timeout=args.lease_timeout,
    )
    _print_json({"ok": True, "worker": worker})
    return 0


def cmd_worker_claim(args: argparse.Namespace) -> int:
    worker = claim_worker(_db_arg(args), job_id=args.job_id, lease_timeout=args.lease_timeout)
    _print_json({"ok": worker is not None, "worker": worker})
    return 0 if worker is not None else 2


def cmd_worker_complete(args: argparse.Namespace) -> int:
    ok = complete_worker(_db_arg(args), args.worker_id, job_id=args.job_id)
    _print_json({"ok": ok})
    return 0 if ok else 2


def cmd_worker_list(args: argparse.Namespace) -> int:
    _print_json({"ok": True, "workers": list_workers(_db_arg(args))})
    return 0


def cmd_debug_ui(args: argparse.Namespace) -> int:
    from .debug_server import serve

    serve(args.host, args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="csbot")
    parser.add_argument("--db", default=None, help=f"SQLite compatibility path when CSBOT_PG_DSN is not set, default {DEFAULT_DB_PATH}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--xlsx", default=str(DEFAULT_KB_XLSX))
    doctor.set_defaults(func=cmd_doctor)

    kb = sub.add_parser("kb")
    kb_sub = kb.add_subparsers(dest="kb_command", required=True)
    kb_import = kb_sub.add_parser("import")
    kb_import.add_argument("--xlsx", required=True)
    kb_import.add_argument("--kb-version", default="v1")
    kb_import.add_argument("--vector", action="store_true", help="Also import KB docs into MEM0/local vector memory")
    kb_import.add_argument("--progress", action="store_true", help="Print vector import progress to stderr")
    kb_import.set_defaults(func=cmd_import)
    kb_rebuild = kb_sub.add_parser("rebuild")
    kb_rebuild.add_argument("--kb-version", default="pg")
    kb_rebuild.add_argument("--vector", action="store_true", help="Also import rebuilt KB docs into MEM0/local vector memory")
    kb_rebuild.add_argument("--progress", action="store_true", help="Print vector import progress to stderr")
    kb_rebuild.set_defaults(func=cmd_kb_rebuild)
    kb_search = kb_sub.add_parser("search")
    kb_search.add_argument("--query", required=True)
    kb_search.add_argument("--context-json")
    kb_search.set_defaults(func=cmd_search)

    retrieve_cmd = sub.add_parser("retrieve")
    retrieve_cmd.add_argument("--customer-id", required=True)
    retrieve_cmd.add_argument("--query", required=True)
    retrieve_cmd.add_argument("--context-json")
    retrieve_cmd.set_defaults(func=cmd_retrieve)

    feishu = sub.add_parser("feishu")
    feishu_sub = feishu.add_subparsers(dest="feishu_command", required=True)
    feishu_sync = feishu_sub.add_parser("sync")
    feishu_sync.add_argument("--dry-run", action="store_true")
    feishu_sync.add_argument("--table", type=int, help="1-based table number from the built-in Feishu AI KB table map")
    feishu_sync.set_defaults(func=cmd_feishu_sync)

    weiban = sub.add_parser("weiban")
    weiban_sub = weiban.add_subparsers(dest="weiban_command", required=True)
    weiban_sync = weiban_sub.add_parser("sync")
    weiban_sync.add_argument("--dry-run", action="store_true")
    weiban_sync.set_defaults(func=cmd_weiban_sync)

    sync = sub.add_parser("sync")
    sync_sub = sync.add_subparsers(dest="sync_command", required=True)
    sync_all = sync_sub.add_parser("all")
    sync_all.add_argument("--dry-run", action="store_true")
    sync_all.add_argument("--kb-version", default="pg")
    sync_all.add_argument("--skip-mem0", action="store_true", help="Only rebuild PG kb_docs/kb_aliases, do not import MEM0")
    sync_all.add_argument("--progress", action="store_true", help="Print MEM0/vector import progress to stderr")
    sync_all.add_argument("--attempts", type=int, default=None, help="Retry attempts for each sync task, default CSBOT_SYNC_RETRY_ATTEMPTS or 3")
    sync_all.add_argument("--retry-delay", type=float, default=None, help="Seconds between retries, default CSBOT_SYNC_RETRY_DELAY_SECONDS or 2")
    sync_all.set_defaults(func=cmd_sync_all)
    sync_status = sync_sub.add_parser("status")
    sync_status.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    sync_status.add_argument("--compact", action="store_true")
    sync_status.set_defaults(func=cmd_sync_status)
    sync_if_stale_cmd = sync_sub.add_parser("if-stale")
    sync_if_stale_cmd.add_argument("--dry-run", action="store_true")
    sync_if_stale_cmd.add_argument("--force", action="store_true")
    sync_if_stale_cmd.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    sync_if_stale_cmd.add_argument("--kb-version", default="startup")
    sync_if_stale_cmd.add_argument("--skip-mem0", action="store_true", help="Only rebuild PG kb_docs/kb_aliases, do not import MEM0")
    sync_if_stale_cmd.add_argument("--progress", action="store_true", help="Print MEM0/vector import progress to stderr")
    sync_if_stale_cmd.add_argument("--non-blocking", action=argparse.BooleanOptionalAction, default=True, help="Return 0 even when stale sync fails; failures are logged")
    sync_if_stale_cmd.add_argument("--compact", action="store_true", help="Print compact startup-friendly JSON")
    sync_if_stale_cmd.set_defaults(func=cmd_sync_if_stale)

    autonomous = sub.add_parser("autonomous-reply")
    autonomous.add_argument("--customer-id", required=True)
    autonomous.add_argument("--query", required=True)
    autonomous.add_argument("--context-json")
    autonomous.add_argument("--timeout", type=int, default=180)
    autonomous.set_defaults(func=cmd_autonomous_reply)

    mem = sub.add_parser("mem")
    mem_sub = mem.add_subparsers(dest="mem_command", required=True)
    mem_add = mem_sub.add_parser("add")
    mem_add.add_argument("--customer-id", required=True)
    mem_add.add_argument("--text", required=True)
    mem_add.add_argument("--metadata-json")
    mem_add.set_defaults(func=cmd_mem_add)
    mem_search = mem_sub.add_parser("search")
    mem_search.add_argument("--customer-id", default="")
    mem_search.add_argument("--query", required=True)
    mem_search.set_defaults(func=cmd_mem_search)
    mem_configure = mem_sub.add_parser("configure")
    mem_configure.set_defaults(func=cmd_mem_configure)
    mem_list = mem_sub.add_parser("list")
    mem_list.add_argument("--customer-id", required=True)
    mem_list.set_defaults(func=cmd_mem_list)
    mem_delete = mem_sub.add_parser("delete")
    mem_delete.add_argument("--memory-id", required=True)
    mem_delete.set_defaults(func=cmd_mem_delete)

    ops = sub.add_parser("ops")
    ops_sub = ops.add_subparsers(dest="ops_command", required=True)
    ops_order = ops_sub.add_parser("order")
    ops_order.add_argument("--identifier", required=True)
    ops_order.add_argument("--id-type", default="phone", choices=["phone", "order_id"])
    ops_order.add_argument("--context-json")
    ops_order.set_defaults(func=cmd_ops_order)
    ops_logistics = ops_sub.add_parser("logistics")
    ops_logistics.add_argument("--order-id")
    ops_logistics.add_argument("--tracking-no")
    ops_logistics.add_argument("--buyer-phone")
    ops_logistics.add_argument("--external-user-id")
    ops_logistics.add_argument("--context-json")
    ops_logistics.set_defaults(func=cmd_ops_logistics)
    ops_wecom_user = ops_sub.add_parser("wecom-user")
    ops_wecom_user.add_argument("--external-user-id", required=True)
    ops_wecom_user.set_defaults(func=cmd_ops_wecom_user)
    ops_ticket = ops_sub.add_parser("ticket-draft")
    ops_ticket.add_argument("--context-json")
    ops_ticket.add_argument("--order-id")
    ops_ticket.add_argument("--latest-logistics-time")
    ops_ticket.set_defaults(func=cmd_ops_ticket_draft)
    ops_handoff = ops_sub.add_parser("handoff")
    ops_handoff.add_argument("--customer-id", required=True)
    ops_handoff.add_argument("--query", required=True)
    ops_handoff.add_argument("--reason", required=True)
    ops_handoff.add_argument("--context-json")
    ops_handoff.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    ops_handoff.set_defaults(func=cmd_ops_handoff)

    queue = sub.add_parser("queue")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_list = queue_sub.add_parser("list")
    queue_list.add_argument("--limit", type=int, default=20)
    queue_list.set_defaults(func=cmd_queue_list)

    worker = sub.add_parser("worker")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    worker_register = worker_sub.add_parser("register")
    worker_register.add_argument("--worker-id", required=True)
    worker_register.set_defaults(func=cmd_worker_register)
    worker_heartbeat = worker_sub.add_parser("heartbeat")
    worker_heartbeat.add_argument("--worker-id", required=True)
    worker_heartbeat.add_argument("--status", default="idle", choices=["idle", "busy", "compressing", "offline"])
    worker_heartbeat.add_argument("--job-id")
    worker_heartbeat.add_argument("--lease-timeout", type=float, default=300.0)
    worker_heartbeat.set_defaults(func=cmd_worker_heartbeat)
    worker_claim = worker_sub.add_parser("claim")
    worker_claim.add_argument("--job-id", required=True)
    worker_claim.add_argument("--lease-timeout", type=float, default=300.0)
    worker_claim.set_defaults(func=cmd_worker_claim)
    worker_complete = worker_sub.add_parser("complete")
    worker_complete.add_argument("--worker-id", required=True)
    worker_complete.add_argument("--job-id", required=True)
    worker_complete.set_defaults(func=cmd_worker_complete)
    worker_list = worker_sub.add_parser("list")
    worker_list.set_defaults(func=cmd_worker_list)

    debug_ui = sub.add_parser("debug-ui")
    debug_ui.add_argument("--host", default="127.0.0.1")
    debug_ui.add_argument("--port", type=int, default=8899)
    debug_ui.set_defaults(func=cmd_debug_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
