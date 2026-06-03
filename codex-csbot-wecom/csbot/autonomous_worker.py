from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .codex_cli import (
    codex_catalog_config_args,
    codex_extra_args,
    codex_model_args,
    codex_output_schema_args,
    ensure_isolated_codex_home,
    resolve_codex_command,
)
from .codex_contract import validate_autonomous_reply
from .config import DEFAULT_PROJECT_DIR, resolve_codex_workdir, resolve_db_path, resolve_mem0_url, using_pg
from .db import connect, ensure_schema
from .textutil import json_loads, normalize_text, score_text


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_DIR / "schemas" / "codex_reply.schema.json"
TIMEOUT_REPLY_TEXT = "您好，这个问题我需要转人工客服确认处理，请您稍等。"
PI_PROVIDER_NAME = "pi"
VALID_ACTIONS = {"send", "clarify", "handoff", "no_answer"}
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def _looks_like_reply(value: object) -> bool:
    return isinstance(value, dict) and value.get("action") in VALID_ACTIONS and isinstance(value.get("reply_text"), str)


def safe_json_parse(text: str) -> tuple[dict | None, str]:
    value = json_loads(text, None)
    if _looks_like_reply(value):
        return value, ""
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    for candidate in reversed(candidates):
        value = json_loads(candidate, None)
        if _looks_like_reply(value):
            return value, ""
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        value = json_loads(text[start : end + 1], None)
        if _looks_like_reply(value):
            return value, ""
    for idx, char in enumerate(text):
        if char != "{":
            continue
        decoder = json.JSONDecoder()
        try:
            value, _end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if _looks_like_reply(value):
            return value, ""
    return None, "codex_output_is_not_json"


def to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _is_supplement_context(context: dict | None) -> bool:
    if not isinstance(context, dict):
        return False
    if str(context.get("agent_mode") or "").strip() == "supplement":
        return True
    agent_context = context.get("agent_context")
    return isinstance(agent_context, dict) and str(agent_context.get("reply_source") or "").strip() == "supplement"


def _tool_examples(db_path: Path, customer_id: str, query: str, context: dict | None = None) -> list[str]:
    encoded_query = json.dumps(query, ensure_ascii=False)
    encoded_customer = json.dumps(customer_id, ensure_ascii=False)
    db_args = "" if using_pg() else f"--db {json.dumps(str(db_path), ensure_ascii=False)} "
    scope_context = {"scope": "supplement", "agent_mode": "supplement"} if _is_supplement_context(context) else {}
    scope_arg = f" --context-json {json.dumps(json.dumps(scope_context, ensure_ascii=False), ensure_ascii=False)}" if scope_context else ""
    return [
        (
            f"/opt/homebrew/bin/python3 -m csbot {db_args}"
            f"retrieve --customer-id {encoded_customer} --query {encoded_query}{scope_arg}"
        ),
        (
            f"/opt/homebrew/bin/python3 -m csbot {db_args}"
            f"mem search --customer-id {encoded_customer} --query {encoded_query}"
        ),
        (
            f"/opt/homebrew/bin/python3 -m csbot {db_args}"
            f"mem search --query {encoded_query}"
        ),
        (
            f"/opt/homebrew/bin/python3 -m csbot {db_args}"
            f"kb search --query {encoded_query}{scope_arg}"
        ),
        "/opt/homebrew/bin/python3 -m csbot ops order --identifier \"手机号或订单号\" --id-type phone",
        "/opt/homebrew/bin/python3 -m csbot ops logistics --order-id \"订单号\"",
        "/opt/homebrew/bin/python3 -m csbot ops logistics --external-user-id \"企微external_user_id\"",
        "/opt/homebrew/bin/python3 -m csbot ops ticket-draft --context-json '{...}'",
        "/opt/homebrew/bin/python3 -m csbot ops handoff --customer-id \"客户ID\" --query \"客户消息\" --reason \"转人工原因\"",
    ]


def _agent_rules_path() -> Path:
    configured = os.environ.get("CSBOT_AGENTS_FILE") or os.environ.get("WECOM_GUI_AGENTS_FILE")
    if configured:
        return Path(configured).expanduser()
    return resolve_codex_workdir() / "AGENTS.md"


