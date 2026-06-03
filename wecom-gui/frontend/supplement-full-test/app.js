const statusEl = document.getElementById("status");
const customerEl = document.getElementById("customer");
const messageEl = document.getElementById("message");
const newUserEl = document.getElementById("newUser");
const sendBtn = document.getElementById("send");
const resetBtn = document.getElementById("reset");
const conversationEl = document.getElementById("conversation");
const stateEl = document.getElementById("state");
const logsEl = document.getElementById("logs");
let requestSeq = 0;
let sendController = null;
let refreshTimer = null;

function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;"
  }[ch]));
}

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.style.color = isError ? "#b42318" : "#596170";
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})}
  });
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch (err) {
    data = {raw: text};
  }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

function customer() {
  return customerEl.value.trim() || "补剂完整问答客户";
}

function renderMessages(messages) {
  const source = Array.isArray(messages) ? messages : [];
  if (!source.length) {
    conversationEl.innerHTML = "<div class=\"meta\">暂无消息</div>";
    return;
  }
  conversationEl.innerHTML = source.map(item => {
    const type = String(item.message_type || "");
    const role = type === "reply" ? "客服" : type === "system" ? "系统" : "客户";
    return `
      <div class="msg ${type === "reply" ? "reply" : "customer"}">
        <div class="meta">${escapeText(role)}</div>
        <div class="bubble">${escapeText(item.text || "")}</div>
      </div>
    `;
  }).join("");
  conversationEl.scrollTop = conversationEl.scrollHeight;
}

function renderState(payload) {
  const state = payload.state || {};
  const job = payload.job || {};
  const rows = [
    ["customer_key", payload.customer_key || ""],
    ["trace_id", state.trace_id || ""],
    ["stage", state.stage || ""],
    ["pending_next_stage", state.pending_next_stage || ""],
    ["digging_count", state.digging_count ?? ""],
    ["selected_needs", Array.isArray(state.selected_needs) ? state.selected_needs.join("、") : ""],
    ["profile_opt_out", state.known_profile?.profile_opt_out ? "true" : "false"],
    ["known_profile", JSON.stringify(state.known_profile || {})],
    ["job_status", job.status || ""],
    ["reply_source", job.reply_source || ""]
  ];
  stateEl.innerHTML = rows.map(([key, value]) => `
    <div class="kv"><span>${escapeText(key)}</span><strong>${escapeText(value)}</strong></div>
  `).join("");
}

function renderLogs(logs) {
  const source = Array.isArray(logs) ? logs.slice().reverse() : [];
  if (!source.length) {
    logsEl.innerHTML = "<div class=\"meta\">暂无链路日志</div>";
    return;
  }
  logsEl.innerHTML = source.map(item => {
    const created = item.created_at ? new Date(Number(item.created_at) * 1000).toLocaleTimeString() : "";
    const details = item.details || {};
    return `
      <div class="log">
        <div>
          <div class="event">${escapeText(item.event_type || "")}</div>
          <div class="meta">${escapeText(created)}</div>
        </div>
        <div>${escapeText(item.stage || "")}</div>
        <div class="details">${escapeText(JSON.stringify(details, null, 2))}</div>
      </div>
    `;
  }).join("");
}

function render(payload) {
  renderMessages(payload.messages || []);
  renderState(payload);
  renderLogs(payload.logs || []);
}

async function refresh({silent = false} = {}) {
  const payload = await api(`/api/supplement-full-test/status?customer=${encodeURIComponent(customer())}`);
  render(payload);
  if (!silent) setStatus("已加载");
}

function stopAutoRefresh() {
  if (!refreshTimer) return;
  clearInterval(refreshTimer);
  refreshTimer = null;
}

function startAutoRefresh() {
  if (refreshTimer) return;
  refreshTimer = setInterval(() => {
    refresh({silent: true}).catch(err => setStatus(err.message || String(err), true));
  }, 1500);
}

async function send() {
  const text = messageEl.value.trim();
  const isNewUser = Boolean(newUserEl?.checked);
  if (!text && !isNewUser) {
    setStatus("请输入客户消息", true);
    return;
  }
  const seq = ++requestSeq;
  if (sendController) sendController.abort();
  sendController = new AbortController();
  sendBtn.disabled = true;
  setStatus(isNewUser ? "正在走新用户欢迎 + 补剂流程..." : "正在走后端真实 Agent...");
  try {
    const payload = await api("/api/supplement-full-test/send", {
      method: "POST",
      signal: sendController.signal,
      body: JSON.stringify({
        customer: customer(),
        text,
        new_user: isNewUser
      })
    });
    if (seq !== requestSeq) return;
    if (payload.stale) {
      render(payload);
      setStatus("旧请求已忽略");
      return;
    }
    render(payload);
    messageEl.value = "";
    if (payload.skipped) {
      setStatus("老用户非补剂问题：已记录，未触发补剂 Agent");
    } else {
      setStatus("后端真实链路已返回");
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    if (seq !== requestSeq) return;
    setStatus(err.message || String(err), true);
  } finally {
    if (seq === requestSeq) {
      sendBtn.disabled = false;
      sendController = null;
    }
  }
}

async function reset() {
  requestSeq++;
  if (sendController) {
    sendController.abort();
    sendController = null;
  }
  sendBtn.disabled = false;
  resetBtn.disabled = true;
  setStatus("正在重置...");
  try {
    const payload = await api("/api/supplement-full-test/reset", {
      method: "POST",
      body: JSON.stringify({customer: customer()})
    });
    render(payload);
    setStatus("已重置");
  } catch (err) {
    setStatus(err.message || String(err), true);
  } finally {
    resetBtn.disabled = false;
  }
}

sendBtn.addEventListener("click", send);
resetBtn.addEventListener("click", reset);
messageEl.addEventListener("keydown", event => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    send();
  }
});
customerEl.addEventListener("change", refresh);
startAutoRefresh();
refresh().catch(err => setStatus(err.message || String(err), true));
