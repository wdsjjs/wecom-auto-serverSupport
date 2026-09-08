"""Click CLI for WeCom desktop GUI customer-service automation."""

from __future__ import annotations

import shlex
import os
from pathlib import Path

import click

from cli_anything.wecom_gui import __version__
from cli_anything.wecom_gui.core import agent as agent_core
from cli_anything.wecom_gui.core import agent_input as agent_input_core
from cli_anything.wecom_gui.core import app as app_core
from cli_anything.wecom_gui.core import chat as chat_core
from cli_anything.wecom_gui.core import edge_state as edge_state_core
from cli_anything.wecom_gui.core import edge_worker as edge_worker_core
from cli_anything.wecom_gui.core import inbox as inbox_core
from cli_anything.wecom_gui.core import llm as llm_core
from cli_anything.wecom_gui.core import reply as reply_core
from cli_anything.wecom_gui.core import review_server
from cli_anything.wecom_gui.core import runtime_state as runtime_state_core
from cli_anything.wecom_gui.core import sidebar_server
from cli_anything.wecom_gui.core import state as state_core
from cli_anything.wecom_gui.core import supervisor as supervisor_core
from cli_anything.wecom_gui.core import watcher
from cli_anything.wecom_gui.core import worker as worker_core
from cli_anything.wecom_gui.utils import output


