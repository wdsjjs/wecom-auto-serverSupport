"""Local HTTP server used by the WeCom sidebar H5 page."""

from __future__ import annotations

import json
import os
import random
import string
import time
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse as urlparse_mod
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlparse

from cli_anything.wecom_gui import __version__
from cli_anything.wecom_gui.core import state
from cli_anything.wecom_gui.utils import macos_backend


_TOKEN_CACHE: dict[str, dict[str, float | str]] = {}


SIDEBAR_HTML = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>企微 UID 绑定</title>
    <style>
      body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #1f2329; }
      main { padding: 16px; }
      section { background: #fff; border: 1px solid #e5e6eb; border-radius: 8px; padding: 14px; margin-bottom: 12px; }
      h1 { font-size: 17px; margin: 0 0 12px; }
      label { display: block; font-size: 13px; color: #4e5969; margin: 10px 0 6px; }
      input { width: 100%; box-sizing: border-box; border: 1px solid #dcdfe6; border-radius: 6px; padding: 9px 10px; font-size: 14px; background: #fff; }
      button { margin-top: 12px; border: 0; border-radius: 6px; padding: 9px 12px; font-size: 14px; color: #fff; background: #165dff; cursor: pointer; }
      button.secondary { color: #1f2329; background: #f2f3f5; margin-left: 8px; }
      pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; background: #f7f8fa; border-radius: 6px; padding: 10px; max-height: 220px; overflow: auto; }
      .ok { color: #168f3f; }
      .err { color: #c62828; }
      .hint { color: #86909c; font-size: 12px; line-height: 1.5; }
    </style>
    <script src="https://wwcdn.weixin.qq.com/node/wework/wwopen/js/wwLogin-1.2.7.js"></script>
    <script src="https://res.wx.qq.com/open/js/jweixin-1.6.0.js"></script>
  </head>
  <body>
    <main>
      <section>
        <h1>企微 UID 绑定</h1>
        <label>客户名称</label>
        <input id="name" placeholder="例如：刘裕鑫" />
        <label>企微 UID / external_user_id</label>
        <input id="uid" placeholder="例如：wm_xxx 或 external_userid" />
        <button id="bind">绑定</button>
        <button id="refresh" class="secondary">刷新绑定</button>
        <button id="probe" class="secondary">测试本机连接</button>
      </section>
      <section>
        <div id="status">等待绑定。</div>
        <div class="hint">企微侧边栏会自动尝试读取当前客户 external_user_id；失败时可用 URL 参数或手动输入绑定。</div>
        <pre id="result"></pre>
      </section>
    </main>
    <script>
      const $ = (id) => document.getElementById(id);
      async function postJson(url, body) {
        const res = await fetch(url, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body)
        });
        return await res.json();
      }
      function setStatus(text, cls = "") {
        $("status").textContent = text;
        $("status").className = cls;
      }
      function mergeIdentity(payload) {
        const uid = payload.uid || payload.external_user_id || payload.externalUserId || payload.user_id || "";
        const name = payload.customer_name || payload.name || payload.display_name || payload.nickname || payload.remark || "";
        if (uid) $("uid").value = uid;
        if (name) $("name").value = name;
        return {uid: $("uid").value.trim(), customer_name: $("name").value.trim()};
      }
      async function bind(extra = {}) {
        setStatus("正在绑定...");
        try {
          const body = {
            customer_name: $("name").value.trim(),
            uid: $("uid").value.trim(),
            source: "wecom-sidebar-manual",
            ...extra
          };
          const data = await postJson("/api/wecom/bind-current", body);
          $("result").textContent = JSON.stringify(data, null, 2);
          if (data.ok && data.binding) {
            $("uid").value = data.binding.uid || $("uid").value;
            $("name").value = data.binding.customer_name || $("name").value;
            setStatus(`绑定成功：${data.binding.customer_name} / ${data.binding.uid}`, "ok");
          } else if (data.ok) {
            setStatus("等待企微 UID 或当前客户名称。");
          } else {
            setStatus(`绑定失败：${data.error || "unknown"}`, "err");
          }
        } catch (err) {
          setStatus(String(err), "err");
        }
      }
      async function bindCurrent(extra = {}) {
        const body = {
          customer_name: $("name").value.trim(),
          uid: $("uid").value.trim(),
          source: "wecom-sidebar-current",
          ...extra
        };
        const data = await postJson("/api/wecom/bind-current", body);
        $("result").textContent = JSON.stringify(data, null, 2);
        if (data.ok && data.binding) {
          $("uid").value = data.binding.uid || $("uid").value;
          $("name").value = data.binding.customer_name || $("name").value;
          setStatus(`自动绑定成功：${data.binding.customer_name} / ${data.binding.uid}`, "ok");
        } else if (data.ok) {
          setStatus("等待企微 UID 或当前客户名称。");
        } else {
          setStatus(`自动绑定失败：${data.error || "unknown"}`, "err");
        }
      }
      async function refresh() {
        const res = await fetch("/api/wecom/bindings");
        $("result").textContent = JSON.stringify(await res.json(), null, 2);
      }
      async function probeLocalhost() {
        const started = Date.now();
        try {
          const res = await fetch("http://127.0.0.1:8111/health", {
            method: "GET",
            cache: "no-store",
            mode: "cors"
          });
          const text = await res.text();
          let data = {};
          try { data = JSON.parse(text); } catch (err) { data = {raw: text}; }
          const result = {
            ok: res.ok,
            status: res.status,
            elapsed_ms: Date.now() - started,
            url: "http://127.0.0.1:8111/health",
            data
          };
          $("result").textContent = JSON.stringify({localhost_probe: result}, null, 2);
          setStatus(res.ok ? "企微侧边栏可以访问本机 8111。" : `本机 8111 返回异常：${res.status}`, res.ok ? "ok" : "err");
          return result;
        } catch (err) {
          const result = {
            ok: false,
            elapsed_ms: Date.now() - started,
            url: "http://127.0.0.1:8111/health",
            error: String(err)
          };
          $("result").textContent = JSON.stringify({localhost_probe: result}, null, 2);
          setStatus(`企微侧边栏访问本机 8111 失败：${String(err)}`, "err");
          return result;
        }
      }
      async function loadJsConfig() {
        const bridge = window.wx || window.jWeixin;
        if (!bridge || typeof bridge.config !== "function") return false;
        const pageUrl = location.href.split("#")[0];
        const data = await (await fetch(`/api/wecom/jsconfig?url=${encodeURIComponent(pageUrl)}`)).json();
        if (!data.ok) {
          $("result").textContent = JSON.stringify(data, null, 2);
          setStatus(`企微 JS-SDK 配置失败：${data.error || "unknown"}`, "err");
          return false;
        }
        await new Promise((resolve, reject) => {
          bridge.ready(resolve);
          bridge.error(reject);
          bridge.config({
            beta: true,
            debug: false,
            appId: data.corpId,
            timestamp: data.config.timestamp,
            nonceStr: data.config.nonceStr,
            signature: data.config.signature,
            jsApiList: ["getCurExternalContact"]
          });
        });
        if (data.agentConfig && typeof bridge.agentConfig === "function") {
          await new Promise((resolve, reject) => {
            bridge.agentConfig({
              corpid: data.corpId,
              agentid: data.agentId,
              timestamp: data.agentConfig.timestamp,
              nonceStr: data.agentConfig.nonceStr,
              signature: data.agentConfig.signature,
              jsApiList: ["getCurExternalContact"],
              success: resolve,
              fail: reject
            });
          });
        }
        return true;
      }
      function readParams() {
        const params = new URLSearchParams(location.search);
        const identity = mergeIdentity(Object.fromEntries(params.entries()));
        if (identity.uid) bindCurrent({source: "wecom-sidebar-url", raw_location: location.href});
      }
      async function autoBindFromWeCom() {
        const bridge = window.wx || window.jWeixin;
        if (!bridge || typeof bridge.invoke !== "function") {
          setStatus("等待绑定。未检测到企微 JS bridge，可使用 URL 参数或手动绑定。");
          return;
        }
        try {
          await loadJsConfig();
        } catch (err) {
          $("result").textContent = JSON.stringify({jsconfig_error: String(err)}, null, 2);
          setStatus(`企微 JS-SDK 初始化失败：${String(err)}`, "err");
        }
        bridge.invoke("getCurExternalContact", {}, (res) => {
          const payload = res || {};
          const uid = payload.userId || payload.user_id || payload.external_userid || payload.external_user_id || payload.externalUserId || "";
          const identity = mergeIdentity({...payload, uid});
          $("result").textContent = JSON.stringify({wecom_result: payload, parsed: identity}, null, 2);
          if (identity.uid) {
            bindCurrent({source: "wecom-sidebar-jsapi", wecom_result: payload});
          } else {
            bindCurrent({source: "wecom-sidebar-jsapi-missing-uid", wecom_result: payload});
            setStatus("未从企微侧边栏获取到 UID，可使用 URL 参数或手动绑定。", "err");
          }
        });
      }
      async function pollWeComIdentity() {
        await autoBindFromWeCom();
        setTimeout(pollWeComIdentity, 3000);
      }
      $("bind").addEventListener("click", bind);
      $("refresh").addEventListener("click", refresh);
      $("probe").addEventListener("click", probeLocalhost);
      readParams();
      probeLocalhost();
      pollWeComIdentity();
      refresh();
    </script>
  </body>
</html>
"""


def _json_response(handler: BaseHTTPRequestHandler, status_code: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Private-Network", "true")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def _json_get(url: str, *, timeout: int = 10) -> dict:
    with urlrequest.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wecom_api_base_url() -> str:
    return _env("WECOM_API_BASE_URL", "WEWORK_API_BASE_URL") or "https://qyapi.weixin.qq.com/cgi-bin"


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _wecom_access_token(*, secret: str = "", cache_key: str = "external_contact") -> str:
    corp_id = _env("WECOM_EXTERNAL_CONTACT_CORP_ID", "WECOM_CORP_ID", "WEWORK_CORP_ID")
    secret = secret or _env("WECOM_EXTERNAL_CONTACT_SECRET")
    if not corp_id or not secret:
        return ""
    now = time.time()
    cached = _TOKEN_CACHE.get(cache_key) or {}
    if cached.get("access_token") and float(cached.get("expires_at") or 0) > now:
        return str(cached["access_token"])
    query = urlparse_mod.urlencode({"corpid": corp_id, "corpsecret": secret})
    try:
        data = _json_get(f"{_wecom_api_base_url()}/gettoken?{query}")
    except Exception:
        return ""
    if int(data.get("errcode") or 0) != 0:
        return ""
    token = str(data.get("access_token") or "").strip()
    if not token:
        return ""
    expires_in = int(data.get("expires_in") or 7200)
    _TOKEN_CACHE[cache_key] = {"access_token": token, "expires_at": now + max(60, expires_in - 120)}
    return token


def _wecom_app_access_token() -> str:
    secret = _env("WECOM_APP_SECRET", "WEWORK_AGENT_SECRET")
    return _wecom_access_token(secret=secret, cache_key="app")


def _wecom_ticket(*, ticket_type: str, cache_key: str) -> str:
    token = _wecom_app_access_token()
    if not token:
        return ""
    now = time.time()
    cached = _TOKEN_CACHE.get(cache_key) or {}
    if cached.get("ticket") and float(cached.get("expires_at") or 0) > now:
        return str(cached["ticket"])
    if ticket_type:
        query = urlparse_mod.urlencode({"access_token": token, "type": ticket_type})
        url = f"{_wecom_api_base_url()}/ticket/get?{query}"
    else:
        query = urlparse_mod.urlencode({"access_token": token})
        url = f"{_wecom_api_base_url()}/get_jsapi_ticket?{query}"
    try:
        data = _json_get(url)
    except Exception:
        return ""
    if int(data.get("errcode") or 0) != 0:
        return ""
    ticket = str(data.get("ticket") or "").strip()
    if not ticket:
        return ""
    expires_in = int(data.get("expires_in") or 7200)
    _TOKEN_CACHE[cache_key] = {"ticket": ticket, "expires_at": now + max(60, expires_in - 120)}
    return ticket


def _nonce() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(16))


def _jsapi_signature(*, ticket: str, nonce: str, timestamp: int, url: str) -> str:
    plain = f"jsapi_ticket={ticket}&noncestr={nonce}&timestamp={timestamp}&url={url}"
    return sha1(plain.encode("utf-8")).hexdigest()


def _wecom_jsconfig(url: str) -> dict:
    corp_id = _env("WECOM_CORP_ID", "WEWORK_CORP_ID")
    agent_id = _env("WECOM_AGENT_ID", "WEWORK_AGENT_ID")
    if not corp_id:
        return {"ok": False, "error": "missing WECOM_CORP_ID/WEWORK_CORP_ID"}
    if not _env("WECOM_APP_SECRET", "WEWORK_AGENT_SECRET"):
        return {"ok": False, "error": "missing WECOM_APP_SECRET/WEWORK_AGENT_SECRET"}
    corp_ticket = _wecom_ticket(ticket_type="", cache_key="corp_jsapi_ticket")
    if not corp_ticket:
        return {"ok": False, "error": "failed_to_get_corp_jsapi_ticket"}
    timestamp = int(time.time())
    nonce = _nonce()
    config = {
        "timestamp": timestamp,
        "nonceStr": nonce,
        "signature": _jsapi_signature(ticket=corp_ticket, nonce=nonce, timestamp=timestamp, url=url),
    }
    agent_config = None
    if agent_id:
        agent_ticket = _wecom_ticket(ticket_type="agent_config", cache_key="agent_jsapi_ticket")
        if agent_ticket:
            agent_nonce = _nonce()
            agent_config = {
                "timestamp": timestamp,
                "nonceStr": agent_nonce,
                "signature": _jsapi_signature(ticket=agent_ticket, nonce=agent_nonce, timestamp=timestamp, url=url),
            }
    return {"ok": True, "corpId": corp_id, "agentId": agent_id, "config": config, "agentConfig": agent_config}


def _wecom_external_contact(external_user_id: str) -> dict:
    token = _wecom_access_token()
    if not token:
        return {}
    query = urlparse_mod.urlencode({"access_token": token, "external_userid": external_user_id})
    try:
        data = _json_get(f"https://qyapi.weixin.qq.com/cgi-bin/externalcontact/get?{query}")
    except Exception:
        return {}
    if int(data.get("errcode") or 0) != 0:
        return {}
    return data


def _name_from_wecom_contact(contact: dict) -> tuple[str, str]:
    external = contact.get("external_contact") if isinstance(contact.get("external_contact"), dict) else {}
    follow_users = contact.get("follow_user") if isinstance(contact.get("follow_user"), list) else []
    remarks = [
        str(item.get("remark") or "").strip()
        for item in follow_users
        if isinstance(item, dict) and str(item.get("remark") or "").strip()
    ]
    remark = remarks[0] if remarks else ""
    name = str(external.get("name") or "").strip()
    customer_name = remark or name
    display_name = name or remark or customer_name
    return customer_name, display_name


def _enrich_payload_from_wecom(payload: dict, uid: str) -> dict:
    contact = _wecom_external_contact(uid)
    if not contact:
        return payload
    customer_name, display_name = _name_from_wecom_contact(contact)
    if customer_name and not str(payload.get("customer_name") or "").strip():
        payload["customer_name"] = customer_name
    if display_name and not str(payload.get("display_name") or "").strip():
        payload["display_name"] = display_name
    payload["wecom_external_contact"] = contact
    return payload


def _bind_payload(payload: dict) -> dict:
    uid = str(payload.get("uid") or payload.get("external_user_id") or payload.get("user_id") or "").strip()
    customer_name = str(
        payload.get("customer_name")
        or payload.get("conversation_title")
        or payload.get("name")
        or payload.get("display_name")
        or payload.get("nickname")
        or payload.get("remark")
        or ""
    ).strip()
    display_name = str(payload.get("display_name") or payload.get("nickname") or customer_name).strip()
    binding = state.bind_wecom_customer(
        uid=uid,
        customer_name=customer_name,
        display_name=display_name,
        source=str(payload.get("source") or "sidebar"),
        raw=payload,
    )
    state.append_event({"type": "wecom_customer_bound", "binding": binding})
    return {"ok": True, "binding": binding}


def _selected_customer_name() -> str:
    row = macos_backend.selected_conversation_row(limit=30) or {}
    return str(row.get("title") or "").strip()


def _bind_current_payload(payload: dict) -> dict:
    payload = dict(payload)
    uid = str(payload.get("uid") or payload.get("external_user_id") or payload.get("user_id") or "").strip()
    if uid:
        payload["uid"] = uid
        payload = _enrich_payload_from_wecom(payload, uid)
    customer_name = str(
        payload.get("customer_name")
        or payload.get("conversation_title")
        or payload.get("name")
        or payload.get("display_name")
        or payload.get("nickname")
        or payload.get("remark")
        or _selected_customer_name()
        or ""
    ).strip()
    uid_only = False
    if uid and not customer_name:
        uid_only = True
        customer_name = uid
        payload["uid_only"] = True
    if customer_name:
        payload["customer_name"] = customer_name
    if not uid:
        return {
            "ok": True,
            "binding": None,
            "needs": {
                "uid": True,
                "customer_name": not bool(customer_name),
            },
        }
    result = _bind_payload(payload)
    if uid_only:
        result["needs"] = {"uid": False, "customer_name": True}
    return result


class SidebarHandler(BaseHTTPRequestHandler):
    server_version = "WeComSidebar/0.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_OPTIONS(self) -> None:
        _json_response(self, 200, {"ok": True})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            body = SIDEBAR_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/health":
            _json_response(self, 200, {"ok": True, "version": __version__})
            return
        if parsed.path == "/api/wecom/jsconfig":
            params = parse_qs(parsed.query)
            page_url = (params.get("url") or [""])[0]
            if not page_url:
                _json_response(self, 400, {"ok": False, "error": "url is required"})
                return
            _json_response(self, 200, _wecom_jsconfig(page_url))
            return
        if parsed.path == "/api/wecom/bind":
            params = parse_qs(parsed.query)
            binding = state.lookup_wecom_customer(
                uid=(params.get("uid") or params.get("external_user_id") or [""])[0],
                customer_name=(params.get("customer_name") or params.get("name") or [""])[0],
            )
            _json_response(self, 200, {"ok": True, "binding": binding})
            return
        if parsed.path == "/api/wecom/bindings":
            params = parse_qs(parsed.query)
            limit = int((params.get("limit") or ["100"])[0])
            _json_response(self, 200, {"ok": True, "bindings": state.list_wecom_customer_bindings(limit=limit)})
            return
        _json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/wecom/bind-current":
            try:
                payload = _read_json(self)
                result = _bind_current_payload(payload)
                print(
                    "[wecom-sidebar] bind-current "
                    f"source={payload.get('source') or ''} "
                    f"uid={'yes' if payload.get('uid') or payload.get('external_user_id') or payload.get('user_id') else 'no'} "
                    f"name={'yes' if payload.get('customer_name') or payload.get('conversation_title') or payload.get('name') else 'no'} "
                    f"bound={'yes' if result.get('binding') else 'no'} "
                    f"needs={json.dumps(result.get('needs') or {}, ensure_ascii=False)}",
                    flush=True,
                )
                _json_response(self, 200, result)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
            return
        if parsed.path != "/api/wecom/bind":
            _json_response(self, 404, {"ok": False, "error": "not_found"})
            return
        try:
            _json_response(self, 200, _bind_payload(_read_json(self)))
        except Exception as exc:
            _json_response(self, 400, {"ok": False, "error": str(exc)})


def serve_sidebar(*, host: str = "127.0.0.1", port: int = 8111) -> None:
    httpd = ThreadingHTTPServer((host, port), SidebarHandler)
    print(f"wecom sidebar: http://{host}:{port}", flush=True)
    httpd.serve_forever()
