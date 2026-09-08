"""Best-effort central projection of redacted local runtime state."""

from __future__ import annotations

import threading

from cli_anything.wecom_gui.core import edge_channel, runtime_state


def publish(process: str, **kwargs) -> dict:
    local = runtime_state.report(process, **kwargs)

    def project() -> None:
        try:
            client = edge_channel.ChannelClient(edge_channel.ChannelConfig.from_env())
            client.post_runtime({"process": process, **local})
        except Exception:
            # Local telemetry is authoritative for the Mac UI. Reporting is
            # never a reason to interrupt GUI reading, drafting, or delivery.
            pass

    threading.Thread(target=project, name=f"wecom-runtime-{process}", daemon=True).start()
    return local
