"""HTTPS client for the UDA customer-service WeCom desktop channel."""

from __future__ import annotations

import json
import mimetypes
import os
from urllib.parse import quote
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
    local_test_mode: bool
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "ChannelConfig":
        base_url = os.environ.get("WECOM_CHANNEL_BASE_URL", "").strip().rstrip("/")
        device_id = os.environ.get("WECOM_CHANNEL_DEVICE_ID", "").strip()
        device_token = os.environ.get("WECOM_CHANNEL_DEVICE_TOKEN", "").strip()
        local_test_mode = os.environ.get("WECOM_CHANNEL_LOCAL_TEST_MODE", "").strip().lower() == "true"
        if not base_url or (not local_test_mode and (not device_id or not device_token)):
            raise ChannelError(
                "WECOM_CHANNEL_BASE_URL is required; WECOM_CHANNEL_DEVICE_ID and "
                "WECOM_CHANNEL_DEVICE_TOKEN are required unless WECOM_CHANNEL_LOCAL_TEST_MODE=true"
            )
        if not base_url.startswith("https://") and not (local_test_mode and base_url.startswith("http://")):
            raise ChannelError("WECOM_CHANNEL_BASE_URL must use https:// unless local test mode is enabled")
        return cls(
            base_url=base_url,
            device_id=device_id,
            device_token=device_token,
            local_test_mode=local_test_mode,
            timeout_seconds=max(1.0, float(os.environ.get("WECOM_CHANNEL_TIMEOUT_SECONDS", "10"))),
        )


def _path(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    return value if value.startswith("/") else f"/{value}"


class ChannelClient:
    def __init__(self, config: ChannelConfig, *, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.supports_deferred_media = False

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if not self.config.local_test_mode:
            headers["Authorization"] = f"Bearer {self.config.device_token}"
            headers["X-Wecom-Channel-Device-Id"] = self.config.device_id
        return headers

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
        result = self._json_request(
            "POST",
            _path("WECOM_CHANNEL_HEARTBEAT_PATH", "/api/wecom-channel/edge/heartbeat"),
            json={} if self.config.local_test_mode else {"device_id": self.config.device_id},
        )
        payload = result.get("data", result) if isinstance(result, dict) else {}
        self.supports_deferred_media = payload.get("capabilities", {}).get("deferredMediaV1") is True
        return result

    def register_message(self, event: dict) -> dict:
        result = self._json_request("POST", "/api/wecom-channel/edge/messages/register", json=event)
        result = result.get("data", result)
        if result.get("accepted") is not True:
            raise ChannelError("message registration was not acknowledged")
        return result

    def post_runtime(self, payload: dict) -> dict:
        return self._json_request(
            "POST",
            _path("WECOM_CHANNEL_RUNTIME_PATH", "/api/wecom-channel/edge/runtime"),
            json=payload,
            timeout=min(self.config.timeout_seconds, 2.0),
        )

    def post_inbound(self, event: dict, media: list[dict], *, attachment_only: bool = False) -> dict:
        path = (f'/api/wecom-channel/edge/messages/{quote(event["message"]["id"], safe="")}/media' if attachment_only else
                _path("WECOM_CHANNEL_INBOUND_PATH", "/api/wecom-channel/edge/inbound-events"))
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
                files.append(("media", (capture_path.name, handle,
                                       mimetypes.guess_type(capture_path.name)[0] or "application/octet-stream")))
                upload_event["message"]["media"][index].pop("capture_path", None)
            if not files:
                if attachment_only:
                    raise ChannelError("attachment completion requires image files")
                return self._json_request("POST", path, json=upload_event)
            result = self._json_request(
                "POST",
                path,
                data={"event": json.dumps(upload_event, ensure_ascii=False)},
                files=files,
            )
            if attachment_only:
                result = result.get("data", result)
                if result.get("accepted") is not True or result.get("message", {}).get("mediaState") != "ready":
                    raise ChannelError("attachment completion was not acknowledged")
            return result
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