def _load_agent_rules() -> tuple[str, Path | None]:
    path = _agent_rules_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise FileNotFoundError(f"AGENTS.md not found for autonomous worker: {path}") from exc
    if not text:
        raise ValueError(f"AGENTS.md is empty for autonomous worker: {path}")
    return text, path


def build_autonomous_prompt(
    *,
    customer_id: str,
    query: str,
    context: dict,
    db_path: str | Path,
    mem0_url: str,
) -> str:
    resolved_db_path = resolve_db_path(db_path)
    codex_workdir = resolve_codex_workdir()
    command_examples = _tool_examples(resolved_db_path, customer_id, query, context)
    context_json = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    commands_json = json.dumps(command_examples, ensure_ascii=False, indent=2)
    knowledge_backend = "PostgreSQL PG 知识库" if using_pg() else "SQLite 兼容知识库"
    agent_rules, agent_rules_path = _load_agent_rules()
    runtime_context = {
        "project_dir": str(DEFAULT_PROJECT_DIR),
        "workdir": str(codex_workdir),
        "agent_rules_file": str(agent_rules_path),
        "knowledge_backend": knowledge_backend,
        "sqlite_compat_db_path": str(resolved_db_path),
        "mem0_url": mem0_url,
        "schema_path": str(SCHEMA_PATH),
        "knowledge_tables": [
            "kb_docs(kb_doc_id,kb_version,business_type,product,topic,text,facts_json,source_sheet,source_row,source_field)",
            "kb_aliases(alias,product,kb_doc_id,weight)",
            "reply_audit(customer_id,query,retrieval_json,reply_json)",
            "vector_memories(customer_id,text,metadata_json,embedding_json)",
        ],
        "tool_commands": command_examples,
        "customer_id": customer_id,
        "latest_message": query,
        "context": context,
    }
    supplement_instructions = ""
    if isinstance(context, dict) and context.get("agent_mode") == "supplement":
        supplement_instructions = """

<supplement_recommendation_agent_instructions>
你现在处于补剂推荐 Agent 模式，reply_source=supplement。
必须围绕客户补剂推荐诉求工作：识别基础信息、需求点、挖需结果，并按知识库证据推荐产品或追问。

必须连接和使用所有相关知识库工具：
1. 使用 `retrieve --customer-id ... --query ... --context-json '{"scope":"supplement","agent_mode":"supplement"}'` 做 PG/script 与 MEM0 双召回。
2. 使用 `kb search --query ... --context-json '{"scope":"supplement","agent_mode":"supplement"}'` 检索 SQL/PG 中的 `10 补剂推荐`、`5 产品常规信息`、`7 L0级注意事项`、`6 论文表` 或 kb_docs 中的 recommendation_rule、product_profile、safety_policy、research_evidence。
3. 使用 `mem search --customer-id ... --query ...` 查询客户画像、历史购买、偏好和既往需求；Mem0 只能辅助画像，不能覆盖 PG/script 产品事实。

事实优先级：
- 产品事实、推荐规则、禁忌、吃法、价格、链接、免责话术必须以 SQL/PG/script/kb_docs 为准。
- 推荐产品后必须追加数据库已有的相关论文链接：优先查询 `6 论文表` / research_evidence，使用标题/论文方向 + 链接；没有查到真实链接时不要编造，也不要添加论文段落。
- SQL/PG 与 Mem0 冲突时，以 SQL/PG 为准，并在 conflicts 中记录冲突摘要。
- 证据不足时 action 用 clarify 或 handoff，不要编造推荐规则、产品事实或医疗功效。

流程要求：
- 第一步：GUI 已在欢迎语后发送第一段固定话术，收集基础信息并让用户选择 3-5 个需求点；不要重复输出第一段固定话术。
- 第二步：根据用户回复的数字或文字需求点进行追问，一共追问两次；如果 context.agent_context.digging_count 小于 2，优先根据 `需求点`、`挖需铺垫`、`挖需问题` 生成下一轮追问，action 用 clarify。
- 挖需回复一次只能提出 1 个问题，必须直接提问；不要写「从xx方面看」「从xx角度看」「结合您的情况」「考虑到」等铺垫或分析话术。
- 如果 context.agent_context.profile_opt_out=true，或用户明确表示不愿意/不方便提供个人信息、基础信息、年龄、身高、体重等隐私信息，不要再追问这些信息；只基于用户已给出的需求点继续问 1 个非隐私挖需问题，或在证据足够时直接推荐。
- 第三步：追问完成或信息已经足够后推荐产品。推荐时按 `需求点 + 挖需结果` 匹配推荐规则；无法匹配具体挖需结果时使用兜底推荐规则。
- 同一结果下按 `相同挖需结果下的优先级` 排序：高 > 中 > 空。
- 关联 `5 产品常规信息` 补充适用年龄、服用方法、禁忌、规格和链接。
- 必须按 `7 L0级注意事项` 做合规校验，禁止治疗承诺和极限词；特殊场景必须附带知识库中的推荐后免责话术。
- 推荐产品不超过 3 个时，回复必须使用这个开头：
  结合您的需求，为您推荐这几款产品组合。接下来，我详细为您介绍下：
  然后填写推荐产品介绍。
- 推荐产品超过 3 个时，回复必须使用这个结构：
  结合您的需求，优先为您推荐这几款产品组合。接下来，我详细为您介绍下：
  先介绍高优先级产品。
  如果您服用后感觉效果良好，后续可以搭配以下产品：
  再介绍后续可搭配产品。
- 推荐正文最后如果有论文链接，追加：
  相关论文参考：
  - 论文标题或方向：数据库链接

输出仍必须是 schema 要求的 JSON。commands_run 记录实际执行过的工具命令，不得记录 API key、token、完整隐私画像或完整请求体。
</supplement_recommendation_agent_instructions>
"""
    return f"""{agent_rules}
{supplement_instructions}

<runtime_context_json>
{json.dumps(runtime_context, ensure_ascii=False, indent=2)}
</runtime_context_json>
"""


