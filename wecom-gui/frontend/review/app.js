      const statusEl = document.getElementById("status");
      const wecomStatusEl = document.getElementById("wecomStatus");
      const noticeEl = document.getElementById("notice");
      const countsEl = document.getElementById("counts");
      const listEl = document.getElementById("list");
      const refreshBtn = document.getElementById("refresh");
      const baseTitle = document.title;
      let selectedStatus = "handoff";
      let lastImportantCounts = null;
      let isComposing = false;
      let lastEditAt = 0;
      const labels = {
        handoff: "转人工待处理",
        pending: "待读取",
        reading: "读取中",
        drafting: "AI处理中",
        ready: "待审核",
        issues: "待补充问题",
        approved: "已审核待发送",
        sending: "发送中",
        done: "已完成",
        skipped: "已拒绝/跳过",
        failed: "失败"
      };
      const visibleStatuses = ["handoff", "ready", "issues", "approved", "pending", "reading", "drafting", "sending", "done", "skipped", "failed"];
      const replyDrafts = new Map();
      const issueDrafts = new Map();
      const attachmentDrafts = new Map();
      function authHeaders(extra = {}) {
        return { ...extra };
      }
      function setStatus(text, isError = false) {
        statusEl.textContent = text;
        statusEl.className = isError ? "status error" : "status";
      }
      function setNotice(text) {
        const message = text || "";
        noticeEl.textContent = message;
        noticeEl.className = message ? "notice show" : "notice";
        document.title = message ? `有待处理消息 - ${baseTitle}` : baseTitle;
      }
      function escapeText(value) {
        return String(value ?? "").replace(/[&<>"']/g, ch => ({
          "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
        }[ch]));
      }
      function fmtTime(value) {
        const ts = Number(value || 0);
        if (!ts) return "";
        return new Date(ts * 1000).toLocaleString();
      }
      function customerHint(item) {
        const value = String(item.customer_key_label || "").trim();
        return value ? `会话 ${value}` : "";
      }
      async function api(path, options = {}) {
        const res = await fetch(path, {
          ...options,
          headers: authHeaders(options.headers || {})
        });
        const text = await res.text();
        let data = {};
        try { data = text ? JSON.parse(text) : {}; } catch (err) { data = {raw: text}; }
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        return data;
      }
      async function refreshWecomStatus() {
        try {
          const payload = await api("/api/review/wecom-status");
          const status = payload.status || {};
          const ok = !!status.ok;
          wecomStatusEl.textContent = ok ? `企微在线：${status.app_name || "已连接"}` : "企微离线/不可控";
          wecomStatusEl.className = ok ? "wecom-status ok" : "wecom-status bad";
          if (!ok && Array.isArray(status.notes) && status.notes.length) {
            wecomStatusEl.title = status.notes.join("；");
          } else {
            wecomStatusEl.title = "";
          }
        } catch (err) {
          wecomStatusEl.textContent = "企微状态未知";
          wecomStatusEl.className = "wecom-status bad";
          wecomStatusEl.title = String(err.message || err);
        }
      }
      function renderCounts(counts) {
        countsEl.innerHTML = visibleStatuses.map(key => (
          `<button class="count ${key === selectedStatus ? "active" : ""} ${key === "handoff" ? "handoff" : ""}" data-status="${key}">
            ${key === "handoff" ? "转人工待处理" : (labels[key] || key)}<strong>${Number(counts[key] || 0)}</strong>
          </button>`
        )).join("");
      }
      function activeReplyEditor() {
        const active = document.activeElement;
        if (!active || !active.matches || !active.matches("textarea.reply-editor")) return null;
        return active;
      }
      function activeIssueEditor() {
        const active = document.activeElement;
        if (!active || !active.matches || !active.matches("textarea.issue-editor")) return null;
        return active;
      }
      function shouldPauseListRefresh() {
        return !!activeReplyEditor() || !!activeIssueEditor() || isComposing || (Date.now() - lastEditAt < 500);
      }
      function captureEditorState() {
        const editor = activeReplyEditor() || activeIssueEditor();
        if (!editor) return null;
        return {
          id: String(editor.dataset.id || ""),
          field: editor.matches("textarea.issue-editor") ? "issue" : "reply",
          selectionStart: editor.selectionStart,
          selectionEnd: editor.selectionEnd,
          scrollTop: editor.scrollTop
        };
      }
      function captureConversationScrolls() {
        const result = {};
        document.querySelectorAll("article.item[data-id] .conversation").forEach(el => {
          const item = el.closest("article.item[data-id]");
          if (item) result[String(item.dataset.id)] = el.scrollTop;
        });
        return result;
      }
      function restoreEditorState(snapshot) {
        if (!snapshot || !snapshot.id) return;
        const selector = snapshot.field === "issue" ? "textarea.issue-editor" : "textarea.reply-editor";
        const editor = document.querySelector(`${selector}[data-id="${snapshot.id}"]`);
        if (!editor) return;
        editor.focus();
        try {
          editor.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd);
        } catch (err) {}
        editor.scrollTop = snapshot.scrollTop || 0;
      }
      function restoreConversationScrolls(scrolls) {
        if (!scrolls) return;
        document.querySelectorAll("article.item[data-id] .conversation").forEach(el => {
          const item = el.closest("article.item[data-id]");
          const key = item ? String(item.dataset.id) : "";
          if (Object.prototype.hasOwnProperty.call(scrolls, key)) {
            el.scrollTop = scrolls[key] || 0;
          } else {
            el.scrollTop = el.scrollHeight;
          }
        });
      }
      function observeImportantCounts(counts, options = {}) {
        const allowAutoSwitch = options.allowAutoSwitch !== false;
        const current = {
          handoff: Number(counts.handoff || 0),
          ready: Number(counts.ready || 0),
          handoffAttention: Number(counts.handoff_attention || 0)
        };
        if (lastImportantCounts) {
          const addedHandoff = current.handoff > lastImportantCounts.handoff;
          const addedReady = current.ready > lastImportantCounts.ready;
          const addedHandoffAttention = current.handoffAttention > lastImportantCounts.handoffAttention;
          if (addedHandoff || addedReady || addedHandoffAttention) {
            const parts = [];
            if (addedHandoff) parts.push(`转人工待处理 ${current.handoff} 条`);
            if (addedHandoffAttention) parts.push(`转人工新消息 ${current.handoffAttention} 条`);
            if (addedReady) parts.push(`待审核 ${current.ready} 条`);
            setNotice(`有新的待处理消息：${parts.join("，")}`);
            if (allowAutoSwitch) {
              if (addedHandoff || addedHandoffAttention) selectedStatus = "handoff";
              else if (selectedStatus !== "handoff") selectedStatus = "ready";
            }
          }
        }
        lastImportantCounts = current;
      }
      function roleClass(message) {
        const type = String(message.message_type || "");
        const role = String(message.role || "");
        if (type === "reply" || role.includes("客服")) return "service";
        if (type === "customer" || role.includes("用户") || role.includes("客户")) return "user";
        return "unknown";
      }
      function roleLabel(message) {
        const type = String(message.message_type || "");
        if (type === "reply") return "客服";
        if (type === "customer") return "客户";
        const role = String(message.role || "").trim();
        if (role.includes("客服")) return "客服";
        if (role.includes("用户") || role.includes("客户")) return "客户";
        return role || "消息";
      }
      function renderMessages(messages, fallbackText) {
        const source = Array.isArray(messages) ? messages.filter(item => String(item.text || "").trim() || (item.media || []).length) : [];
        if (!source.length) {
          return `<div class="box">${escapeText(fallbackText || "")}</div>`;
        }
        return `<div class="conversation">${source.map(message => `
          <div class="msg ${roleClass(message)}">
            <div class="msg-meta">${escapeText(roleLabel(message))}${message.time_text ? ` · ${escapeText(message.time_text)}` : ""}</div>
            <div class="bubble">
              ${escapeText(message.text || "")}
              ${renderMedia(message.media || [])}
            </div>
          </div>
        `).join("")}</div>`;
      }
      function renderMedia(mediaItems) {
        if (!Array.isArray(mediaItems) || !mediaItems.length) return "";
        return `<div class="media-list">${mediaItems.map(media => {
          if (media.capture_ok && media.url) {
            return `<img class="media-thumb" src="${escapeText(media.url)}" alt="聊天图片" loading="lazy" />`;
          }
          return `<div class="media-failed">图片未能截取${media.error ? `：${escapeText(media.error)}` : ""}</div>`;
        }).join("")}</div>`;
      }
      function attachmentsFor(item) {
        const key = String(item.id);
        if (attachmentDrafts.has(key)) return attachmentDrafts.get(key);
        return Array.isArray(item.reply_attachments) ? item.reply_attachments : [];
      }
      function renderAttachments(item) {
        return "";
      }
      function renderItems(items, options = {}) {
        const editorState = options.restoreEditor ? captureEditorState() : null;
        const conversationScrolls = captureConversationScrolls();
        if (!items.length) {
          listEl.innerHTML = `<div class="empty">暂无${escapeText(labels[selectedStatus] || selectedStatus)}记录。</div>`;
          restoreEditorState(editorState);
          return;
        }
        if (selectedStatus === "issues") {
          listEl.innerHTML = items.map(renderIssueTask).join("");
          restoreEditorState(editorState);
          return;
        }
        listEl.innerHTML = items.map(item => `
          <article class="item" data-id="${item.id}">
            <div class="item-head">
              <div>
                <div class="title">${escapeText(item.title || "未命名客户")}</div>
                <div class="status">
                  ${escapeText(labels[item.status] || item.status)} · #${item.id}
                  ${item.handoff_pending ? `<span class="badge">${item.handoff_type === "direct" ? "直接转人工" : "AI转人工"}</span>` : ""}
                  ${item.handoff_attention ? `<span class="badge attention">客户新消息</span>` : ""}
                </div>
                ${customerHint(item) ? `<div class="identity">${escapeText(customerHint(item))}</div>` : ""}
              </div>
              <div class="meta">${escapeText(fmtTime(item.updated_at))}</div>
            </div>
            <div class="label">会话上下文</div>
            ${renderMessages(item.messages, item.latest_text || item.preview || "")}
            ${renderReplySection(item)}
            ${item.handoff_pending ? `<div class="label">转人工原因</div><div class="box error">${escapeText(item.handoff_reason || "需要人工处理")}</div>` : ""}
            ${item.error ? `<div class="label">备注</div><div class="box error">${escapeText(item.error)}</div>` : ""}
            ${renderActions(item)}
          </article>
        `).join("");
        restoreConversationScrolls(conversationScrolls);
        restoreEditorState(editorState);
      }
      function softMergeItems(items) {
        renderItems(items || [], {restoreEditor: true});
      }
      function sourceLabel(value) {
        if (value === "handoff_reply") return "人工回复";
        if (value === "review_edit") return "审核改写";
        return value || "回复";
      }
      function renderIssueTask(item) {
        const key = String(item.id);
        const value = issueDrafts.has(key) ? issueDrafts.get(key) : "";
        return `
          <article class="item" data-id="${item.id}">
            <div class="item-head">
              <div>
                <div class="title">${escapeText(item.conversation || "未命名客户")}</div>
                <div class="status">${escapeText(sourceLabel(item.source))} · #${item.id}</div>
                ${item.conversation_key ? `<div class="identity">会话 ${escapeText(item.conversation_key)}</div>` : ""}
              </div>
              <div class="meta">${escapeText(fmtTime(item.updated_at))}</div>
            </div>
            ${item.original_reply ? `<div class="label">原回复</div><div class="box">${escapeText(item.original_reply)}</div>` : ""}
            <div class="label">最终发送内容</div>
            <div class="box reply">${escapeText(item.final_reply || "")}</div>
            <div class="label">补充问题原因</div>
            <textarea class="issue-editor" data-id="${item.id}" placeholder="填写这条回复需要改写或人工处理的原因">${escapeText(value)}</textarea>
            <div class="issue-actions">
              <button class="primary" data-action="complete-issue" data-id="${item.id}">提交记录</button>
            </div>
          </article>
        `;
      }
      function renderReplySection(item) {
        if (item.status === "ready") {
          return `
            <div class="label">回复内容</div>
            <textarea class="reply-editor" data-id="${item.id}" placeholder="${item.handoff_pending ? "输入客服回复，发送后会继续停留在转人工处理" : "可以修改 AI 生成内容，或直接写客服自己的回复"}">${escapeText(replyTextFor(item))}</textarea>
            ${renderAttachments(item)}
          `;
        }
        if (item.handoff_pending) return "";
        return `<div class="label">回复内容</div><div class="box reply">${escapeText(item.reply_text || "")}</div>`;
      }
      function renderActions(item) {
        if (item.handoff_pending && item.status === "ready" && item.handoff_waiting) {
          return `
            <div class="actions">
              <button class="danger" data-action="finish" data-id="${item.id}">结束会话</button>
              <span class="spacer"></span>
              <button class="primary" data-action="approve" data-id="${item.id}">发送回复</button>
            </div>
          `;
        }
        if (item.handoff_pending && item.status === "ready") {
          return `
            <div class="actions">
              <button class="danger" data-action="finish" data-id="${item.id}">结束会话</button>
              <span class="spacer"></span>
              <button class="primary" data-action="approve" data-id="${item.id}">发送回复</button>
            </div>
          `;
        }
        if (item.status === "ready") {
          return `
            <div class="actions">
              <span class="spacer"></span>
              <button class="primary" data-action="approve" data-id="${item.id}">发送</button>
            </div>
          `;
        }
        return "";
      }
      function replyTextFor(item) {
        const key = String(item.id);
        if (replyDrafts.has(key)) return replyDrafts.get(key);
        if (item.handoff_pending) return "";
        return item.reply_text || "";
      }
      async function refresh(options = {}) {
        const force = !!options.force;
        const preserveConversations = options.preserveConversations !== false && !force;
        try {
          refreshBtn.disabled = true;
          const counts = await api("/api/review/counts");
          refreshWecomStatus();
          const pauseList = shouldPauseListRefresh() && !force;
          observeImportantCounts(counts.counts || {}, {allowAutoSwitch: !pauseList});
          renderCounts(counts.counts || {});
          if (pauseList) {
            setStatus(`正在输入，已暂停列表刷新 ${new Date().toLocaleTimeString()}。`);
            return;
          }
          const items = await api(`/api/review/items?status=${encodeURIComponent(selectedStatus)}`);
          if (preserveConversations) {
            softMergeItems(items.items || []);
          } else {
            renderItems(items.items || [], {restoreEditor: true});
          }
          setStatus(`已刷新 ${new Date().toLocaleTimeString()}，通过后后台会复核再发送。`);
        } catch (err) {
          setStatus(String(err.message || err), true);
        } finally {
          refreshBtn.disabled = false;
        }
      }
      async function act(id, action) {
        const buttons = document.querySelectorAll(`button[data-id="${id}"]`);
        const editor = document.querySelector(`textarea.reply-editor[data-id="${id}"]`);
        const body = {};
        if (action === "approve" && editor) {
          const replyText = editor.value.trim();
          if (!replyText) {
            setStatus("回复内容不能为空。", true);
            return;
          }
          body.reply_text = editor.value;
          replyDrafts.set(String(id), editor.value);
        }
        buttons.forEach(btn => btn.disabled = true);
        try {
          await api(`/api/review/items/${id}/${action}`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body)
          });
          if (action === "approve" || action === "finish") {
            replyDrafts.delete(String(id));
            attachmentDrafts.delete(String(id));
          }
          if (action === "finish") setNotice("");
          await refresh({force: true});
        } catch (err) {
          setStatus(String(err.message || err), true);
          buttons.forEach(btn => btn.disabled = false);
        }
      }
      listEl.addEventListener("click", (event) => {
        const button = event.target.closest("button[data-action]");
        if (!button) return;
        if (button.dataset.action === "delete-attachment") {
          return;
        }
        if (button.dataset.action === "complete-issue") {
          completeIssue(button.dataset.id);
          return;
        }
        act(button.dataset.id, button.dataset.action);
      });
      async function completeIssue(id) {
        const editor = document.querySelector(`textarea.issue-editor[data-id="${id}"]`);
        const text = (editor ? editor.value : "").trim();
        if (!text) {
          setStatus("补充原因不能为空。", true);
          return;
        }
        issueDrafts.set(String(id), text);
        const buttons = document.querySelectorAll(`button[data-id="${id}"]`);
        buttons.forEach(btn => btn.disabled = true);
        try {
          await api(`/api/review/issues/${id}/complete`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({issue_text: text})
          });
          issueDrafts.delete(String(id));
          setStatus("问题原因已记录。");
          await refresh({force: true});
        } catch (err) {
          setStatus(String(err.message || err), true);
          buttons.forEach(btn => btn.disabled = false);
        }
      }
      async function uploadAttachment(id, file) {
        const reader = new FileReader();
        const dataUrl = await new Promise((resolve, reject) => {
          reader.onload = () => resolve(String(reader.result || ""));
          reader.onerror = () => reject(reader.error || new Error("read file failed"));
          reader.readAsDataURL(file);
        });
        const payload = await api(`/api/review/items/${id}/attachments`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({filename: file.name, content_type: file.type, data_url: dataUrl})
        });
        const attachments = (payload.item && payload.item.reply_attachments) || [];
        attachmentDrafts.set(String(id), attachments);
        setStatus(`已添加图片 ${file.name}`);
        await refresh({force: true});
      }
      async function deleteAttachment(id, attachmentId) {
        const payload = await api(`/api/review/items/${id}/attachments/${encodeURIComponent(attachmentId)}`, {
          method: "DELETE"
        });
        const attachments = (payload.item && payload.item.reply_attachments) || [];
        attachmentDrafts.set(String(id), attachments);
        await refresh({force: true});
      }
      listEl.addEventListener("change", (event) => {
        const input = event.target.closest("input[data-upload-id]");
        if (!input || !input.files || !input.files.length) return;
      });
      listEl.addEventListener("input", (event) => {
        const editor = event.target.closest("textarea[data-id]");
        if (!editor) return;
        lastEditAt = Date.now();
        if (editor.classList.contains("issue-editor")) {
          issueDrafts.set(String(editor.dataset.id), editor.value);
        } else {
          replyDrafts.set(String(editor.dataset.id), editor.value);
        }
      });
      listEl.addEventListener("compositionstart", (event) => {
        if (!event.target.closest("textarea[data-id]")) return;
        isComposing = true;
        lastEditAt = Date.now();
      });
      listEl.addEventListener("compositionend", (event) => {
        const editor = event.target.closest("textarea[data-id]");
        isComposing = false;
        lastEditAt = Date.now();
        if (editor && editor.classList.contains("issue-editor")) {
          issueDrafts.set(String(editor.dataset.id), editor.value);
        } else if (editor) {
          replyDrafts.set(String(editor.dataset.id), editor.value);
        }
      });
      countsEl.addEventListener("click", (event) => {
        const button = event.target.closest("button[data-status]");
        if (!button) return;
        selectedStatus = button.dataset.status || "ready";
        refresh({force: true});
      });
      refreshBtn.addEventListener("click", () => refresh({force: true}));
      refreshWecomStatus();
      refresh();
      setInterval(refreshWecomStatus, 5000);
      setInterval(refresh, 2000);