def _load_env_local(path: str | Path = ".env.local") -> None:
    """Load simple KEY=VALUE pairs from .env.local without overriding env."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if not parts or "=" not in parts[0]:
            continue
        key, value = parts[0].split("=", 1)
        if key and key not in os.environ:
            os.environ[key] = value


def _emit_or_fail(func, *args, message: str | None = None, **kwargs) -> None:
    """Run a command body and convert runtime failures to clean CLI errors."""
    try:
        output.emit(func(*args, **kwargs), message)
    except Exception as exc:
        output.error(str(exc), code=type(exc).__name__)
        raise click.exceptions.Exit(1)


@click.group(invoke_without_command=True)
@click.option("--json", "use_json", is_flag=True, help="Output as JSON.")
@click.version_option(__version__, prog_name="cli-anything-wecom-gui")
@click.pass_context
def cli(ctx: click.Context, use_json: bool) -> None:
    """Drive WeCom desktop GUI for customer-service automation.

    Run without a subcommand to enter the interactive REPL.
    """
    _load_env_local()
    output.USE_JSON = use_json
    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


@cli.command()
def doctor() -> None:
    """Check local WeCom GUI automation prerequisites."""
    _emit_or_fail(app_core.doctor, message="WeCom GUI automation doctor")


@cli.group()
def app() -> None:
    """Launch and focus the WeCom desktop app."""


@app.command()
@click.option("--app-name", default=None, help="Override app name, e.g. WeCom or 企业微信.")
def focus(app_name: str | None) -> None:
    """Focus the WeCom desktop app."""
    _emit_or_fail(app_core.focus, app_name, message="Focused WeCom")


@cli.group()
def inbox() -> None:
    """Scan visible inbox/conversation labels."""


@inbox.command("scan")
@click.option("--limit", default=30, show_default=True, type=click.IntRange(min=1))
@click.option("--app-name", default=None, help="Override app name.")
def inbox_scan(limit: int, app_name: str | None) -> None:
    """Scan visible conversation labels from the WeCom window."""
    _emit_or_fail(inbox_core.scan_visible, app_name, limit=limit)


@cli.group()
def chat() -> None:
    """Inspect the current WeCom chat."""


@chat.command("open")
@click.option("--name", required=True, help="Visible conversation label to click.")
@click.option("--app-name", default=None, help="Override app name.")
def chat_open(name: str, app_name: str | None) -> None:
    """Open a visible chat by exact label."""
    _emit_or_fail(inbox_core.open_by_name, name, app_name)


@chat.command("read")
@click.option("--last", default=10, show_default=True, type=click.IntRange(min=1), help="Visible lines to return.")
@click.option("--app-name", default=None, help="Override app name.")
@click.option("--capture-images", is_flag=True, help="Open image previews and capture image attachments.")
def chat_read(last: int, app_name: str | None, capture_images: bool) -> None:
    """Read recent visible text from the current chat window."""
    _emit_or_fail(chat_core.read_current, last=last, app_name=app_name, capture_images=capture_images)


@cli.group()
def ai() -> None:
    """Draft replies from visible chat context."""


@ai.command("draft")
@click.option("--last", default=10, show_default=True, type=click.IntRange(min=1))
@click.option("--fallback", default=None, help="Fallback text when no API key is configured.")
@click.option(
    "--provider",
    default=None,
    type=click.Choice(["uda", "openai", "codex", "fallback"]),
    help="AI provider. Defaults to WECOM_GUI_AI_PROVIDER or uda.",
)
def ai_draft(last: int, fallback: str | None, provider: str | None) -> None:
    """Generate a draft reply for the current chat."""
    try:
        context = chat_core.read_current(last=last, capture_images=True)
        output.emit(llm_core.draft_reply(context["messages"], fallback=fallback, provider=provider))
    except Exception as exc:
        output.error(str(exc), code=type(exc).__name__)
        raise click.exceptions.Exit(1)


@cli.group()
def reply() -> None:
    """Send or stage replies in the current chat."""


@reply.command("send")
@click.option("--text", required=True, help="Reply text to paste/send.")
@click.option("--dry-run", is_flag=True, help="Do not touch the GUI.")
@click.option("--no-submit", is_flag=True, help="Paste text but do not press Return.")
def reply_send(text: str, dry_run: bool, no_submit: bool) -> None:
    """Paste a reply into the focused WeCom chat and optionally send it."""
    _emit_or_fail(reply_core.send_text, text, dry_run=dry_run, submit=not no_submit)


@cli.command()
@click.option("--poll", default=5.0, show_default=True, type=click.FloatRange(min=1.0))
@click.option("--last", default=10, show_default=True, type=click.IntRange(min=1))
@click.option("--mode", default="approve", show_default=True, type=click.Choice(["dry-run", "approve", "auto"]))
@click.option("--once", is_flag=True, help="Run one polling iteration, useful for validation.")
@click.option("--scan-inbox", is_flag=True, help="Scan visible inbox rows instead of only current chat.")
@click.option("--scan-only", is_flag=True, help="Only scan inbox rows into the queue; never click chats.")
@click.option("--inbox-limit", default=12, show_default=True, type=click.IntRange(min=1))
@click.option("--process-existing", is_flag=True, help="Process already-visible changed rows on first scan.")
def watch(
    poll: float,
    last: int,
    mode: str,
    once: bool,
    scan_inbox: bool,
    scan_only: bool,
    inbox_limit: int,
    process_existing: bool,
) -> None:
    """Watch current chat, scan inbox, or enqueue inbox rows."""
    if scan_only:
        _emit_or_fail(worker_core.scan_loop, poll=poll, inbox_limit=inbox_limit, once=once)
    elif scan_inbox:
        _emit_or_fail(
            watcher.watch_inbox,
            poll=poll,
            last=last,
            mode=mode,
            once=once,
            inbox_limit=inbox_limit,
            process_existing=process_existing,
        )
    else:
        _emit_or_fail(watcher.watch_current, poll=poll, last=last, mode=mode, once=once)


@cli.group("queue")
def queue_group() -> None:
    """Inspect and manage the local reply queue."""


@queue_group.command("list")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "processing", "reading", "drafting", "ready", "approved", "sending", "done", "failed", "skipped"]),
)
@click.option("--limit", default=50, show_default=True, type=click.IntRange(min=1))
def queue_list(status: str | None, limit: int) -> None:
    """List queued conversations."""
    _emit_or_fail(lambda: {"ok": True, "items": state_core.list_queue(status=status, limit=limit)})


@queue_group.command("clear")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "processing", "reading", "drafting", "ready", "approved", "sending", "done", "failed", "skipped"]),
)
def queue_clear(status: str | None) -> None:
    """Clear queued conversations."""
    _emit_or_fail(lambda: {"ok": True, "deleted": state_core.clear_queue(status=status)})


@cli.group("wecom")
def wecom_group() -> None:
    """Manage WeCom sidebar identity bindings."""


@wecom_group.command("bind")
@click.option("--uid", required=True, help="WeCom external_user_id / UID from sidebar H5.")
@click.option("--customer-name", required=True, help="Visible conversation/customer name.")
@click.option("--display-name", default="", help="Optional display name.")
@click.option("--source", default="cli", show_default=True)
def wecom_bind(uid: str, customer_name: str, display_name: str, source: str) -> None:
    """Bind a WeCom UID to a visible customer name."""
    _emit_or_fail(
        lambda: {
            "ok": True,
            "binding": state_core.bind_wecom_customer(
                uid=uid,
                customer_name=customer_name,
                display_name=display_name,
                source=source,
                raw={"uid": uid, "customer_name": customer_name, "display_name": display_name, "source": source},
            ),
        }
    )


@wecom_group.command("lookup")
@click.option("--uid", default="", help="WeCom external_user_id / UID.")
@click.option("--customer-name", default="", help="Visible conversation/customer name.")
def wecom_lookup(uid: str, customer_name: str) -> None:
    """Lookup a WeCom UID/customer-name binding."""
    _emit_or_fail(lambda: {"ok": True, "binding": state_core.lookup_wecom_customer(uid=uid, customer_name=customer_name)})


@wecom_group.command("bindings")
@click.option("--limit", default=100, show_default=True, type=click.IntRange(min=1))
def wecom_bindings(limit: int) -> None:
    """List recent WeCom UID bindings."""
    _emit_or_fail(lambda: {"ok": True, "bindings": state_core.list_wecom_customer_bindings(limit=limit)})


@cli.command("sidebar")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8111, show_default=True, type=click.IntRange(min=1, max=65535))
def sidebar_cmd(host: str, port: int) -> None:
    """Run the local WeCom sidebar H5 and UID binding API."""
    sidebar_server.serve_sidebar(host=host, port=port)


@cli.command("review")
@click.option("--host", default=None, help="Bind host. Defaults to WECOM_REVIEW_HOST or 0.0.0.0.")
@click.option("--port", default=None, type=click.IntRange(min=1, max=65535), help="Bind port. Defaults to WECOM_REVIEW_PORT or 8122.")
def review_cmd(host: str | None, port: int | None) -> None:
    """Run the LAN reply review page and approval API."""
    review_server.serve_review(host=host, port=port)


@cli.command("agent-input")
@click.option("--last", default=12, show_default=True, type=click.IntRange(min=1))
@click.option("--output", "output_path", default="", help="Write payload to this JSON file. Defaults to local state dir.")
@click.option("--customer-name", default="", help="Override selected conversation/customer name.")
@click.option("--customer-uid", default="", help="Override WeCom external_user_id / UID.")
@click.option("--inbox-limit", default=30, show_default=True, type=click.IntRange(min=1))
@click.option("--no-capture-images", is_flag=True, help="Do not capture image attachments while reading.")
def agent_input_cmd(
    last: int,
    output_path: str,
    customer_name: str,
    customer_uid: str,
    inbox_limit: int,
    no_capture_images: bool,
) -> None:
    """Read current WeCom chat and export the payload that would be passed to Agent."""
    try:
        payload = agent_input_core.build_current_agent_input(
            last=last,
            capture_images=not no_capture_images,
            customer_name=customer_name,
            customer_uid=customer_uid,
            inbox_limit=inbox_limit,
        )
        path = agent_input_core.write_agent_input_file(
            payload,
            output_path or agent_input_core.default_output_path(),
        )
        payload["output_path"] = str(path)
        output.emit(payload, message=f"Agent input written to {path}")
    except Exception as exc:
        output.error(str(exc), code=type(exc).__name__)
        raise click.exceptions.Exit(1)


@cli.command("agent")
@click.option("--poll", default=0.5, show_default=True, type=click.FloatRange(min=0.1))
@click.option("--scan-interval", default=1.0, show_default=True, type=click.FloatRange(min=0.3))
@click.option("--inbox-limit", default=12, show_default=True, type=click.IntRange(min=1))
@click.option("--scan-pages", default=1, show_default=True, type=click.IntRange(min=1, max=8))
@click.option("--scroll-ticks", default=6, show_default=True, type=click.IntRange(min=1, max=120))
@click.option("--deep-scan-interval", default=30.0, show_default=True, type=click.FloatRange(min=1.0))
@click.option("--last", default=12, show_default=True, type=click.IntRange(min=1))
@click.option("--mode", default="dry-run", show_default=True, type=click.Choice(["dry-run", "auto", "review"]))
@click.option("--max-drafts", default=4, show_default=True, type=click.IntRange(min=1, max=16))
@click.option("--log-interval", default=5.0, show_default=True, type=click.FloatRange(min=1.0))
@click.option("--once", is_flag=True, help="Run one fast-agent tick then exit.")
@click.option("--read-only", is_flag=True, help="Read and log queued chats without calling AI.")
def agent_cmd(
    poll: float,
    scan_interval: float,
    inbox_limit: int,
    scan_pages: int,
    scroll_ticks: int,
    deep_scan_interval: float,
    last: int,
    mode: str,
    max_drafts: int,
    log_interval: float,
    once: bool,
    read_only: bool,
) -> None:
    """Fast agent: scan, read, draft concurrently, then send."""
    _emit_or_fail(
        agent_core.agent_loop,
        poll=poll,
        scan_interval=scan_interval,
        inbox_limit=inbox_limit,
        scan_pages=scan_pages,
        scroll_ticks=scroll_ticks,
        deep_scan_interval=deep_scan_interval,
        last=last,
        mode=mode,
        max_drafts=max_drafts,
        log_interval=log_interval,
        once=once,
        read_only=read_only,
    )


@cli.group("edge-channel")
def edge_channel_group() -> None:
    """Run the unattended central-channel client on this Mac."""


@cli.group("supervisor")
def supervisor_group() -> None:
    """Manage the controlled local edge-channel session."""


@supervisor_group.command("status")
def supervisor_status() -> None:
    _emit_or_fail(supervisor_core.status)


@supervisor_group.command("start")
@click.argument("service", type=click.Choice(["edge", "all"]))
def supervisor_start(service: str) -> None:
    _emit_or_fail(supervisor_core.action, service, "start")


@supervisor_group.command("stop")
@click.argument("service", type=click.Choice(["edge", "all"]))
def supervisor_stop(service: str) -> None:
    _emit_or_fail(supervisor_core.action, service, "stop")


@supervisor_group.command("restart")
@click.argument("service", type=click.Choice(["edge", "all"]))
def supervisor_restart(service: str) -> None:
    _emit_or_fail(supervisor_core.action, service, "restart")


@cli.group("runtime")
def runtime_group() -> None:
    """Read the redacted local runtime state for the native desktop client."""


@runtime_group.command("status")
@click.option("--limit", default=20, show_default=True, type=click.IntRange(min=1, max=20))
def runtime_status(limit: int) -> None:
    _emit_or_fail(runtime_state_core.snapshot, limit=limit)


@edge_channel_group.command("status")
def edge_channel_status() -> None:
    """Show the local durable spool; this never contacts the central service."""
    _emit_or_fail(edge_state_core.edge_status)


@edge_channel_group.command("run")
@click.option("--once", is_flag=True, help="Run one capture/upload/pull/send tick and exit.")
@click.option("--poll", default=1.0, show_default=True, type=click.FloatRange(min=0.1))
@click.option("--inbox-limit", default=30, show_default=True, type=click.IntRange(min=1, max=100))
@click.option("--last", default=100, show_default=True, type=click.IntRange(min=1, max=100))
def edge_channel_run(once: bool, poll: float, inbox_limit: int, last: int) -> None:
    """Capture external direct chats and execute centrally issued commands."""
    if once:
        _emit_or_fail(edge_worker_core.tick, inbox_limit=inbox_limit, last=last)
        return
    edge_worker_core.run_forever(poll_seconds=poll, inbox_limit=inbox_limit, last=last)


@cli.command()
def repl() -> None:
    """Interactive REPL."""
    from cli_anything.wecom_gui.utils.repl_skin import ReplSkin

    skin = ReplSkin("wecom_gui", version=__version__)
    skin.print_banner()
    pt_session = skin.create_prompt_session()
    while True:
        try:
            raw = skin.get_input(pt_session)
        except (EOFError, KeyboardInterrupt):
            skin.print_goodbye()
            return
        line = raw.strip()
        if not line:
            continue
        if line in {"exit", "quit", "q"}:
            skin.print_goodbye()
            return
        if line in {"help", "?"}:
            skin.help(
                {
                    "doctor": "Check GUI automation prerequisites",
                    "app focus": "Focus WeCom",
                    "inbox scan": "Scan visible conversation labels",
                    "chat open --name ...": "Open a visible chat",
                    "chat read --last 10": "Read current chat context",
                    "ai draft": "Draft a reply",
                    "reply send --text ...": "Paste/send a reply",
                    "watch --mode approve": "Poll current chat with approval",
                    "agent --mode auto": "Fast scan/read/draft/send agent",
                }
            )
            continue
        try:
            cli.main(args=shlex.split(line), standalone_mode=False)
        except SystemExit:
            pass
        except Exception as exc:
            skin.error(str(exc))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