def _autonomous_command(out_path: Path) -> tuple[list[str], dict]:
    codex_command = resolve_codex_command()
    catalog_args, catalog_status = codex_catalog_config_args(codex_command)
    isolated_home = ensure_isolated_codex_home()
    codex_workdir = resolve_codex_workdir()
    return [
        codex_command,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "danger-full-access",
        "-C",
        str(codex_workdir),
        *codex_output_schema_args("schemas/codex_reply.schema.json"),
        "--output-last-message",
        str(out_path),
        *catalog_args,
        *codex_model_args(),
        *codex_extra_args(),
        "-",
    ], {
        "provider": "codex",
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
        },
    }


def _pi_autonomous_command() -> tuple[list[str], dict]:
    pi_command = os.environ.get("CSBOT_PI_COMMAND", os.environ.get("WECOM_GUI_PI_COMMAND", "/opt/homebrew/bin/pi")).strip()
    if not pi_command:
        pi_command = "/opt/homebrew/bin/pi"
    pi_home = os.environ.get(
        "CSBOT_PI_HOME",
        os.environ.get("WECOM_GUI_PI_HOME", str(Path.home() / ".codex-csbot-wecom" / "pi-home")),
    ).strip()
    provider = os.environ.get("CSBOT_PI_PROVIDER", os.environ.get("WECOM_GUI_PI_PROVIDER", "uda-openai")).strip()
    model = os.environ.get(
        "CSBOT_PI_TEXT_MODEL",
        os.environ.get("WECOM_GUI_PI_TEXT_MODEL", os.environ.get("CSBOT_PI_MODEL", os.environ.get("WECOM_GUI_PI_MODEL", "deepseek-v4-flash"))),
    ).strip()
    timeout = os.environ.get("CSBOT_PI_TIMEOUT", os.environ.get("WECOM_GUI_PI_TIMEOUT", "")).strip()
    args = [
        pi_command,
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-themes",
        "--provider",
        provider,
        "--model",
        model,
        "--thinking",
        os.environ.get("CSBOT_PI_THINKING", os.environ.get("WECOM_GUI_PI_THINKING", "off")).strip() or "off",
        "-p",
    ]
    return args, {
        "provider": PI_PROVIDER_NAME,
        "command": pi_command,
        "home": pi_home,
        "pi_provider": provider,
        "model": model,
        "timeout": timeout,
        "supports_images": False,
    }


