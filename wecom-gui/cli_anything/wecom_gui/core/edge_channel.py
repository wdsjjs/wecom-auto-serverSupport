"""HTTPS client for the UDA customer-service WeCom desktop channel."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


class ChannelError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChannelConfig:
    base_url: str
    device_id: str
    device_token: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "ChannelConfig":
        base_url = os.environ.get("WECOM_CHANNEL_BASE_URL", "").strip().rstrip("/")
        device_id = os.environ.get("WECOM_CHANNEL_DEVICE_ID", "").strip()
        device_token = os.environ.get("WECOM_CHANNEL_DEVICE_TOKEN", "").strip()
        if not base_url or not device_id or not device_token:
            raise ChannelError(
                "WECOM_CHANNEL_BASE_URL, WECOM_CHANNEL_DEVICE_ID, "
                "and WECOM_CHANNEL_DEVICE_TOKEN are required"
            )
        if not base_url.startswith("https://"):
            raise ChannelError("WECOM_CHANNEL_BASE_URL must use https://")
        return cls(
            base_url=base_url,
            device_id=device_id,
            device_token=device_token,
            timeout_seconds=max(1.0, float(os.environ.get("WECOM_CHANNEL_TIMEOUT_SECONDS", "10"))),
        )


def _path(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    return value if value.startswith("/") else f"/{value}"


class ChannelClient:
    def __init__(self, config: ChannelConfig, *, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.device_token}",
            "X-Wecom-Channel-Device-Id": self.config.device_id,
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}{path}"

    def _json_request(self, method: str, path: str, **kwargs: Any) -> dict:
        try:
            response = self.session.request(
                method,
                self._url(path),
                headers=self._headers(),
                timeout=kwargs.pop("timeout", self.config.timeout_seconds),
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ChannelError(str(exc)) from exc
        if response.status_code == 204:
            return {}
        if response.status_code >= 400:
            raise ChannelError(f"HTTP {response.status_code}: {response.text[:500]}")
        try:
            return response.json() if response.content else {}
        except ValueError as exc:
            raise ChannelError("channel returned non-JSON response") from exc

    def heartbeat(self) -> dict:
        return self._json_request(
            "POST",
            _path("WECOM_CHANNEL_HEARTBEAT_PATH", "/api/wecom-channel/edge/heartbeat"),
            json={"device_id": self.config.device_id},
        )

    def post_inbound(self, event: dict, media: list[dict]) -> dict:
        path = _path("WECOM_CHANNEL_INBOUND_PATH", "/api/wecom-channel/edge/inbound-events")
        upload_event = json.loads(json.dumps(event, ensure_ascii=False))
        files: list[tuple[str, tuple[str, object, str]]] = []
        opened: list[object] = []
        try:
            for index, item in enumerate(media):
                capture_path = Path(str(item.get("capture_path") or "")).expanduser()
                if not capture_path.is_file():
                    raise ChannelError(f"captured inbound image missing: {capture_path}")
                handle = capture_path.open("rb")
                opened.append(handle)
                files.append(("media", (capture_path.name, handle, "image/jpeg")))
                upload_event["message"]["media"][index].pop("capture_path", None)
            if not files:
                return self._json_request("POST", path, json=upload_event)
            return self._json_request(
                "POST",
                path,
                data={"event": json.dumps(upload_event, ensure_ascii=False)},
                files=files,
            )
        finally:
            for handle in opened:
                handle.close()

    def pull_command(self, *, wait_seconds: int = 25) -> dict | None:
        payload = self._json_request(
            "GET",
            _path("WECOM_CHANNEL_PULL_PATH", "/api/wecom-channel/edge/commands/pull"),
            params={"wait_seconds": max(1, min(25, int(wait_seconds)))},
            timeout=max(self.config.timeout_seconds, float(wait_seconds) + 5.0),
        )
        command = payload.get("command") if isinstance(payload, dict) else None
        if command is None and isinstance(payload, dict) and payload.get("command_id"):
            command = payload
        return command if isinstance(command, dict) else None

    def download_command_media(self, command_id: str, media_id: str, destination: Path) -> Path:
        path = _path("WECOM_CHANNEL_COMMAND_MEDIA_PATH", "/api/wecom-channel/edge/commands/{command_id}/media/{media_id}")
        url = self._url(path.format(command_id=command_id, media_id=media_id))
        try:
            response = self.session.get(url, headers=self._headers(), timeout=self.config.timeout_seconds, stream=True)
        except requests.RequestException as exc:
            raise ChannelError(str(exc)) from exc
        if response.status_code >= 400:
            raise ChannelError(f"HTTP {response.status_code}: cannot download command media")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    handle.write(chunk)
        return destination

    def post_command_result(self, command_id: str, lease_id: str, result: dict) -> dict:
        return self._json_request(
            "POST",
            _path("WECOM_CHANNEL_RESULT_PATH", "/api/wecom-channel/edge/commands/{command_id}/result").format(command_id=command_id),
            json={"command_id": command_id, "lease_id": lease_id, **result},
        )
