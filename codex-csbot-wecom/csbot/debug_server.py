from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .autonomous_worker import run_autonomous_worker, safe_json_parse, to_text
from .codex_cli import (
    codex_catalog_config_args,
    codex_extra_args,
    codex_model_args,
    codex_output_schema_args,
    ensure_isolated_codex_home,
    resolve_codex_command,
)
from .codex_contract import validate_codex_reply
from .config import DEFAULT_PROJECT_DIR, resolve_codex_workdir, resolve_db_path
from .policy import handoff_reply, should_handoff
from .retrieve import retrieve
from .textutil import json_loads


PROJECT_DIR = Path(__file__).resolve().parents[1]
WEB_DIR = PROJECT_DIR / "web"
SCHEMA_PATH = PROJECT_DIR / "schemas" / "codex_reply.schema.json"


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _safe_json_parse(text: str) -> tuple[dict | None, str]:
    return safe_json_parse(text)


def _to_text(value: object) -> str:
    return to_text(value)


def _reply_from_handoff(query: str) -> dict | None:
    if not should_handoff(query):
        return None
    return {
        "action": "handoff",
        "reply_text": handoff_reply(),
        "used_script_sources": [],
        "used_vector_memories": [],
        "confidence": 1.0,
    }


def _worker_prompt(*, customer_id: str, query: str, context: dict, retrieval: dict) -> str:
    return f"""你是全自动客服 Codex worker。只输出一个 JSON 对象，不要 Markdown，不要解释。

你已经收到主程序提供的双路召回结果。必须只基于这个 retrieval JSON 生成回复。

硬规则：
1. factual 回复必须使用 script_hits 里的 source，并把这些 source 放到 used_script_sources。
2. vector_hits 只能用于用户画像、历史上下文、模糊指代补充，不能覆盖 script_hits 的事实。
3. 如果 merged_context.needs_clarification=true，action 用 clarify，reply_text 只追问客户需要确认的信息。
4. 投诉、退款、人工升级时 action 用 handoff。
5. 如果没有可用 script_hits 支撑事实答案，action 用 no_answer 或 clarify，不能编造。
6. 输出必须符合这个 JSON 形状：
{{
  "action": "send | clarify | handoff | no_answer",
  "reply_text": "最终发给客户的话术",
  "used_script_sources": [],
  "used_vector_memories": [],
  "confidence": 0.0
}}

客户 ID: {customer_id}
客户最新消息: {query}
客户上下文 JSON:
{json.dumps(context, ensure_ascii=False, indent=2)}

retrieval JSON:
{json.dumps(retrieval, ensure_ascii=False, indent=2)}
"""


def run_codex_worker(
    *,
    customer_id: str,
    query: str,
    context: dict,
    retrieval_result: dict,
    timeout: int = 180,
) -> dict:
    handoff = _reply_from_handoff(query)
    prompt = _worker_prompt(customer_id=customer_id, query=query, context=context, retrieval=retrieval_result)
    if handoff is not None:
        validation = validate_codex_reply(handoff, retrieval_result)
        return {
            "skipped": True,
            "skip_reason": "handoff_policy",
            "prompt": prompt,
            "command": [],
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
            "exit_code": 0,
            "reply": handoff,
            "parse_error": "",
            "validation": {"ok": validation.ok, "reason": validation.reason},
        }

    codex_command = resolve_codex_command()
    catalog_args, catalog_status = codex_catalog_config_args(codex_command)
    isolated_home = ensure_isolated_codex_home()
    codex_workdir = resolve_codex_workdir()
    command = [
        codex_command,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "-C",
        str(codex_workdir),
        *codex_output_schema_args(SCHEMA_PATH),
        *catalog_args,
        *codex_model_args(),
        *codex_extra_args(),
        "-",
    ]
    codex_cli = {
        "model_catalog": {
            "enabled": catalog_status.enabled,
            "path": catalog_status.path,
            "generated": catalog_status.generated,
            "error": catalog_status.error,
        },
        "isolated_home": {
            "enabled": isolated_home.enabled,
            "path": isolated_home.path,
            "generated": isolated_home.generated,
            "error": isolated_home.error,
        }
    }
    started = time.perf_counter()
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out_file:
        out_path = Path(out_file.name)
    command_with_output = [*command[:-1], "--output-last-message", str(out_path), command[-1]]
    try:
        proc = subprocess.run(
            command_with_output,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=str(codex_workdir),
            env={
                **os.environ,
                **(
                    {"CODEX_HOME": str(isolated_home.path)}
                    if isolated_home.enabled and isolated_home.path and not isolated_home.error
                    else {}
                ),
                "CSBOT_MEM0_URL": os.environ.get("CSBOT_MEM0_URL", "http://127.0.0.1:8888"),
            },
        )
        last_message = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        reply, parse_error = _safe_json_parse(last_message or proc.stdout)
        validation = validate_codex_reply(reply or {}, retrieval_result)
        return {
            "skipped": False,
            "prompt": prompt,
            "command": command_with_output,
            "codex_cli": codex_cli,
            "stdout": _to_text(proc.stdout),
            "stderr": _to_text(proc.stderr),
            "last_message": last_message,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "exit_code": proc.returncode,
            "reply": reply,
            "parse_error": parse_error,
            "validation": {"ok": validation.ok, "reason": validation.reason},
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "skipped": False,
            "prompt": prompt,
            "command": command_with_output,
            "codex_cli": codex_cli,
            "stdout": _to_text(exc.stdout),
            "stderr": _to_text(exc.stderr),
            "last_message": out_path.read_text(encoding="utf-8") if out_path.exists() else "",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "exit_code": None,
            "reply": None,
            "parse_error": "codex_timeout",
            "validation": {"ok": False, "reason": "codex_timeout"},
        }
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass


def run_debug_case(payload: dict) -> dict:
    customer_id = str(payload.get("customer_id") or "debug-customer").strip()
    query = str(payload.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    raw_context = payload.get("context")
    context = raw_context if isinstance(raw_context, dict) else {}
    if isinstance(raw_context, str) and raw_context.strip():
        parsed = json_loads(raw_context, {})
        context = parsed if isinstance(parsed, dict) else {}
    run_codex = bool(payload.get("run_codex", True))
    timeout = int(payload.get("timeout") or 180)
    db_path = resolve_db_path(payload.get("db_path"))
    mode = str(payload.get("mode") or "").strip().lower()

    os.environ.setdefault("CSBOT_MEM0_URL", "http://127.0.0.1:8888")
    if mode == "autonomous":
        codex_result = (
            run_autonomous_worker(
                customer_id=customer_id,
                query=query,
                context=context,
                timeout=timeout,
                db_path=db_path,
            )
            if run_codex
            else {"skipped": True, "skip_reason": "run_codex=false", "mode": "autonomous"}
        )
        return {
            "ok": True,
            "mode": "autonomous",
            "request": {"mode": "autonomous", "customer_id": customer_id, "query": query, "context": context},
            "retrieval": {
                "query": query,
                "customer_id": customer_id,
                "mode": "autonomous",
                "script_hits": [],
                "vector_hits": [],
                "merged_context": {
                    "answer_basis": "autonomous_worker",
                    "needs_clarification": None,
                    "conflicts": [],
                    "vector_error": None,
                },
            },
            "retrieval_trace": {
                "mode": "autonomous",
                "duration_ms": 0,
                "script_count": None,
                "vector_count": None,
                "answer_basis": "autonomous_worker",
                "needs_clarification": None,
                "vector_error": None,
            },
            "codex": codex_result,
        }

    started = time.perf_counter()
    retrieval_result = retrieve(
        customer_id=customer_id,
        query=query,
        db_path=db_path,
        context=context,
    )
    retrieval_trace = {
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "script_count": len(retrieval_result.get("script_hits", [])),
        "vector_count": len(retrieval_result.get("vector_hits", [])),
        "answer_basis": retrieval_result.get("merged_context", {}).get("answer_basis"),
        "needs_clarification": retrieval_result.get("merged_context", {}).get("needs_clarification"),
        "vector_error": retrieval_result.get("merged_context", {}).get("vector_error"),
    }
    codex_result = (
        run_codex_worker(
            customer_id=customer_id,
            query=query,
            context=context,
            retrieval_result=retrieval_result,
            timeout=timeout,
        )
        if run_codex
        else {"skipped": True, "skip_reason": "run_codex=false"}
    )
    return {
        "ok": True,
        "mode": "fixed_dual",
        "request": {"mode": "fixed_dual", "customer_id": customer_id, "query": query, "context": context},
        "retrieval": retrieval_result,
        "retrieval_trace": retrieval_trace,
        "codex": codex_result,
    }


class DebugHandler(BaseHTTPRequestHandler):
    server_version = "CsbotDebug/0.1"

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/", "/debug"}:
            self._serve_file(WEB_DIR / "debug.html", "text/html; charset=utf-8")
            return
        if path == "/api/health":
            _json_response(self, HTTPStatus.OK, {"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/debug-run":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            result = run_debug_case(_read_json_body(self))
            _json_response(self, HTTPStatus.OK, result)
        except Exception as exc:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        raw = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def serve(host: str = "127.0.0.1", port: int = 8899) -> None:
    httpd = ThreadingHTTPServer((host, port), DebugHandler)
    print(f"csbot debug UI: http://{host}:{port}")
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