def _valid_image_path(path: str) -> bool:
    image_path = Path(path).expanduser()
    try:
        if not image_path.is_file() or image_path.stat().st_size < 16:
            return False
        head = image_path.read_bytes()[:8]
    except OSError:
        return False
    return any(head.startswith(magic) for magic in IMAGE_MAGIC)


def _context_image_paths(context: dict) -> list[str]:
    raw_paths: list[str] = []
    image_paths = context.get("image_paths")
    if isinstance(image_paths, list):
        raw_paths.extend(str(path) for path in image_paths)
    for message in context.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for media in message.get("media") or []:
            if not isinstance(media, dict):
                continue
            if media.get("capture_ok") is False:
                continue
            path = str(media.get("capture_path") or "").strip()
            if path:
                raw_paths.append(path)
    paths: list[str] = []
    seen: set[str] = set()
    for path in raw_paths:
        expanded = str(Path(path).expanduser())
        if expanded in seen or not _valid_image_path(expanded):
            continue
        seen.add(expanded)
        paths.append(expanded)
    return paths


def _pi_image_model() -> str:
    return os.environ.get(
        "CSBOT_PI_IMAGE_MODEL",
        os.environ.get("WECOM_GUI_PI_IMAGE_MODEL", ""),
    ).strip()


def _with_pi_image_args(command: list[str], worker_info: dict, image_paths: list[str], prompt_path: Path) -> tuple[list[str], dict]:
    if not image_paths or worker_info.get("provider") != PI_PROVIDER_NAME:
        return command, worker_info
    model = _pi_image_model()
    if not model:
        model = str(worker_info.get("model") or "").strip()
    if not model:
        return command, worker_info
    args = list(command)
    if "--model" in args:
        args[args.index("--model") + 1] = model
    else:
        args.extend(["--model", model])
    args.extend(
        [
            f"@{prompt_path}",
            *[f"@{path}" for path in image_paths],
            (
                "随附图片是当前客户最新消息的真实图片内容，必须直接查看并识别图片。"
                "如果聊天历史里有客服曾说看不到图片，忽略那类旧话术，以本次随附图片为准。"
                "只要图片文件有效且可见，就禁止回复看不到、无法查看、请客户描述图片。"
                "结合 prompt 中的客服规则、聊天上下文和知识库要求，输出最终可发送给客户的 JSON 回复。"
            ),
        ]
    )
    info = {**worker_info, "model": model, "image_paths": image_paths, "supports_images": True}
    return args, info


def _worker_command(out_path: Path) -> tuple[list[str], dict]:
    provider = os.environ.get("CSBOT_AUTONOMOUS_PROVIDER", os.environ.get("WECOM_GUI_AI_PROVIDER", "codex")).strip().lower()
    if provider == PI_PROVIDER_NAME:
        return _pi_autonomous_command()
    return _autonomous_command(out_path)


def _worker_env(mem0_url: str, worker_info: dict) -> dict:
    env = {**os.environ, "CSBOT_MEM0_URL": mem0_url or os.environ.get("CSBOT_MEM0_URL", "http://127.0.0.1:8888")}
    if worker_info.get("provider") == PI_PROVIDER_NAME:
        pi_home = str(worker_info.get("home") or "").strip()
        if pi_home:
            env["PI_CODING_AGENT_DIR"] = pi_home
        return env
    isolated_home = (worker_info.get("isolated_home") or {}) if isinstance(worker_info.get("isolated_home"), dict) else {}
    if isolated_home.get("enabled") and isolated_home.get("path") and not isolated_home.get("error"):
        env["CODEX_HOME"] = str(isolated_home["path"])
    return env


def _notify_handoff_if_needed(*, customer_id: str, query: str, context: dict, reply: dict | None) -> dict | None:
    if not isinstance(reply, dict) or reply.get("action") != "handoff":
        return None
    return {
        "ok": True,
        "notified": False,
        "reason": "handled_by_wecom_review",
        "customer_id": customer_id,
        "query": query,
        "handoff_reason": str(reply.get("decision_basis") or reply.get("reply_text") or "转人工").strip(),
        "context": context,
    }


