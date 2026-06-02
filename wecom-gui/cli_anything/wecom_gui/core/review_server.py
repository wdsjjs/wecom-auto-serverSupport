"""Local/LAN review server for approving AI reply drafts."""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import time
import uuid
import base64
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cli_anything.wecom_gui import __version__
from cli_anything.wecom_gui.core import state
from cli_anything.wecom_gui.core.text import clean_customer_reply_text, clean_history_message_text
from cli_anything.wecom_gui.utils import macos_backend


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8122
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


REVIEW_HTML = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>客服回复审核</title>
    <style>
      :root { color-scheme: light; }
      body {
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #f5f7fb;
        color: #1f2329;
      }
      header {
        position: sticky;
        top: 0;
        z-index: 2;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 14px 18px;
        background: #ffffff;
        border-bottom: 1px solid #e5e6eb;
      }
      h1 { margin: 0; font-size: 18px; font-weight: 650; }
      main { max-width: 1280px; margin: 0 auto; padding: 18px; }
      button {
        border: 1px solid #c9cdd4;
        border-radius: 6px;
        padding: 8px 12px;
        background: #fff;
        color: #1f2329;
        cursor: pointer;
        font-size: 14px;
      }
      button.primary { border-color: #165dff; background: #165dff; color: #fff; }
      button.secondary { border-color: #165dff; color: #165dff; }
      button.danger { border-color: #d92d20; color: #d92d20; }
      button:disabled { opacity: .55; cursor: not-allowed; }
      .toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .status { color: #4e5969; font-size: 13px; }
      .wecom-status {
        border: 1px solid #c9cdd4;
        border-radius: 999px;
        padding: 4px 9px;
        color: #4e5969;
        background: #fff;
        font-size: 12px;
      }
      .wecom-status.ok { border-color: #16a34a; color: #166534; background: #f0fdf4; }
      .wecom-status.bad { border-color: #dc2626; color: #b91c1c; background: #fef2f2; }
      .notice {
        display: none;
        margin: 0 0 12px;
        border: 1px solid #fed7aa;
        border-radius: 6px;
        padding: 10px 12px;
        color: #c2410c;
        background: #fff7ed;
        font-size: 14px;
      }
      .notice.show { display: block; }
      .counts { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
      .count {
        background: #fff;
        border: 1px solid #e5e6eb;
        border-radius: 6px;
        padding: 8px 10px;
        font-size: 13px;
        color: #4e5969;
      }
      .count.active { border-color: #165dff; color: #165dff; background: #f2f6ff; }
      .count.handoff { border-color: #f97316; color: #c2410c; background: #fff7ed; }
      .count strong { color: #1f2329; margin-left: 4px; }
      .list { display: grid; gap: 12px; }
      .item {
        background: #fff;
        border: 1px solid #e5e6eb;
        border-radius: 8px;
        padding: 14px;
      }
      .item-head {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 10px;
      }
      .title { font-weight: 650; font-size: 16px; overflow-wrap: anywhere; }
      .meta { color: #86909c; font-size: 12px; white-space: nowrap; }
      .identity { margin-top: 4px; color: #86909c; font-size: 12px; }
      .label { color: #4e5969; font-size: 12px; margin: 12px 0 4px; }
      .box {
        border: 1px solid #e5e6eb;
        border-radius: 6px;
        background: #f7f8fa;
        padding: 10px;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        line-height: 1.5;
      }
      .reply { background: #f2f6ff; border-color: #c9dcff; }
      .waiting { color: #7c2d12; background: #fff7ed; border-color: #fed7aa; }
      textarea.reply-editor {
        box-sizing: border-box;
        width: 100%;
        min-height: 190px;
        resize: vertical;
        border: 1px solid #c9dcff;
        border-radius: 6px;
        background: #f2f6ff;
        color: #1f2329;
        padding: 10px;
        font: inherit;
        line-height: 1.5;
      }
      .conversation {
        border: 1px solid #e5e6eb;
        border-radius: 6px;
        background: #f7f8fa;
        padding: 10px;
        display: grid;
        gap: 8px;
        max-height: 260px;
        overflow-y: auto;
      }
      .msg {
        display: grid;
        gap: 3px;
        max-width: min(640px, 84%);
      }
      .msg.service { justify-self: end; }
      .msg-meta { color: #86909c; font-size: 12px; }
      .bubble {
        border: 1px solid #e5e6eb;
        border-radius: 8px;
        padding: 8px 10px;
        background: #fff;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        line-height: 1.5;
      }
      .msg.service .bubble {
        background: #f2f6ff;
        border-color: #c9dcff;
      }
      .msg.unknown .bubble { color: #4e5969; }
      textarea.issue-editor {
        box-sizing: border-box;
        width: 100%;
        min-height: 110px;
        resize: vertical;
        border: 1px solid #e5e6eb;
        border-radius: 6px;
        background: #fff;
        color: #1f2329;
        padding: 8px 10px;
        font: inherit;
        line-height: 1.5;
      }
      .issue-editor { background: #fff; }
      .issue-actions { display: flex; justify-content: flex-end; margin-top: 8px; }
      .media-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
      .media-thumb {
        max-width: 180px;
        max-height: 180px;
        border: 1px solid #e5e6eb;
        border-radius: 6px;
        background: #fff;
      }
      .media-failed {
        border: 1px dashed #d92d20;
        border-radius: 6px;
        padding: 8px 10px;
        color: #b42318;
        background: #fff5f5;
        font-size: 12px;
      }
      .attachments { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 8px; }
      .attachment {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        border: 1px solid #c9dcff;
        border-radius: 6px;
        padding: 5px 8px;
        color: #1d4ed8;
        background: #eff6ff;
        font-size: 12px;
      }
      .attachment button { padding: 2px 6px; font-size: 12px; }
      .upload input { display: none; }
      .badge {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        border-radius: 999px;
        padding: 2px 8px;
        font-size: 12px;
        border: 1px solid #fed7aa;
        color: #c2410c;
        background: #fff7ed;
      }
      .badge.attention { border-color: #dc2626; color: #b91c1c; background: #fef2f2; }
      .actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
      .actions .spacer { flex: 1; }
      .empty {
        border: 1px dashed #c9cdd4;
        border-radius: 8px;
        padding: 38px 18px;
        text-align: center;
        color: #86909c;
        background: #fff;
      }
      .error { color: #b42318; }
      @media (max-width: 720px) {
        header { align-items: flex-start; flex-direction: column; }
        main { padding: 12px; }
        .item-head { flex-direction: column; }
        .meta { white-space: normal; }
        .actions { justify-content: stretch; }
        .actions button { flex: 1; }
      }
    </style>
  </head>
  <body>
    <header>
      <h1>客服回复审核</h1>
      <div class="toolbar">
        <span class="wecom-status" id="wecomStatus">企微检测中...</span>
        <span class="status" id="status">加载中...</span>
        <button id="refresh">刷新</button>
      </div>
    </header>
    <main>
      <div class="notice" id="notice"></div>
      <div class="counts" id="counts"></div>
      <div class="list" id="list"></div>
    </main>
    <script>
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
      function captureConversationHtml() {
        const result = {};
        document.querySelectorAll("article.item[data-id] .conversation").forEach(el => {
          const item = el.closest("article.item[data-id]");
          if (item) {
            const key = String(item.dataset.id);
            result[key] = {html: el.innerHTML, scrollTop: el.scrollTop};
          }
        });
        return result;
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
        const attachments = attachmentsFor(item);
        if (!item.handoff_pending || item.status !== "ready") return "";
        const chips = attachments.map(attachment => `
          <span class="attachment">
            ${escapeText(attachment.name || "图片")}
            <button type="button" data-action="delete-attachment" data-id="${item.id}" data-attachment-id="${escapeText(attachment.id)}">移除</button>
          </span>
        `).join("");
        return `
          <div class="attachments" data-attachments-for="${item.id}">
            ${chips}
            <label class="attachment upload">
              添加图片
              <input type="file" accept="image/png,image/jpeg" data-upload-id="${item.id}" />
            </label>
          </div>
        `;
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
        const preservedConversations = captureConversationHtml();
        renderItems(items || [], {restoreEditor: true});
        document.querySelectorAll("article.item[data-id] .conversation").forEach(el => {
          const item = el.closest("article.item[data-id]");
          const key = item ? String(item.dataset.id) : "";
          const preserved = preservedConversations[key];
          if (!preserved) return;
          el.innerHTML = preserved.html;
          el.scrollTop = preserved.scrollTop || 0;
        });
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
        if (item.handoff_pending && item.handoff_waiting) return "";
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
          const attachments = attachmentDrafts.get(String(id)) || [];
          if (!replyText && !attachments.length) {
            setStatus("回复内容或图片不能为空。", true);
            return;
          }
          body.reply_text = editor.value;
          body.attachment_ids = attachments.map(item => item.id).filter(Boolean);
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
          deleteAttachment(button.dataset.id, button.dataset.attachmentId);
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
        uploadAttachment(input.dataset.uploadId, input.files[0]).catch(err => setStatus(String(err.message || err), true));
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
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    handler.send_header("Access-Control-Allow-Private-Network", "true")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    value = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _send_file_response(handler: BaseHTTPRequestHandler, path: Path) -> None:
    try:
        body = path.read_bytes()
    except OSError:
        _json_response(handler, 404, {"ok": False, "error": "media_not_found"})
        return
    if not macos_backend.validate_image_file(path):
        _json_response(handler, 415, {"ok": False, "error": "invalid_image_file"})
        return
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not content_type.startswith("image/"):
        _json_response(handler, 415, {"ok": False, "error": "unsupported_media_type"})
        return
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "private, max-age=300")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _attachment_dir() -> Path:
    path = state.state_dir() / "review-attachments"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _attachment_ext(filename: str, content_type: str = "") -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"}:
        return suffix
    guessed = mimetypes.guess_extension(content_type or "")
    if guessed in {".png", ".jpg", ".jpeg"}:
        return guessed
    return ".png"


def _sanitize_attachment(item: dict) -> dict:
    return {
        "id": str(item.get("id") or uuid.uuid4().hex),
        "type": "image",
        "name": str(item.get("name") or "image"),
        "path": str(item.get("path") or ""),
        "size": int(item.get("size") or 0),
        "content_type": str(item.get("content_type") or "image/png"),
    }


def _current_attachments(job_id: int) -> list[dict]:
    item = state.get_job(job_id) or {}
    attachments = item.get("reply_attachments") if isinstance(item.get("reply_attachments"), list) else []
    return [_sanitize_attachment(attachment) for attachment in attachments if isinstance(attachment, dict)]


def _store_attachment(job_id: int, *, filename: str, content_type: str, body: bytes) -> dict:
    if not body:
        raise ValueError("empty_attachment")
    if len(body) > MAX_ATTACHMENT_BYTES:
        raise ValueError("attachment_too_large")
    attachment_id = uuid.uuid4().hex
    ext = _attachment_ext(filename, content_type)
    path = _attachment_dir() / f"review-{job_id}-{attachment_id}{ext}"
    path.write_bytes(body)
    if not macos_backend.validate_image_file(path):
        try:
            path.unlink()
        except OSError:
            pass
        raise ValueError("invalid_image_file")
    return {
        "id": attachment_id,
        "type": "image",
        "name": filename or path.name,
        "path": str(path),
        "size": len(body),
        "content_type": mimetypes.guess_type(path.name)[0] or content_type or "image/png",
    }


def _context_latest_text(item: dict) -> str:
    context_raw = str(item.get("context_json") or "").strip()
    if not context_raw:
        return ""
    try:
        context = json.loads(context_raw)
    except json.JSONDecodeError:
        return ""
    latest = context.get("latest") if isinstance(context, dict) else None
    if not isinstance(latest, dict):
        return ""
    return str(latest.get("content") or latest.get("text") or "").strip()


def _fallback_messages(item: dict, latest_text: str) -> list[dict]:
    text = latest_text or str(item.get("preview") or "").strip()
    if not text:
        return []
    return [
        {
            "conversation_key": item.get("conversation_key") or "",
            "job_id": item.get("id"),
            "message_hash": item.get("last_message_hash") or "",
            "seq": 0,
            "role": "用户",
            "message_type": "customer",
            "text": text,
            "time_text": item.get("time_text") or "",
            "source": "context_json" if latest_text else "preview",
            "role_confidence": "",
            "media": [],
            "raw": {},
        }
    ]


def _sanitize_media(message: dict) -> list[dict]:
    media_items = message.get("media") if isinstance(message.get("media"), list) else []
    sanitized: list[dict] = []
    message_id = int(message.get("id") or 0)
    for index, media in enumerate(media_items):
        if not isinstance(media, dict):
            continue
        capture_ok = bool(media.get("capture_path")) and media.get("capture_ok", True) is not False
        item = {
            "type": str(media.get("type") or "image"),
            "capture_ok": capture_ok,
            "error": str(media.get("error") or ""),
            "source": str(media.get("source") or ""),
        }
        if capture_ok and message_id:
            item["url"] = f"/api/review/media/{message_id}/{index}"
        sanitized.append(item)
    return sanitized


def _message_type(message: dict) -> str:
    explicit = str(message.get("message_type") or "").strip().lower()
    if explicit in {"customer", "reply", "unknown"}:
        return explicit
    role = str(message.get("role") or "").strip().lower()
    if role in {"customer", "user", "human"} or "用户" in role or "客户" in role:
        return "customer"
    if (
        role in {"reply", "service", "assistant", "agent", "staff"}
        or "客服" in role
        or "坐席" in role
    ):
        return "reply"
    return "unknown"


def _position_message_type(message: dict) -> str:
    raw = message.get("raw") if isinstance(message.get("raw"), dict) else {}
    right = message.get("right")
    if right is None:
        right = raw.get("right")
    threshold = raw.get("role_threshold") or message.get("role_threshold") or 760
    try:
        right_value = float(right)
        threshold_value = float(threshold)
    except (TypeError, ValueError):
        return ""
    return "reply" if right_value >= threshold_value else "customer"


def _classified_messages(messages: list[dict]) -> list[dict]:
    items: list[dict] = []
    for message in messages:
        message_type = _message_type(message)
        position_type = _position_message_type(message)
        if position_type and str(message.get("role_confidence") or "") in {"medium", "high"}:
            message_type = position_type
        role = "客服" if message_type == "reply" else "用户" if message_type == "customer" else message.get("role", "")
        items.append(
            {
                **message,
                "role": role,
                "message_type": message_type,
                "media": _sanitize_media(message),
                "text": clean_history_message_text(message.get("text") or ""),
            }
        )
    return items


def _public_reply_attachments(item: dict) -> list[dict]:
    attachments = item.get("reply_attachments") if isinstance(item.get("reply_attachments"), list) else []
    public: list[dict] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        public.append(
            {
                "id": str(attachment.get("id") or ""),
                "type": str(attachment.get("type") or "image"),
                "name": str(attachment.get("name") or "image"),
                "size": int(attachment.get("size") or 0),
                "content_type": str(attachment.get("content_type") or ""),
            }
        )
    return public


def _review_item(item: dict) -> dict:
    latest_text = _context_latest_text(item)
    conversation_key = str(item.get("conversation_key") or "").strip()
    messages = state.list_conversation_messages(conversation_key=conversation_key) if conversation_key else []
    if not messages:
        messages = _fallback_messages(item, latest_text)
    messages = _classified_messages(messages)
    error_code = str(item.get("error") or "")
    handoff_waiting = error_code == state.HANDOFF_WAITING_ERROR
    handoff_attention = error_code == state.HANDOFF_NEW_MESSAGE_ERROR
    visible_error = "" if handoff_waiting or handoff_attention else item.get("error") or ""
    key_label = conversation_key
    if key_label.startswith("visible:"):
        key_label = key_label.split(":", 1)[1][:10]
    elif len(key_label) > 18:
        key_label = key_label[:18]
    return {
        "id": item.get("id"),
        "conversation_key": conversation_key,
        "customer_key_label": key_label,
        "title": item.get("title") or "",
        "status": item.get("status") or "",
        "preview": item.get("preview") or "",
        "latest_text": latest_text,
        "messages": messages,
        "reply_text": clean_customer_reply_text(item.get("reply_text") or ""),
        "reply_attachments": _public_reply_attachments(item),
        "handoff_type": item.get("handoff_type") or "",
        "handoff_reason": item.get("handoff_reason") or "",
        "handoff_pending": bool(item.get("handoff_type")),
        "handoff_waiting": handoff_waiting,
        "handoff_attention": handoff_attention,
        "error": visible_error,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "time_text": item.get("time_text") or "",
    }


def _issue_item(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "job_id": item.get("job_id"),
        "conversation_key": item.get("conversation_key") or "",
        "conversation": item.get("conversation") or "",
        "source": item.get("source") or "",
        "original_reply": clean_customer_reply_text(item.get("original_reply") or ""),
        "final_reply": clean_customer_reply_text(item.get("final_reply") or ""),
        "status": item.get("status") or "",
        "issue_text": clean_history_message_text(item.get("issue_text") or ""),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def list_review_items(*, status: str = "ready", limit: int = 100) -> list[dict]:
    state.expire_stale_handoffs()
    if status == "issues":
        return [_issue_item(item) for item in state.list_reply_issue_tasks(status="pending", limit=limit)]
    allowed = {"handoff", "pending", "reading", "drafting", "ready", "approved", "sending", "done", "skipped", "failed"}
    selected_status = status if status in allowed else "ready"
    if selected_status == "handoff":
        items = state.list_ready_for_review(handoff=True, limit=limit)
    elif selected_status == "ready":
        items = state.list_ready_for_review(handoff=False, limit=limit)
    else:
        items = state.list_queue(status=selected_status, limit=limit)
    return [_review_item(item) for item in items]


def review_counts() -> dict[str, int]:
    state.expire_stale_handoffs()
    counts = state.queue_counts()
    for status in ("pending", "reading", "drafting", "ready", "approved", "sending", "done", "skipped", "failed"):
        counts.setdefault(status, 0)
    counts["handoff"] = state.handoff_pending_count()
    counts["handoff_attention"] = state.handoff_attention_count()
    counts["issues"] = state.reply_issue_pending_count()
    counts["ready"] = len(state.list_ready_for_review(handoff=False, limit=1000000))
    return counts


def wecom_status() -> dict:
    status = macos_backend.doctor_status()
    return {
        "ok": status.ok,
        "app_name": status.app_name or "",
        "app_running": status.app_running,
        "accessibility_ok": status.accessibility_ok,
        "osascript_ok": status.osascript_ok,
        "notes": status.notes,
        "ts": time.time(),
    }


def _attachments_for_action(job_id: int, attachment_ids: list | None = None) -> list[dict]:
    attachments = _current_attachments(job_id)
    if attachment_ids is None:
        return attachments
    allowed = {str(value) for value in attachment_ids}
    return [attachment for attachment in attachments if attachment["id"] in allowed]


def approve_item(job_id: int, *, reply_text: str | None = None, attachment_ids: list | None = None) -> dict:
    attachments = _attachments_for_action(job_id, attachment_ids)
    if not state.mark_approved(job_id, reply_text=reply_text, attachments=attachments):
        return {"ok": False, "error": "not_ready", "id": job_id}
    item = state.get_job(job_id)
    state.append_event({"type": "review_approved", "job_id": job_id, "conversation": (item or {}).get("title")})
    state.record_metric(
        "review_approved",
        conversation_key=str((item or {}).get("conversation_key") or ""),
        conversation=str((item or {}).get("title") or ""),
        job_id=job_id,
        reply_source=str((item or {}).get("reply_source") or ""),
    )
    return {"ok": True, "item": _review_item(item or {})}


def save_item(job_id: int, *, reply_text: str, attachment_ids: list | None = None) -> dict:
    attachments = _attachments_for_action(job_id, attachment_ids)
    if not state.save_reply(job_id, reply_text=reply_text, attachments=attachments):
        return {"ok": False, "error": "not_ready", "id": job_id}
    item = state.get_job(job_id)
    state.append_event({"type": "review_saved", "job_id": job_id, "conversation": (item or {}).get("title")})
    state.record_metric(
        "review_saved",
        conversation_key=str((item or {}).get("conversation_key") or ""),
        conversation=str((item or {}).get("title") or ""),
        job_id=job_id,
        reply_source=str((item or {}).get("reply_source") or "human"),
    )
    return {"ok": True, "item": _review_item(item or {})}


def record_reply_issue(job_id: int, *, issue_text: str) -> dict:
    issue = clean_history_message_text(issue_text)
    if not issue:
        return {"ok": False, "error": "empty_issue", "id": job_id}
    item = state.get_job(job_id)
    if not item:
        return {"ok": False, "error": "not_found", "id": job_id}
    state.append_event(
        {
            "type": "review_reply_issue",
            "job_id": job_id,
            "conversation": item.get("title"),
            "issue": issue,
        }
    )
    state.record_metric(
        "review_reply_issue",
        conversation_key=str(item.get("conversation_key") or ""),
        conversation=str(item.get("title") or ""),
        job_id=job_id,
        reply_source=str(item.get("reply_source") or "human"),
        details={"issue": issue},
    )
    return {"ok": True, "item": _review_item(item)}


def complete_reply_issue(task_id: int, *, issue_text: str) -> dict:
    item = state.complete_reply_issue_task(task_id, issue_text=issue_text)
    if item is None:
        return {"ok": False, "error": "invalid_issue_task", "id": task_id}
    state.append_event(
        {
            "type": "review_reply_issue_completed",
            "task_id": task_id,
            "job_id": item.get("job_id"),
            "conversation": item.get("conversation"),
        }
    )
    return {"ok": True, "item": _issue_item(item)}


def upload_attachment(job_id: int, *, filename: str, content_type: str, data_url: str = "", data_base64: str = "") -> dict:
    encoded = data_base64
    if data_url:
        if "," not in data_url:
            return {"ok": False, "error": "invalid_data_url", "id": job_id}
        header, encoded = data_url.split(",", 1)
        if not content_type and ";" in header:
            content_type = header.split(":", 1)[-1].split(";", 1)[0]
    try:
        body = base64.b64decode(encoded, validate=True)
        attachment = _store_attachment(job_id, filename=filename, content_type=content_type, body=body)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "id": job_id}
    attachments = _current_attachments(job_id)
    attachments.append(attachment)
    if not state.save_reply_attachments(job_id, attachments=attachments):
        try:
            Path(attachment["path"]).unlink()
        except OSError:
            pass
        return {"ok": False, "error": "not_ready", "id": job_id}
    return {"ok": True, "item": _review_item(state.get_job(job_id) or {})}


def delete_attachment(job_id: int, attachment_id: str) -> dict:
    target = str(attachment_id or "").strip()
    attachments = _current_attachments(job_id)
    kept = []
    removed = []
    for attachment in attachments:
        if attachment["id"] == target:
            removed.append(attachment)
        else:
            kept.append(attachment)
    if not state.save_reply_attachments(job_id, attachments=kept):
        return {"ok": False, "error": "not_ready", "id": job_id}
    for attachment in removed:
        try:
            Path(attachment["path"]).unlink()
        except OSError:
            pass
    return {"ok": True, "item": _review_item(state.get_job(job_id) or {})}


def regenerate_item(job_id: int, *, reason: str = "review_regenerate") -> dict:
    if not state.regenerate_reply(job_id, reason=reason):
        return {"ok": False, "error": "not_regeneratable", "id": job_id}
    item = state.get_job(job_id)
    state.append_event(
        {"type": "review_regenerate", "job_id": job_id, "conversation": (item or {}).get("title"), "reason": reason}
    )
    state.record_metric(
        "review_regenerate",
        conversation_key=str((item or {}).get("conversation_key") or ""),
        conversation=str((item or {}).get("title") or ""),
        job_id=job_id,
        details={"reason": reason},
    )
    return {"ok": True, "item": _review_item(item or {})}


def reject_item(job_id: int, *, reason: str = "review_rejected") -> dict:
    if not state.reject_ready(job_id, reason=reason):
        return {"ok": False, "error": "not_ready", "id": job_id}
    item = state.get_job(job_id)
    state.append_event(
        {"type": "review_rejected", "job_id": job_id, "conversation": (item or {}).get("title"), "reason": reason}
    )
    state.record_metric(
        "review_rejected",
        conversation_key=str((item or {}).get("conversation_key") or ""),
        conversation=str((item or {}).get("title") or ""),
        job_id=job_id,
        details={"reason": reason},
    )
    return {"ok": True, "item": _review_item(item or {})}


def finish_item(job_id: int, *, reason: str = "handoff_finished") -> dict:
    if not state.finish_handoff(job_id, reason=reason):
        return {"ok": False, "error": "not_handoff", "id": job_id}
    item = state.get_job(job_id)
    state.append_event(
        {"type": "review_handoff_finished", "job_id": job_id, "conversation": (item or {}).get("title")}
    )
    state.record_metric(
        "review_handoff_finished",
        conversation_key=str((item or {}).get("conversation_key") or ""),
        conversation=str((item or {}).get("title") or ""),
        job_id=job_id,
        reply_source=str((item or {}).get("reply_source") or "human"),
    )
    return {"ok": True, "item": _review_item(item or {})}


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "WeComReview/0.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_OPTIONS(self) -> None:
        _json_response(self, 200, {"ok": True})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            body = REVIEW_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/health":
            _json_response(self, 200, {"ok": True, "version": __version__})
            return
        if parsed.path == "/api/review/items":
            params = parse_qs(parsed.query)
            status = (params.get("status") or ["ready"])[0]
            limit = int((params.get("limit") or ["100"])[0])
            _json_response(self, 200, {"ok": True, "items": list_review_items(status=status, limit=limit)})
            return
        if parsed.path == "/api/review/counts":
            _json_response(self, 200, {"ok": True, "counts": review_counts(), "ts": time.time()})
            return
        if parsed.path == "/api/review/wecom-status":
            _json_response(self, 200, {"ok": True, "status": wecom_status()})
            return
        if parsed.path == "/api/review/metrics":
            params = parse_qs(parsed.query)
            since_hours = float((params.get("since_hours") or ["24"])[0])
            _json_response(self, 200, {"ok": True, "metrics": state.metrics_summary(since_hours=since_hours)})
            return
        media_parts = [part for part in parsed.path.split("/") if part]
        if len(media_parts) == 5 and media_parts[:3] == ["api", "review", "media"]:
            try:
                message_id = int(media_parts[3])
                media_index = int(media_parts[4])
            except ValueError:
                _json_response(self, 400, {"ok": False, "error": "invalid_media_id"})
                return
            media = state.get_conversation_media(message_id=message_id, media_index=media_index)
            if not media:
                _json_response(self, 404, {"ok": False, "error": "media_not_found"})
                return
            _send_file_response(self, Path(str(media["capture_path"])))
            return
        _json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 5 and parts[:3] == ["api", "review", "issues"] and parts[4] == "complete":
            try:
                task_id = int(parts[3])
            except ValueError:
                _json_response(self, 400, {"ok": False, "error": "invalid_id"})
                return
            try:
                payload = _read_json(self)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            result = complete_reply_issue(task_id, issue_text=str(payload.get("issue_text") or ""))
            _json_response(self, 200 if result.get("ok") else 409, result)
            return
        if len(parts) == 5 and parts[:3] == ["api", "review", "items"] and parts[4] == "attachments":
            try:
                job_id = int(parts[3])
            except ValueError:
                _json_response(self, 400, {"ok": False, "error": "invalid_id"})
                return
            try:
                payload = _read_json(self)
            except Exception as exc:
                _json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            result = upload_attachment(
                job_id,
                filename=str(payload.get("filename") or "image"),
                content_type=str(payload.get("content_type") or ""),
                data_url=str(payload.get("data_url") or ""),
                data_base64=str(payload.get("data_base64") or ""),
            )
            _json_response(self, 200 if result.get("ok") else 409, result)
            return
        actions = {"approve", "reject", "save", "regenerate", "finish", "issue"}
        if len(parts) != 5 or parts[:3] != ["api", "review", "items"] or parts[4] not in actions:
            _json_response(self, 404, {"ok": False, "error": "not_found"})
            return
        try:
            job_id = int(parts[3])
        except ValueError:
            _json_response(self, 400, {"ok": False, "error": "invalid_id"})
            return
        try:
            payload = _read_json(self)
        except Exception as exc:
            _json_response(self, 400, {"ok": False, "error": str(exc)})
            return
        if parts[4] == "approve":
            reply_text = payload.get("reply_text")
            result = approve_item(
                job_id,
                reply_text=str(reply_text) if reply_text is not None else None,
                attachment_ids=payload.get("attachment_ids") if isinstance(payload.get("attachment_ids"), list) else None,
            )
        elif parts[4] == "reject":
            reason = str(payload.get("reason") or "review_rejected").strip() or "review_rejected"
            result = reject_item(job_id, reason=reason)
        elif parts[4] == "save":
            reply_text = str(payload.get("reply_text") or "")
            result = save_item(
                job_id,
                reply_text=reply_text,
                attachment_ids=payload.get("attachment_ids") if isinstance(payload.get("attachment_ids"), list) else None,
            )
        elif parts[4] == "issue":
            result = record_reply_issue(job_id, issue_text=str(payload.get("issue_text") or ""))
        elif parts[4] == "regenerate":
            reason = str(payload.get("reason") or "review_regenerate").strip() or "review_regenerate"
            result = regenerate_item(job_id, reason=reason)
        else:
            reason = str(payload.get("reason") or "handoff_finished").strip() or "handoff_finished"
            result = finish_item(job_id, reason=reason)
        _json_response(self, 200 if result.get("ok") else 409, result)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 6 and parts[:3] == ["api", "review", "items"] and parts[4] == "attachments":
            try:
                job_id = int(parts[3])
            except ValueError:
                _json_response(self, 400, {"ok": False, "error": "invalid_id"})
                return
            result = delete_attachment(job_id, parts[5])
            _json_response(self, 200 if result.get("ok") else 409, result)
            return
        _json_response(self, 404, {"ok": False, "error": "not_found"})


def serve_review(*, host: str | None = None, port: int | None = None) -> None:
    resolved_host = host or os.environ.get("WECOM_REVIEW_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    resolved_port = int(port or os.environ.get("WECOM_REVIEW_PORT", DEFAULT_PORT))
    httpd = ThreadingHTTPServer((resolved_host, resolved_port), ReviewHandler)
    display_host = _local_ip() if resolved_host in {"0.0.0.0", ""} else resolved_host
    print(f"wecom review: http://{display_host}:{resolved_port}/", flush=True)
    httpd.serve_forever()
