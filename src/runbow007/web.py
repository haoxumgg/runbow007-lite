"""人工上传兜底页面。

李宁 TMS 的导出经常取不到数据，自动下载不能是唯一入口。这个模块提供一个只依赖
标准库的 WSGI 应用：登录之后把手工从 TMS 下载中心下载的 Excel 传上来，走
`Pipeline.process_file`——也就是自动任务用的同一套解析、规则、去重和飞书发送。

刻意不引入 Web 框架：服务器只有 2 GiB 内存，且这个页面每天只会被用几次。
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import html
import logging
import re
import secrets
import socketserver
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from zoneinfo import ZoneInfo

import portalocker

from .config import AppConfig
from .pipeline import Pipeline

logger = logging.getLogger(__name__)

SESSION_COOKIE = "runbow007_session"
ALL_RULES: tuple[str, ...] = ("R1", "R2", "R3", "R4")
RULE_LABELS = {
    "R1": "WMS 过账",
    "R2": "今日签收",
    "R3": "合同签署",
    "R4": "延迟原因",
}
ALLOWED_SUFFIXES = (".xls", ".xlsx")
# 上传页只是入口，真正的重活在解析和发送；等锁 3 秒足够区分"重复提交"和"空闲"。
LOCK_TIMEOUT_SECONDS = 3
PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 240_000
MAX_LOGIN_FAILURES = 5
LOGIN_BLOCK_SECONDS = 900


# ---------------------------------------------------------------------------
# 口令


def hash_password(
    password: str, *, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None
) -> str:
    """把口令编码成 `pbkdf2_sha256$迭代次数$盐$哈希`，可以直接写进配置文件。"""
    if not password:
        raise ValueError("口令不能为空")
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PBKDF2_ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != PBKDF2_ALGORITHM:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        rounds = int(iterations)
    except (ValueError, binascii.Error):
        return False
    if rounds <= 0:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(candidate, expected)


# ---------------------------------------------------------------------------
# 会话与登录限速


@dataclass(frozen=True, slots=True)
class Session:
    token: str
    username: str
    csrf_token: str
    expires_at: float


class SessionStore:
    """进程内会话表。服务重启后需要重新登录，对这个用量完全够用。"""

    def __init__(self, ttl_seconds: int, *, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def create(self, username: str) -> Session:
        session = Session(
            secrets.token_urlsafe(32),
            username,
            secrets.token_urlsafe(32),
            self._clock() + self._ttl,
        )
        with self._lock:
            self._prune()
            self._sessions[session.token] = session
        return session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            self._prune()
            return self._sessions.get(token)

    def drop(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def _prune(self) -> None:
        now = self._clock()
        for token in [key for key, item in self._sessions.items() if item.expires_at <= now]:
            del self._sessions[token]


class LoginThrottle:
    """同一来源连续失败若干次后暂时拒绝，避免默认口令被在线爆破。"""

    def __init__(
        self,
        *,
        max_failures: int = MAX_LOGIN_FAILURES,
        block_seconds: int = LOGIN_BLOCK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._block_seconds = block_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}

    def blocked_seconds(self, key: str) -> int:
        with self._lock:
            count, last_failure = self._failures.get(key, (0, 0.0))
            remaining = self._block_seconds - (self._clock() - last_failure)
            if count < self._max_failures or remaining <= 0:
                return 0
            return int(remaining) + 1

    def record_failure(self, key: str) -> None:
        with self._lock:
            count, last_failure = self._failures.get(key, (0, 0.0))
            now = self._clock()
            if now - last_failure > self._block_seconds:
                count = 0
            self._failures[key] = (count + 1, now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


# ---------------------------------------------------------------------------
# multipart 解析


class UploadError(ValueError):
    """Raised when the browser sends something this page cannot use."""


@dataclass(frozen=True, slots=True)
class Part:
    name: str
    filename: str | None
    value: bytes

    @property
    def text(self) -> str:
        return self.value.decode("utf-8", errors="replace").strip()


_DISPOSITION_NAME = re.compile(rb'name="([^"]*)"')
_DISPOSITION_FILENAME = re.compile(rb'filename="([^"]*)"')


def parse_multipart(body: bytes, boundary: bytes) -> list[Part]:
    """够用的 multipart/form-data 解析。

    Python 3.13 删掉了 `cgi`，`email` 那套要先把整个消息重组成 MIME 才能用；
    这里只需要认几个字段和一个文件，自己切比引依赖划算。
    """
    if not boundary:
        raise UploadError("上传请求缺少 multipart 分隔符")
    parts: list[Part] = []
    for chunk in body.split(b"--" + boundary):
        if not chunk or chunk.startswith(b"--"):
            continue
        block = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        header_end = block.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        raw_headers = block[:header_end]
        value = block[header_end + 4 :]
        if value.endswith(b"\r\n"):
            value = value[:-2]
        disposition = b""
        for line in raw_headers.split(b"\r\n"):
            if line.lower().startswith(b"content-disposition:"):
                disposition = line
                break
        name_match = _DISPOSITION_NAME.search(disposition)
        if not name_match:
            continue
        filename_match = _DISPOSITION_FILENAME.search(disposition)
        filename = (
            filename_match.group(1).decode("utf-8", errors="replace")
            if filename_match
            else None
        )
        parts.append(
            Part(name_match.group(1).decode("utf-8", errors="replace"), filename, value)
        )
    return parts


def boundary_of(content_type: str) -> bytes:
    for token in content_type.split(";"):
        key, _, value = token.strip().partition("=")
        if key.lower() == "boundary":
            return value.strip('"').encode("utf-8")
    return b""


# ---------------------------------------------------------------------------
# 页面


STYLE = """
*, *::before, *::after { box-sizing: border-box; }
:root {
  color-scheme: light;
  --page: #f4f7fb;
  --surface: #ffffff;
  --surface-subtle: #f8fafc;
  --text: #182230;
  --muted: #667085;
  --border: #e4e9f0;
  --border-strong: #cfd7e3;
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --primary-soft: #eff6ff;
  --focus: rgba(37, 99, 235, .2);
  --danger: #b42318;
  --danger-soft: #fef3f2;
  --success: #067647;
  --success-soft: #ecfdf3;
  --warning: #b54708;
  --warning-soft: #fffaeb;
  --shadow: 0 18px 50px rgba(16, 24, 40, .08);
}
body {
  margin: 0;
  min-height: 100vh;
  padding: 28px 18px 48px;
  font-family: Inter, "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
  background:
    radial-gradient(circle at 12% 0, rgba(37, 99, 235, .08), transparent 28rem),
    var(--page);
  color: var(--text);
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
main { max-width: 860px; margin: 0 auto; }
h1, h2, p { margin-top: 0; }
h1 { margin-bottom: 6px; font-size: clamp(24px, 4vw, 30px); line-height: 1.2; }
h2 { margin-bottom: 0; font-size: 17px; line-height: 1.35; }
.card, .panel {
  margin-bottom: 18px;
  padding: 28px;
  overflow: hidden;
  background: var(--surface);
  border: 1px solid rgba(228, 233, 240, .9);
  border-radius: 16px;
  box-shadow: var(--shadow);
}
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 22px;
  padding: 0 4px;
}
.brand { display: inline-flex; align-items: center; gap: 11px; font-weight: 600; }
.brand-mark {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  color: #fff;
  font-size: 12px;
  letter-spacing: -.02em;
  background: #172033;
  border-radius: 10px;
  box-shadow: 0 6px 16px rgba(23, 32, 51, .18);
}
.brand-name { letter-spacing: -.01em; }
.user-nav { display: flex; align-items: center; gap: 14px; color: var(--muted); font-size: 13px; }
.user-nav button.link { min-height: 40px; padding: 8px 0; }
.muted, .field-hint, .safety-note { color: var(--muted); font-size: 13px; }
.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 26px;
}
.eyebrow {
  margin-bottom: 7px;
  color: var(--primary);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.destination {
  display: flex;
  max-width: 280px;
  align-items: center;
  gap: 8px;
  padding: 8px 11px;
  color: var(--muted);
  font-size: 12px;
  background: var(--surface-subtle);
  border-radius: 9px;
}
.status-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  background: #12b76a;
  border-radius: 50%;
}
.destination code {
  min-width: 0;
  overflow: hidden;
  color: var(--text);
  text-overflow: ellipsis;
  white-space: nowrap;
}
form { margin: 0; }
.field { margin-bottom: 22px; }
.field > label, legend {
  display: block;
  margin-bottom: 7px;
  padding: 0;
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
}
.field-hint { display: block; margin: -3px 0 9px; }
fieldset { min-width: 0; margin: 0; padding: 0; border: 0; }
input[type=text], input[type=password], input[type=number], input[type=file] {
  width: 100%;
  min-height: 44px;
  padding: 10px 12px;
  color: var(--text);
  font: inherit;
  font-size: 14px;
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: 9px;
  outline: none;
  transition: border-color .15s ease, box-shadow .15s ease;
}
input:focus-visible { border-color: var(--primary); box-shadow: 0 0 0 4px var(--focus); }
.file-field {
  padding: 18px;
  background: var(--surface-subtle);
  border: 1px dashed var(--border-strong);
  border-radius: 12px;
}
input[type=file] { min-height: 48px; padding: 5px; background: var(--surface); }
input[type=file]::file-selector-button {
  margin-right: 12px;
  padding: 9px 13px;
  color: var(--text);
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  background: var(--surface-subtle);
  border: 0;
  border-radius: 7px;
  cursor: pointer;
}
.form-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(180px, .75fr);
  gap: 22px;
}
.checks { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
.rule-option { position: relative; margin: 0; cursor: pointer; }
.rule-option input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}
.rule-content {
  display: flex;
  min-height: 43px;
  align-items: center;
  gap: 8px;
  padding: 9px 11px;
  color: var(--muted);
  font-size: 13px;
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-radius: 9px;
  transition: border-color .15s ease, background .15s ease, color .15s ease;
}
.rule-code { color: var(--text); font-size: 12px; font-weight: 600; }
.rule-option input:checked + .rule-content {
  color: #1d4ed8;
  background: var(--primary-soft);
  border-color: #93b4fa;
}
.rule-option input:focus-visible + .rule-content { box-shadow: 0 0 0 4px var(--focus); }
.action-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  margin-top: 4px;
  padding-top: 20px;
  border-top: 1px solid var(--border);
}
.mode-option { display: flex; align-items: center; gap: 10px; margin: 0; cursor: pointer; }
.mode-option input { width: 17px; height: 17px; margin: 0; accent-color: var(--primary); }
.mode-copy { display: grid; gap: 1px; }
.mode-title { font-size: 14px; font-weight: 600; }
.mode-hint { color: var(--muted); font-size: 12px; }
button {
  min-height: 42px;
  padding: 10px 19px;
  color: #fff;
  font: inherit;
  font-size: 14px;
  font-weight: 600;
  background: var(--primary);
  border: 0;
  border-radius: 9px;
  cursor: pointer;
  transition: background .15s ease, transform .15s ease;
}
button:hover { background: var(--primary-hover); }
button:active { transform: translateY(1px); }
button:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
button:disabled { background: #94a3b8; cursor: progress; transform: none; }
button.link {
  min-height: auto;
  padding: 5px 0;
  color: var(--primary);
  font-size: 13px;
  font-weight: 400;
  background: none;
}
button.link:hover { color: var(--primary-hover); background: none; }
.safety-note { margin: 12px 0 0; text-align: right; }
.banner {
  margin-bottom: 18px;
  padding: 11px 13px;
  font-size: 13px;
  border-radius: 9px;
}
.error { color: var(--danger); background: var(--danger-soft); border: 1px solid #fecdca; }
.ok { color: var(--success); background: var(--success-soft); border: 1px solid #abefc6; }
.warn { color: var(--warning); background: var(--warning-soft); border: 1px solid #fedf89; }
.result-panel { padding-bottom: 22px; }
.result-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.result-title .muted { margin: 0; }
.metrics {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1px;
  overflow: hidden;
  background: var(--border);
  border-radius: 11px;
}
.metric { padding: 16px; background: var(--surface-subtle); }
.metric-label { display: block; margin-bottom: 4px; color: var(--muted); font-size: 12px; }
.metric-value { display: block; font-size: 23px; font-weight: 600; line-height: 1.2; }
details { margin-top: 14px; color: var(--muted); font-size: 13px; }
summary { width: fit-content; cursor: pointer; color: var(--primary); }
.result-details {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 8px 16px;
  margin: 13px 0 0;
}
.result-details dt { color: var(--muted); }
.result-details dd { min-width: 0; margin: 0; overflow-wrap: anywhere; color: var(--text); }
code { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
.auth-shell { max-width: 420px; margin: 10vh auto 0; }
.auth-brand { display: flex; justify-content: center; margin-bottom: 18px; }
.auth-panel { padding: 32px; }
.auth-heading { margin-bottom: 26px; text-align: center; }
.auth-heading h1 { margin-bottom: 7px; }
.auth-heading p { margin-bottom: 0; }
.auth-panel button { width: 100%; margin-top: 2px; }
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
@media (max-width: 640px) {
  body { padding: 18px 12px 32px; }
  .card, .panel, .auth-panel { padding: 20px; border-radius: 13px; }
  .app-header { margin-bottom: 16px; }
  .user-nav > span { display: none; }
  .section-heading { display: block; margin-bottom: 22px; }
  .destination { max-width: 100%; margin-top: 14px; }
  .form-grid { grid-template-columns: 1fr; gap: 0; }
  .checks { grid-template-columns: 1fr; }
  .action-bar { align-items: stretch; flex-direction: column; }
  .action-bar button { width: 100%; }
  .safety-note { text-align: left; }
  .metrics { grid-template-columns: 1fr; }
  input[type=text], input[type=password], input[type=number], input[type=file] {
    font-size: 16px;
  }
  .metric {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 13px 15px;
  }
  .metric-label { margin: 0; }
  .metric-value { font-size: 20px; }
  .auth-shell { margin-top: 5vh; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
}
"""


def render_page(title: str, body: str, nonce: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f'<style nonce="{nonce}">{STYLE}</style>'
        f"</head><body><main>{body}</main></body></html>"
    )


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def login_page(*, error: str, nonce: str) -> str:
    banner = (
        f'<div role="alert" class="banner error">{_escape(error)}</div>' if error else ""
    )
    body = f"""
<div class="auth-shell">
  <div class="auth-brand">
    <div class="brand">
      <span class="brand-mark">007</span><span class="brand-name">runbow007</span>
    </div>
  </div>
  <section class="panel auth-panel">
    <div class="auth-heading">
      <h1>欢迎回来</h1>
      <p class="muted">登录订单提醒工作台</p>
    </div>
    {banner}
    <form method="post" action="/login">
      <div class="field">
        <label for="username">账号</label>
        <input type="text" id="username" name="username" autocomplete="username" required>
      </div>
      <div class="field">
        <label for="password">密码</label>
        <input type="password" id="password" name="password"
               autocomplete="current-password" required>
      </div>
      <button type="submit">进入工作台</button>
    </form>
  </section>
</div>
"""
    return render_page("登录 · runbow007", body, nonce)


@dataclass(slots=True)
class FormState:
    rules: tuple[str, ...]
    ui_total: str = ""


@dataclass(slots=True)
class UploadView:
    username: str
    csrf_token: str
    form: FormState
    max_upload_mb: int
    chat_id: str
    error: str = ""
    result: str = ""


def upload_page(view: UploadView, nonce: str) -> str:
    rule_inputs = "".join(
        (
            '<label class="rule-option">'
            '<input type="checkbox" name="rules" value="{code}"{checked}>'
            '<span class="rule-content"><span class="rule-code">{code}</span>{label}</span>'
            "</label>"
        ).format(
            code=code,
            label=_escape(RULE_LABELS[code]),
            checked=" checked" if code in view.form.rules else "",
        )
        for code in ALL_RULES
    )
    banner = (
        f'<div role="alert" class="banner error">{_escape(view.error)}</div>'
        if view.error
        else ""
    )
    body = f"""
<header class="app-header">
  <div class="brand">
    <span class="brand-mark">007</span><span class="brand-name">订单提醒</span>
  </div>
  <div class="user-nav">
    <span>{_escape(view.username)}</span>
    <form method="post" action="/logout">
    <input type="hidden" name="csrf_token" value="{_escape(view.csrf_token)}">
      <button class="link" type="submit">退出</button>
    </form>
  </div>
</header>
{view.result}
<section class="panel">
  <div class="section-heading">
    <div>
      <p class="eyebrow">TMS Excel</p>
      <h1>上传订单文件</h1>
      <span class="muted">选择规则，确认后开始处理</span>
    </div>
    <div class="destination" aria-label="飞书目标群">
      <span class="status-dot" aria-hidden="true"></span>
      <span>飞书群</span>
      <code title="{_escape(view.chat_id)}">{_escape(view.chat_id)}</code>
    </div>
  </div>
  {banner}
  <form method="post" action="/upload" enctype="multipart/form-data" id="upload-form">
    <input type="hidden" name="csrf_token" value="{_escape(view.csrf_token)}">
    <div class="field file-field">
      <label for="file">Excel 文件</label>
      <span class="field-hint" id="file-hint">.xls / .xlsx，最大 {view.max_upload_mb} MB</span>
      <input type="file" id="file" name="file" accept=".xls,.xlsx"
             aria-describedby="file-hint" required>
    </div>
    <div class="form-grid">
      <fieldset class="field">
        <legend>提醒规则</legend>
        <div class="checks">{rule_inputs}</div>
      </fieldset>
      <div class="field">
        <label for="ui_total">页面总条数</label>
        <span class="field-hint" id="ui-total-hint">选填，用于完整性校验</span>
        <input type="number" id="ui_total" name="ui_total" min="1"
               value="{_escape(view.form.ui_total)}" placeholder="例如 4750"
               aria-describedby="ui-total-hint">
      </div>
    </div>
    <div class="action-bar">
      <span class="mode-copy">
        <span class="mode-title">直接推送飞书</span>
        <span class="mode-hint">发送本轮全部命中订单</span>
      </span>
      <button type="submit" id="submit-button">确认并推送</button>
    </div>
    <p class="safety-note">每次上传都会发送全部当前命中项，不与历史提醒比较。</p>
  </form>
</section>
<script nonce="{nonce}">
var uploadForm = document.getElementById('upload-form');
var submitButton = document.getElementById('submit-button');
uploadForm.addEventListener('submit', function (event) {{
  if (!window.confirm('将把本轮全部命中订单发送到飞书群，确认继续？')) {{
    event.preventDefault();
    return;
  }}
  uploadForm.setAttribute('aria-busy', 'true');
  submitButton.disabled = true;
  submitButton.textContent = '正在解析并推送…';
}});
</script>
"""
    return render_page("上传 · runbow007", body, nonce)


def result_card(result: object, filename: str, finished_at: datetime) -> str:
    row_count = getattr(result, "row_count", 0)
    candidate_count = getattr(result, "candidate_count", 0)
    sent_count = getattr(result, "sent_count", 0)
    rule_counts: Iterable[tuple[str, int]] = getattr(result, "rule_counts", ())
    if sent_count:
        banner = (
            f'<div role="status" class="banner ok">'
            f"已推送飞书，本次提醒 {sent_count} 项。</div>"
        )
    else:
        banner = (
            '<div role="status" class="banner warn">'
            "解析完成，没有符合条件的订单，未发送飞书。</div>"
        )
    breakdown = "、".join(f"{code} {count}" for code, count in rule_counts) or "无"
    return f"""
<section class="panel result-panel" aria-live="polite">
  <div class="result-title">
    <h2>处理结果</h2>
    <p class="muted">{_escape(finished_at.strftime("%m-%d %H:%M"))}</p>
  </div>
  {banner}
  <div class="metrics">
    <div class="metric">
      <span class="metric-label">解析行数</span>
      <strong class="metric-value" data-metric="rows">{row_count}</strong>
    </div>
    <div class="metric">
      <span class="metric-label">规则命中</span>
      <strong class="metric-value" data-metric="candidates">{candidate_count}</strong>
    </div>
    <div class="metric">
      <span class="metric-label">本次推送</span>
      <strong class="metric-value" data-metric="sent">{sent_count}</strong>
    </div>
  </div>
  <details>
    <summary>查看运行详情</summary>
    <dl class="result-details">
      <dt>文件</dt><dd>{_escape(filename)}</dd>
      <dt>规则</dt><dd>{_escape(breakdown)}</dd>
      <dt>完成时间</dt><dd>{_escape(finished_at.strftime("%Y-%m-%d %H:%M:%S"))}</dd>
      <dt>运行编号</dt><dd><code>{_escape(getattr(result, "run_id", ""))}</code></dd>
    </dl>
  </details>
</section>
"""


# ---------------------------------------------------------------------------
# WSGI 应用


@dataclass(slots=True)
class Response:
    status: str
    body: bytes
    headers: list[tuple[str, str]] = field(default_factory=list)


class WebApp:
    def __init__(
        self,
        config: AppConfig,
        *,
        pipeline_factory: Callable[[AppConfig], Pipeline] = Pipeline,
    ) -> None:
        self.config = config
        self.web = config.web
        self.pipeline_factory = pipeline_factory
        self.sessions = SessionStore(self.web.session_timeout_minutes * 60)
        self.throttle = LoginThrottle()
        self.max_upload_bytes = self.web.max_upload_mb * 1024 * 1024
        self._default_rules = tuple(
            code for code in ALL_RULES if code in self.web.default_rules
        ) or ALL_RULES

    # -- WSGI 入口 ---------------------------------------------------------

    def __call__(self, environ: dict, start_response: Callable) -> list[bytes]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/") or "/"
        try:
            response = self._route(method, path, environ)
        except Exception:  # pragma: no cover - 最后一道网，避免整页 500 空白
            logger.exception("处理 %s %s 失败", method, path)
            response = self._html(
                "500 Internal Server Error",
                render_page("出错了", '<div class="card">服务内部错误，请查看日志。</div>', ""),
                nonce="",
            )
        headers = [
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            *response.headers,
        ]
        headers.append(("Content-Length", str(len(response.body))))
        start_response(response.status, headers)
        return [response.body]

    def _route(self, method: str, path: str, environ: dict) -> Response:
        if path == "/healthz":
            return Response("200 OK", b"ok", [("Content-Type", "text/plain; charset=utf-8")])
        if path == "/":
            return _redirect("/upload")
        if path == "/login":
            if method == "GET":
                return self._login_form(environ)
            if method == "POST":
                return self._login(environ)
            return _method_not_allowed()
        if path == "/logout":
            if method != "POST":
                return _method_not_allowed()
            return self._logout(environ)
        if path == "/upload":
            session = self.sessions.get(_cookie_token(environ))
            if session is None:
                return _redirect("/login")
            if method == "GET":
                return self._upload_form(session)
            if method == "POST":
                return self._upload(environ, session)
            return _method_not_allowed()
        return self._html(
            "404 Not Found", render_page("404", '<div class="card">页面不存在。</div>', ""), ""
        )

    # -- 登录 --------------------------------------------------------------

    def _login_form(self, environ: dict, *, error: str = "") -> Response:
        if self.sessions.get(_cookie_token(environ)) is not None:
            return _redirect("/upload")
        nonce = _nonce()
        return self._html("200 OK", login_page(error=error, nonce=nonce), nonce)

    def _login(self, environ: dict) -> Response:
        fields = self._read_form(environ)
        client = environ.get("REMOTE_ADDR", "unknown")
        blocked = self.throttle.blocked_seconds(client)
        if blocked:
            logger.warning("登录尝试过于频繁，来源 %s 已被暂时拒绝", client)
            return self._login_form(
                environ, error=f"登录失败次数过多，请 {blocked // 60 + 1} 分钟后再试。"
            )
        username = (fields.get("username") or [""])[0].strip()
        password = (fields.get("password") or [""])[0]
        if not self._credentials_match(username, password):
            self.throttle.record_failure(client)
            logger.warning("登录失败，来源 %s，账号 %s", client, username or "(空)")
            return self._login_form(environ, error="账号或密码不正确。")
        self.throttle.reset(client)
        session = self.sessions.create(username)
        logger.info("登录成功，来源 %s，账号 %s", client, username)
        response = _redirect("/upload")
        response.headers.append(("Set-Cookie", self._cookie(session.token)))
        return response

    def _credentials_match(self, username: str, password: str) -> bool:
        name_ok = _same_secret(username, self.web.username)
        if self.web.password_hash:
            password_ok = verify_password(password, self.web.password_hash)
        else:
            password_ok = _same_secret(password, self.web.password)
        return name_ok and password_ok

    def _logout(self, environ: dict) -> Response:
        token = _cookie_token(environ)
        session = self.sessions.get(token)
        fields = self._read_form(environ)
        if session is not None and not _same_secret(
            (fields.get("csrf_token") or [""])[0], session.csrf_token
        ):
            return _redirect("/upload")
        self.sessions.drop(token)
        response = _redirect("/login")
        response.headers.append(("Set-Cookie", self._cookie("", expired=True)))
        return response

    def _cookie(self, token: str, *, expired: bool = False) -> str:
        parts = [f"{SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict"]
        if self.web.secure_cookie:
            parts.append("Secure")
        parts.append("Max-Age=0" if expired else f"Max-Age={self.web.session_timeout_minutes * 60}")
        return "; ".join(parts)

    # -- 上传 --------------------------------------------------------------

    def _upload_form(
        self,
        session: Session,
        *,
        form: FormState | None = None,
        error: str = "",
        result: str = "",
    ) -> Response:
        nonce = _nonce()
        view = UploadView(
            username=session.username,
            csrf_token=session.csrf_token,
            form=form or FormState(self._default_rules),
            max_upload_mb=self.web.max_upload_mb,
            chat_id=self.config.feishu.chat_id,
            error=error,
            result=result,
        )
        status = "400 Bad Request" if error else "200 OK"
        return self._html(status, upload_page(view, nonce), nonce)

    def _upload(self, environ: dict, session: Session) -> Response:
        try:
            body = self._read_body(environ, limit=self.max_upload_bytes)
        except UploadError as exc:
            return self._upload_form(session, error=str(exc))
        parts = parse_multipart(body, boundary_of(environ.get("CONTENT_TYPE", "")))
        fields = {part.name: part for part in parts if part.filename is None}
        rules = tuple(
            part.text.upper()
            for part in parts
            if part.name == "rules" and part.text.upper() in ALL_RULES
        )
        def field_text(name: str) -> str:
            part = fields.get(name)
            return part.text if part is not None else ""

        form = FormState(rules, field_text("ui_total"))
        csrf = field_text("csrf_token")
        if not _same_secret(csrf, session.csrf_token):
            logger.warning("上传请求缺少有效的 CSRF 令牌")
            return self._upload_form(session, form=form, error="会话已过期，请重新提交。")

        upload = next((part for part in parts if part.name == "file" and part.filename), None)
        try:
            filename, payload = _validate_upload(upload)
            ui_total = _parse_ui_total(form.ui_total)
            if not rules:
                raise UploadError("请至少勾选一条规则。")
        except UploadError as exc:
            return self._upload_form(session, form=form, error=str(exc))

        logger.info(
            "收到人工上传: 文件=%s 字节=%s 规则=%s 全量发送=True 账号=%s",
            filename,
            len(payload),
            ",".join(rules),
            session.username,
        )
        try:
            result = self._run_pipeline(
                filename, payload, rules=rules, ui_total=ui_total
            )
        except portalocker.AlreadyLocked:
            return self._upload_form(
                session, form=form, error="已有任务正在运行，请稍后再试。"
            )
        except Exception as exc:
            logger.exception("人工上传处理失败: %s", filename)
            return self._upload_form(
                session, form=form, error=f"处理失败：{_short(str(exc))}"
            )
        finished_at = datetime.now(ZoneInfo(self.config.runtime.timezone))
        return self._upload_form(
            session,
            form=FormState(rules, form.ui_total),
            result=result_card(result, filename, finished_at),
        )

    def _run_pipeline(
        self,
        filename: str,
        payload: bytes,
        *,
        rules: tuple[str, ...],
        ui_total: int | None,
    ) -> object:
        upload_dir = self.config.runtime.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix.lower()
        handle, temporary = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=upload_dir)
        temporary_path = Path(temporary)
        try:
            with open(handle, "wb") as stream:
                stream.write(payload)
            # 和定时任务抢同一把锁：两边永远不会同时写库或同时发飞书。
            with portalocker.Lock(self.config.runtime.lock_path, timeout=LOCK_TIMEOUT_SECONDS):
                pipeline = self.pipeline_factory(self.config)
                return pipeline.process_file(
                    temporary_path,
                    rule_codes=rules,
                    expected_ui_total=ui_total,
                    send=True,
                    send_all_current=True,
                )
        finally:
            temporary_path.unlink(missing_ok=True)

    # -- 工具 --------------------------------------------------------------

    def _read_form(self, environ: dict) -> dict[str, list[str]]:
        """读取普通表单。超长的请求体当作空表单，登录和退出都会因此失败。"""
        try:
            raw = self._read_body(environ, limit=8 * 1024)
        except UploadError:
            logger.warning("表单请求体过大，已忽略")
            return {}
        return parse_qs(raw.decode("utf-8", "replace"))

    def _read_body(self, environ: dict, *, limit: int) -> bytes:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError as exc:
            raise UploadError("请求长度无法识别。") from exc
        if length > limit:
            raise UploadError(f"上传内容超过 {self.web.max_upload_mb} MB 上限。")
        stream = environ.get("wsgi.input")
        if stream is None or length <= 0:
            return b""
        return stream.read(length)

    def _html(self, status: str, page: str, nonce: str) -> Response:
        policy = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'"
        )
        if nonce:
            policy += f"; script-src 'nonce-{nonce}'"
        return Response(
            status,
            page.encode("utf-8"),
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Security-Policy", policy),
            ],
        )


def _validate_upload(upload: Part | None) -> tuple[str, bytes]:
    if upload is None or not upload.filename:
        raise UploadError("请选择要上传的 Excel 文件。")
    filename = Path(upload.filename.replace("\\", "/")).name
    if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
        raise UploadError("只支持 TMS 导出的 .xls 或 .xlsx 文件。")
    if not upload.value:
        raise UploadError("上传的文件是空的。")
    return filename, upload.value


def _parse_ui_total(raw: str) -> int | None:
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise UploadError("页面总条数必须是数字。") from exc
    if value <= 0:
        raise UploadError("页面总条数必须大于 0。")
    return value


def _same_secret(candidate: str, expected: str) -> bool:
    """定长比较。

    `hmac.compare_digest` 只接受纯 ASCII 的 str，口令里有一个中文就会抛
    TypeError 变成 500；先编码成 UTF-8 再比。
    """
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _cookie_token(environ: dict) -> str | None:
    raw = environ.get("HTTP_COOKIE", "")
    if not raw:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:  # pragma: no cover - 畸形 Cookie 直接当作未登录
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def _redirect(location: str) -> Response:
    return Response("303 See Other", b"", [("Location", location)])


def _method_not_allowed() -> Response:
    return Response(
        "405 Method Not Allowed",
        b"method not allowed",
        [("Content-Type", "text/plain; charset=utf-8")],
    )


def _nonce() -> str:
    return secrets.token_urlsafe(16)


def _short(message: str, limit: int = 500) -> str:
    collapsed = " ".join(message.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 服务器


class _ThreadingWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


class _LoggingHandler(WSGIRequestHandler):
    # 慢链路上传 30 MB 也要留足时间，但不能让半开连接永远占着线程。
    timeout = 300

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.info("%s %s", self.address_string(), format % args)


def serve(config: AppConfig, *, host: str | None = None, port: int | None = None) -> None:
    """阻塞式启动上传页面，Ctrl+C 或 SIGTERM 退出。"""
    config.ensure_directories()
    # 先构造一次，配置或数据库有问题时立刻失败，而不是等第一次上传才报错。
    Pipeline(config)
    app = WebApp(config)
    bind_host = host or config.web.host
    bind_port = port or config.web.port
    server = make_server(
        bind_host,
        bind_port,
        app,
        server_class=_ThreadingWSGIServer,
        handler_class=_LoggingHandler,
    )
    logger.info(
        "人工上传页面已启动: http://%s:%s/ 账号 %s",
        bind_host,
        bind_port,
        config.web.username,
    )
    if not config.web.password_hash and config.web.password == "admin123456":
        logger.warning("当前仍在使用默认口令，请尽快修改 web.password 或 web.password_hash")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - 交互式退出
        logger.info("收到中断信号，正在关闭上传页面")
    finally:
        server.server_close()