def _timeout_reply(timeout: int) -> dict:
    return {
        "action": "handoff",
        "reply_text": TIMEOUT_REPLY_TEXT,
        "used_script_sources": [],
        "used_vector_memories": [],
        "confidence": 0.0,
        "commands_run": [],
        "conflicts": [],
        "retrieval_summary": f"AI worker 在 {timeout} 秒内没有返回可解析 JSON，超过等待阈值后转人工。",
        "decision_basis": "codex_timeout",
    }


def _paper_link_from_facts(facts: dict) -> str:
    for key in ("链接", "论文链接", "URL", "url", "DOI"):
        value = normalize_text(facts.get(key))
        if value:
            if key == "DOI" and not value.lower().startswith(("http://", "https://")):
                return f"https://doi.org/{value}"
            return value
    return ""


def _paper_title_from_row(row, facts: dict) -> str:
    return (
        normalize_text(facts.get("论文名称"))
        or normalize_text(facts.get("标题"))
        or normalize_text(facts.get("论文方向"))
        or normalize_text(row["topic"])
        or normalize_text(row["product"])
        or "相关研究"
    )


def _supplement_search_text(query: str, reply_text: str, context: dict) -> str:
    parts = [query, reply_text]
    agent_context = context.get("agent_context") if isinstance(context, dict) else None
    if isinstance(agent_context, dict):
        parts.extend(normalize_text(value) for value in agent_context.get("selected_needs") or [])
        known_profile = agent_context.get("known_profile")
        if isinstance(known_profile, dict):
            parts.extend(normalize_text(value) for value in known_profile.values())
    return "\n".join(part for part in parts if normalize_text(part))


def _find_supplement_paper_links(
    *,
    query: str,
    reply_text: str,
    context: dict,
    db_path: str | Path,
    limit: int = 3,
) -> list[dict]:
    search_text = _supplement_search_text(query, reply_text, context)
    if not search_text:
        return []
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT * FROM kb_docs
            WHERE business_type = ?
            """,
            ("research_evidence",),
        ).fetchall()
    finally:
        conn.close()

    hits = []
    for row in rows:
        facts = json_loads(row["facts_json"], {})
        if not isinstance(facts, dict):
            facts = {}
        link = _paper_link_from_facts(facts)
        if not link:
            continue
        text = normalize_text(row["text"])
        product = normalize_text(row["product"])
        title = _paper_title_from_row(row, facts)
        direction = normalize_text(facts.get("论文方向"))
        score = score_text(search_text, text)
        if product and product in search_text:
            score += 0.7
        for value in (title, direction, normalize_text(facts.get("产品")), normalize_text(facts.get("产品全称"))):
            if value and value in search_text:
                score += 0.3
        if score <= 0:
            continue
        hits.append(
            {
                "title": title,
                "link": link,
                "score": score,
                "source": {
                    "sheet": row["source_sheet"],
                    "row": row["source_row"],
                    "field": row["source_field"] or "",
                    "kb_doc_id": row["kb_doc_id"],
                },
            }
        )
    hits.sort(key=lambda item: item["score"], reverse=True)
    deduped = []
    seen_links: set[str] = set()
    for hit in hits:
        if hit["link"] in seen_links:
            continue
        seen_links.add(hit["link"])
        deduped.append(hit)
        if len(deduped) >= limit:
            break
    return deduped


def _append_supplement_paper_links(
    reply: dict | None,
    *,
    query: str,
    context: dict,
    db_path: str | Path,
) -> dict | None:
    if not isinstance(reply, dict) or reply.get("action") != "send" or not _is_supplement_context(context):
        return reply
    reply_text = normalize_text(reply.get("reply_text"))
    if not reply_text or "相关论文参考" in reply_text:
        return reply
    papers = _find_supplement_paper_links(query=query, reply_text=reply_text, context=context, db_path=db_path)
    if not papers:
        return reply
    lines = ["相关论文参考："]
    for paper in papers:
        if paper["link"] in reply_text:
            continue
        lines.append(f"- {paper['title']}：{paper['link']}")
    if len(lines) == 1:
        return reply
    reply = dict(reply)
    reply["reply_text"] = f"{reply_text}\n\n" + "\n".join(lines)
    used_sources = reply.get("used_script_sources")
    if not isinstance(used_sources, list):
        used_sources = []
    existing_sources = {
        (
            source.get("sheet"),
            source.get("row"),
            source.get("field", ""),
            source.get("kb_doc_id", ""),
        )
        for source in used_sources
        if isinstance(source, dict)
    }
    for paper in papers:
        source = paper["source"]
        key = (source.get("sheet"), source.get("row"), source.get("field", ""), source.get("kb_doc_id", ""))
        if key not in existing_sources:
            used_sources.append(source)
            existing_sources.add(key)
    reply["used_script_sources"] = used_sources
    commands_run = reply.get("commands_run")
    if isinstance(commands_run, list):
        commands_run.append(
            {
                "command": "kb_docs research_evidence lookup",
                "purpose": "补剂推荐后追加数据库论文链接",
                "success": True,
            }
        )
    summary = normalize_text(reply.get("retrieval_summary"))
    paper_summary = "已从 research_evidence/6 论文表追加相关论文链接。"
    reply["retrieval_summary"] = f"{summary} {paper_summary}".strip() if summary else paper_summary
    return reply


def run_autonomous_worker(
    *,
    customer_id: str,
    query: str,
    context: dict,
    timeout: int = 180,
    db_path: str | Path | None = None,
) -> dict:
    resolved_db_path = resolve_db_path(db_path)
    mem0_url = resolve_mem0_url() or os.environ.get("CSBOT_MEM0_URL", "")
    prompt = build_autonomous_prompt(
        customer_id=customer_id,
        query=query,
        context=context,
        db_path=resolved_db_path,
        mem0_url=mem0_url,
    )
    started = time.perf_counter()
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out_file:
        out_path = Path(out_file.name)
    command, codex_cli = _worker_command(out_path)
    codex_workdir = resolve_codex_workdir()
    env = _worker_env(mem0_url, codex_cli)
    image_paths = _context_image_paths(context)
    prompt_path: Path | None = None
    input_text: str | None = prompt
    if image_paths and codex_cli.get("provider") == PI_PROVIDER_NAME:
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".md", delete=False) as prompt_file:
            prompt_file.write(prompt)
            prompt_path = Path(prompt_file.name)
        command, codex_cli = _with_pi_image_args(command, codex_cli, image_paths, prompt_path)
        input_text = None
    try:
        proc = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=str(codex_workdir),
            env=env,
        )
        last_message = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        reply, parse_error = safe_json_parse(last_message or proc.stdout)
        reply = _append_supplement_paper_links(reply, query=query, context=context, db_path=resolved_db_path)
        validation = validate_autonomous_reply(reply or {})
        handoff = _notify_handoff_if_needed(
            customer_id=customer_id,
            query=query,
            context=context,
            reply=reply,
        )
        return {
            "skipped": False,
            "mode": "autonomous",
            "prompt": prompt,
            "command": command,
            "codex_cli": codex_cli,
            "stdout": to_text(proc.stdout),
            "stderr": to_text(proc.stderr),
            "last_message": last_message,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "exit_code": proc.returncode,
            "reply": reply,
            "handoff": handoff,
            "parse_error": parse_error,
            "validation": {"ok": validation.ok, "reason": validation.reason},
        }
    except subprocess.TimeoutExpired as exc:
        last_message = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        reply = _timeout_reply(timeout)
        handoff = _notify_handoff_if_needed(
            customer_id=customer_id,
            query=query,
            context=context,
            reply=reply,
        )
        return {
            "skipped": False,
            "mode": "autonomous",
            "prompt": prompt,
            "command": command,
            "codex_cli": codex_cli,
            "stdout": to_text(exc.stdout),
            "stderr": to_text(exc.stderr),
            "last_message": last_message,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "exit_code": None,
            "reply": reply,
            "handoff": handoff,
            "parse_error": "codex_timeout",
            "validation": {"ok": True, "reason": ""},
        }
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass
        if prompt_path is not None:
            try:
                prompt_path.unlink()
            except OSError:
                pass
