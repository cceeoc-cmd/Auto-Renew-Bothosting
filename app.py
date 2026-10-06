#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot-hosting Auto Renew v3.3
相对 v3.2 的改动：
- 修正：设置页 EMAIL 字段不是 input，改为以「EMAIL」标签为锚点读取所在块，同时兼容 input 与可见文本
- 新增：登录账号自动识别 —— 先读环境变量 EMAIL；未设置时打开 /a/settings，
        读取 Profile 里的 EMAIL 输入框；再退回「Signed in as xxx」用户名，最后才是页面文本提取

相对 v3.1 的改动（v3.2）：
相对 v3.1 的改动：
- 修复：Turnstile 通过判断改为检查 cf-turnstile-response token（原来 iframe 内的词不在页面源码里，会立刻误判通过）
- 修复：状态/时长只从状态卡片区域读取（在「CONSOLE xx lines」处截断），控制台里残留的
        Stopped/Offline/Uptime 日志不再干扰判断
- 修复：登录/开机/续期失败时退出码为 1（Actions 显示红色）；意外异常也会发 Telegram 通知

相对 v3.0 的改动：
相对 v3.0 的改动：
- 修复：运行时长支持 w（周）/ mo（月）/ y（年），如「Running 2w 2d 14h」
- 修复：登录账号不再误取页面占位邮箱（如 you-compagny.com）；优先用 EMAIL Secret，
        否则只从可见文本/去掉占位符与脚本后的源码里提取，并过滤示例/占位邮箱
- 优化：状态已是 Running+时长时不再等「Console connected」；仅 Stopped 需要确认
        （控制台连上，或连续稳定 8 秒）

相对 v2.9（v3.0）的改动：
- 修复：详情页等到「Console connected」后再判断状态，避免页面刚加载时的默认 Offline/Stopped 被误判为关机
- 修复：Start 按钮同时检查 disabled / aria-disabled / class 含 disabled，置灰按钮不再误报「已点击」
- 优化：SERVER_URL 只填路径（/a/d/<uuid>）时自动补全域名
- 优化：合并 check_and_manage_server 中重复的 running 返回分支

相对 v2.8 的改动：
- 修复：不再用控制台残留「App is running」判断运行中；Stopped/Offline 徽章优先

相对 v2.7 的改动：
- 修复：总览页显示 OFFLINE/Stopped 时先进入 /a/d/<uuid> 详情页再开机（总览页无 Start 按钮）
- 修复：find_server_link 优先匹配 /a/d/<uuid>
- 保留 v2.7：可见文本判状态、主内容区找链接、邮箱脱敏、gh secret stdin
"""

import os
import re
import sys
import traceback
import time
import json
import requests
import subprocess
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from seleniumbase import SB

# ==================== 配置 ====================
EMAIL = os.environ.get("EMAIL") or ""
SESSION_TOKEN = os.environ.get("SESSION_TOKEN") or ""
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or ""
GH_TOKEN = os.environ.get("GH_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""

# 解析 DISCORD_TOKEN（支持 "备注,token" 格式）
DC_TOKEN = DISCORD_TOKEN.split(",", 1)[-1].strip() if DISCORD_TOKEN else ""

if not SESSION_TOKEN and not DC_TOKEN:
    print("ℹ️ 未配置 SESSION_TOKEN 和 DISCORD_TOKEN，脚本终止。")
    sys.exit(1)

COOKIES = {"session_token": SESSION_TOKEN, "login": "true", "theme": "system"}

BASE = "https://bot-hosting.net"
BILLINGS_URL = f"{BASE}/a/billings"
# 可选：直接指定服务页，例如 https://bot-hosting.net/a/d/<uuid>（只填 /a/d/<uuid> 也可）
SERVER_URL = (os.environ.get("SERVER_URL") or "").strip()
if SERVER_URL.startswith("/"):
    SERVER_URL = BASE + SERVER_URL
# 服务详情页路径特征：/a/d/<uuid>
SERVER_PATH_RE = re.compile(r"^/a/d/[0-9a-fA-F\-]{8,}")

_LOGIN_METHOD = "SESSION_TOKEN"
_START_TIME = None
_ACCOUNT = ""
_APP_UPTIME = ""
_SERVER_STATUS = ""  # running / started / start_failed / unknown
_FAILED = False  # 任一关键步骤失败 → 退出码 1
TZ_CN = ZoneInfo("Asia/Shanghai")

# 侧边栏 / 非服务页路径，进入服务页时必须排除
NON_SERVER_SEGMENTS = {
    "billings", "billing", "credits", "affiliation", "affiliate", "templates",
    "settings", "account", "login", "docs", "pricing", "changelog", "support",
    "status", "discord", "developer", "knowledge", "advertise", "deploy", "new",
}

# Discord OAuth 常量
DISCORD_CLIENT_ID = "884382422530158623"
OAUTH_REDIRECT_URI = f"{BASE}/login"
OAUTH_SCOPE = "identify email guilds"
DISCORD_API = "https://discord.com/api/v9/oauth2/authorize"
DISCORD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
STATE_RE = re.compile(r"[?&]state=([^&]+)")


# ==================== 工具函数 ====================
def now_cn() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


def mask_email(email: str) -> str:
    """脱敏：a***a@mail.com"""
    if not email:
        return "未配置"
    if "@" in email:
        name, domain = email.split("@", 1)
        if len(name) <= 1:
            return f"{name}***@{domain}"
        return f"{name[0]}***{name[-1]}@{domain}"
    if len(email) <= 1:
        return f"{email}***"
    return f"{email[0]}***{email[-1]}"


def mask_token(token: str) -> str:
    if not token or len(token) < 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def save_debug_screenshot(sb, name: str):
    try:
        path = f"{name}_{int(time.time())}.png"
        sb.save_screenshot(path)
        print(f"📸 已保存截图: {path}")
    except Exception as e:
        print(f"⚠️ 截图失败: {e}")


def body_text(sb) -> str:
    """页面可见文本（单行化）"""
    try:
        return " ".join((sb.get_text("body") or "").split())
    except Exception:
        return ""


def current_path(sb) -> str:
    try:
        return urllib.parse.urlparse(sb.get_current_url()).path
    except Exception:
        return ""


def is_detail_page(sb) -> bool:
    return bool(SERVER_PATH_RE.search(current_path(sb)))


def get_cookie_info(sb, name: str):
    for c in sb.get_cookies():
        if c.get("name") == name:
            expiry_ts = c.get("expiry")
            expiry_dt = (
                datetime.fromtimestamp(expiry_ts, tz=timezone.utc) if expiry_ts else None
            )
            return c.get("value"), expiry_dt
    return None, None


def should_update_cookie(new_value, old_value, expiry_dt, days_threshold=3) -> bool:
    if not new_value:
        return False
    if new_value != old_value:
        return True
    if expiry_dt:
        remaining = (expiry_dt - datetime.now(timezone.utc)).total_seconds()
        return remaining < days_threshold * 24 * 3600
    return False


def update_github_secret(secret_name: str, new_value: str) -> bool:
    if not new_value:
        print(f"⚠️ 跳过更新 {secret_name}：新值为空")
        return False
    print(f"🔄 更新 Secret: {secret_name} (新值: {mask_token(new_value)})")
    try:
        env = os.environ.copy()
        if GH_TOKEN:
            env["GH_TOKEN"] = GH_TOKEN
        # 通过 stdin 传值，避免 Token 出现在进程参数里
        proc = subprocess.run(
            ["gh", "secret", "set", secret_name],
            input=new_value,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
        if proc.returncode == 0:
            return True
        print(f"❌ 更新失败: {proc.stderr.strip()}")
        return False
    except Exception as e:
        print(f"❌ 更新异常: {e}")
        return False


def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
        if resp.status_code == 200:
            print("✅ Telegram 通知已发送")
        else:
            print(f"❌ Telegram 发送失败: HTTP {resp.status_code}")
    except Exception as e:
        print(f"❌ Telegram 发送异常: {e}")


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}分{s}秒" if s else f"{m}分"
    h, m = divmod(m, 60)
    return f"{h}小时{m}分{s}秒" if s else f"{h}小时{m}分"


def format_notification(
    status: str,
    extra: str = "",
    error: str = "",
    expiry_date: str = "",
    account: str = "",
) -> str:
    display_account = mask_email(account or _ACCOUNT or EMAIL)
    lines = ["🇫🇮 Bot-hosting 续期通知", "", status, f"👤 登录账户: {display_account}"]
    if _LOGIN_METHOD != "SESSION_TOKEN":
        lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    if _SERVER_STATUS:
        status_map = {
            "running": "🟢 运行中",
            "started": "🟡 已开机",
            "start_failed": "🔴 开机失败",
            "unknown": "⚪ 状态未知",
        }
        lines.append(f"🖥️ 机器状态: {status_map.get(_SERVER_STATUS, _SERVER_STATUS)}")
    if _APP_UPTIME:
        lines.append(f"⏳ 运行时长: {_APP_UPTIME}")
    if _START_TIME is not None:
        lines.append(f"🕒 脚本耗时: {format_duration(time.time() - _START_TIME)}")
    lines.append(f"⏱️ 执行时间: {now_cn()}")
    return "\n".join(lines)


def turnstile_token_ready(sb):
    """True=已有 token；False=有控件但还没 token；None=页面里没有 Turnstile 控件"""
    try:
        return sb.execute_script(r"""
            const inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (!inp) return null;
            return !!(inp.value && inp.value.length > 20);
        """)
    except Exception:
        return None


def wait_for_turnstile_pass(sb, timeout: int = 30) -> bool:
    """
    以 cf-turnstile-response token 为准。
    5 秒内一直没出现 Turnstile 控件时，退回检查整页拦截文案（整页 CF 挑战场景）。
    """
    indicators = ["verify you are human", "确认您是真人", "troubleshoot", "just a moment"]
    start = time.time()
    while time.time() - start < timeout:
        tok = turnstile_token_ready(sb)
        if tok is True:
            print("✅ Turnstile 验证已通过（已获得 token）")
            return True
        if tok is None and time.time() - start >= 5:
            if not any(x in sb.get_page_source().lower() for x in indicators):
                print("✅ 未发现 Turnstile 控件，按无需验证处理")
                return True
        sb.sleep(1)
    print("❌ Turnstile 验证超时未通过")
    return False


def get_current_ip(proxy_server: str = "") -> str:
    proxies = {"http": proxy_server, "https": proxy_server} if proxy_server else None
    resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    resp.raise_for_status()
    return resp.text.strip()


def format_countdown(countdown_str: str) -> str:
    """23:27:00 → 23小时27分"""
    try:
        h, m, _ = countdown_str.split(":")
        h, m = int(h), int(m)
        if h and m:
            return f"{h}小时{m}分"
        return f"{h}小时" if h else f"{m}分"
    except Exception:
        return countdown_str


# ==================== 页面信息提取 ====================
def extract_expiry_date(page_source: str) -> str | None:
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*(\d{4}/\d{2}/\d{2})",
        r"[Ee]xpires\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew",
        r"(\d{2}/\d{2}/\d{4})\s*[\-–]\s*renew",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            parts = match.group(1).split("/")
            # MM/DD/YYYY → YYYY/MM/DD
            if len(parts) == 3 and len(parts[0]) == 2 and len(parts[2]) == 4:
                return f"{parts[2]}/{parts[0]}/{parts[1]}"
            return match.group(1)
    return None


_SKIP_DOMAINS = {
    "example.com", "sentry.io", "w3.org", "github.com", "google.com",
    "cloudflare.com", "discord.com", "bot-hosting.net",
}
_PLACEHOLDER_DOMAIN_WORDS = (
    "compagny", "company", "example", "yourdomain", "domain", "sample", "test", "localhost",
)
_GENERIC_LOCALS = {
    "you", "your", "user", "username", "name", "email", "test", "admin", "support",
    "contact", "info", "hello", "noreply", "no-reply", "john", "jane", "john.doe",
}
_FILE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".js", ".css")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _is_real_email(e: str) -> bool:
    low = e.lower()
    local, _, domain = low.partition("@")
    if domain in _SKIP_DOMAINS or domain.endswith(_FILE_EXTS):
        return False
    if any(w in domain for w in _PLACEHOLDER_DOMAIN_WORDS):
        return False
    return local not in _GENERIC_LOCALS


def extract_account_email(page_source: str, visible_text: str = "") -> str | None:
    """
    提取登录账号：
    1) 可见文本里的真实邮箱
    2) 去掉 <script>/<style> 和 placeholder 属性后的源码里的真实邮箱
    占位/示例邮箱（如 you@you-compagny.com）一律过滤。
    """
    for e in _EMAIL_RE.findall(visible_text or ""):
        if _is_real_email(e):
            return e
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page_source or "", flags=re.S | re.I)
    cleaned = re.sub(r"""placeholder\s*=\s*(".*?"|'.*?')""", "", cleaned, flags=re.S | re.I)
    for e in _EMAIL_RE.findall(cleaned):
        if _is_real_email(e):
            return e
    return None


_CONSOLE_HEADER_RE = re.compile(r"\bconsole\b\s*\d+\s*lines?\b", re.I)


def card_text(text: str) -> str:
    """只保留状态卡片部分：在控制台标题（CONSOLE xx lines）之前截断，避开控制台日志干扰"""
    text = text or ""
    m = _CONSOLE_HEADER_RE.search(text)
    return text[:m.start()] if m else text


_UNIT = r"(?:mo|w|d|h|m|s|y)(?![a-zA-Z])|周|天|小时|分|秒"
_UPTIME_RE = re.compile(
    r"(?:Running|Uptime\s*[:：]?|Online\s*[·•]|运行中\s*[·•]?)\s*"
    rf"((?:\d+\s*(?:{_UNIT})\s*)+)",
    re.I,
)
_PART_RE = re.compile(rf"(\d+)\s*({_UNIT})", re.I)
_UNIT_SECONDS = {"y": 31536000, "mo": 2592000, "w": 604800, "周": 604800,
                 "d": 86400, "天": 86400, "h": 3600, "小时": 3600,
                 "m": 60, "分": 60, "s": 1, "秒": 1}


def extract_app_uptime(text: str) -> str | None:
    """从可见文本提取 Running 1d 6h 7m 之类的时长，转为中文。超过 400 天视为异常。"""
    for m in _UPTIME_RE.finditer(card_text(text)):
        total = 0
        for num, unit in _PART_RE.findall(m.group(1)):
            total += int(num) * _UNIT_SECONDS[unit.lower()]
        if total <= 0:
            continue
        if total > 400 * 86400:
            print(f"⚠️ 运行时长异常过大已丢弃: {m.group(1).strip()}")
            continue
        d, rem = divmod(total, 86400)
        h, rem = divmod(rem, 3600)
        mi, s = divmod(rem, 60)
        parts = [f"{d}天" if d else "", f"{h}小时" if h else "",
                 f"{mi}分" if mi else "", f"{s}秒" if s else ""]
        return "".join(parts)
    return None


def detect_server_status(text: str) -> str:
    """
    基于页面可见文本判断：running / stopped / unknown
    - 以状态徽章为准：Running <时长> / Stopped / Offline
    - 不用 "App is running"（控制台残留日志会误判）
    - 按钮文案 Start/Stop 不会被当成状态
    """
    text = card_text(text)
    # 先看明确的停止徽章（优先于控制台旧日志）
    if re.search(r"\b(Stopped|Offline|Suspended)\b", text, re.I) or re.search(r"已停止|已关机|离线", text):
        # 若同时有 Running+时长徽章，以 Running 为准（刷新瞬间可能两者短暂共存）
        if not re.search(r"Running\s+\d", text, re.I):
            return "stopped"
    # 运行中：必须带时长数字，避免裸 Running 文案
    if re.search(r"Running\s+\d", text, re.I) or re.search(r"运行中\s*\d", text):
        return "running"
    return "unknown"


# ==================== 服务器状态检测 / 开机 ====================
def wait_page_ready(sb, timeout: int = 20) -> str:
    """
    等待 SPA 渲染完成后返回页面文本。
    - Running + 时长：已是明确的运行状态，直接返回
    - 详情页 Stopped：可能是控制台连接前的默认态，需等到 Console connected，
      或连续稳定 8 秒才确认
    - 总览页：出现状态词或 slots 字样即可
    超时也返回当前文本。
    """
    end = time.time() + timeout
    text = ""
    need_console = is_detail_page(sb)
    stopped_since = None
    while time.time() < end:
        text = body_text(sb)
        st = detect_server_status(text)
        slots = bool(re.search(r"\d+\s*/\s*\d+\s*slots", text, re.I))
        if st == "stopped" and need_console:
            stopped_since = stopped_since or time.time()
            if "console connected" in text.lower() or time.time() - stopped_since >= 8:
                sb.sleep(1.5)
                return body_text(sb)
        else:
            stopped_since = None
            if st != "unknown" or slots:
                sb.sleep(1.5)
                return body_text(sb)
        sb.sleep(1)
    return text


def list_page_links(sb) -> list[dict]:
    """返回页面所有同源链接，标注是否位于 nav/aside/header/footer 内"""
    return sb.execute_script(r"""
        return Array.from(document.querySelectorAll('a[href]')).map(a => {
            let u;
            try { u = new URL(a.getAttribute('href'), location.origin); } catch (e) { return null; }
            if (u.origin !== location.origin) return null;
            return {
                text: (a.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 60),
                href: u.pathname + u.search,
                chrome: !!a.closest('nav, aside, header, footer, [role="navigation"]'),
            };
        }).filter(Boolean);
    """) or []


def find_server_link(sb) -> str | None:
    """在主内容区找服务详情链接；优先 /a/d/<uuid>，其次带状态文字的卡片"""
    # 0) 已知的服务页路径特征 /a/d/<uuid>
    for link in list_page_links(sb):
        if SERVER_PATH_RE.match(link["href"]):
            return link["href"].split("?")[0]

    # 1) 带状态文字的卡片 → 最近的 <a>
    href = sb.execute_script(r"""
        const re = /(Running\s+\d|Stopped|Offline|Suspended|运行中|已停止)/i;
        for (const el of document.querySelectorAll('main *, body *')) {
            if (el.children.length > 8) continue;
            const t = (el.innerText || '').trim();
            if (!t || t.length > 200 || !re.test(t)) continue;
            if (el.closest('nav, aside, header, footer')) continue;
            const a = el.closest('a[href]') || el.parentElement?.closest('a[href]');
            if (a) return new URL(a.getAttribute('href'), location.origin).pathname;
        }
        return null;
    """)
    if href:
        return href

    # 2) 主内容区里路径不在黑名单的 /a/... 链接
    for link in list_page_links(sb):
        if link["chrome"]:
            continue
        segs = [s for s in link["href"].split("?")[0].split("/") if s]
        if not segs or segs[0] != "a" or len(segs) < 2:
            continue
        if segs[1].lower() in NON_SERVER_SEGMENTS:
            continue
        return link["href"]
    return None


def click_start_button(sb) -> bool:
    """只点文案精确为 Start / 启动 / 开机 且可用的按钮（排除 Restart、置灰按钮）"""
    try:
        clicked = sb.execute_script(r"""
            const ok = new Set(['start', '启动', '开机']);
            for (const n of document.querySelectorAll('button, a, [role="button"]')) {
                if (n.disabled
                    || n.getAttribute('aria-disabled') === 'true'
                    || /disabled/i.test(n.className || '')
                    || n.closest('nav, aside, header, footer')) continue;
                const t = (n.innerText || n.textContent || '').trim().toLowerCase();
                if (ok.has(t)) { n.click(); return t; }
            }
            return null;
        """)
        if clicked:
            print(f"✅ 已点击开机按钮: {clicked}")
            return True
    except Exception as e:
        print(f"⚠️ 点击 Start 失败: {e}")
    return False


def dump_debug(sb, tag: str):
    """定位问题用：完整文本 + 全部链接 + 截图"""
    print(f"📄 [{tag}] URL: {sb.get_current_url()}")
    print(f"📄 [{tag}] 文本: {body_text(sb)[:1200]}")
    links = [f"{'(nav) ' if l['chrome'] else ''}{l['text']} -> {l['href']}" for l in list_page_links(sb)]
    print(f"🔗 [{tag}] 链接({len(links)}): " + " | ".join(links[:40]))
    save_debug_screenshot(sb, tag)


def read_state(sb) -> tuple[str, str, str]:
    text = wait_page_ready(sb)
    return detect_server_status(text), extract_app_uptime(text) or "", text


def check_and_manage_server(sb) -> tuple[str, str]:
    """
    返回 (status, uptime)
    status: running / started / start_failed / unknown
    - running  → 记录时长
    - stopped  → 进入详情页后点 Start
    - unknown  → 尝试进详情页再判断；仍未知则不操作
    关键：总览页即使显示 OFFLINE/Stopped，也必须先进入 /a/d/<uuid>
          详情页才有 Start 按钮，总览页点不到。
    """
    start_url = SERVER_URL or f"{BASE}/a/"
    sb.open(start_url)
    sb.wait_for_ready_state_complete()
    st, up, text = read_state(sb)
    on_detail = is_detail_page(sb)
    print(f"📊 {'服务页' if on_detail or SERVER_URL else '总览页'}状态: {st}, 时长: {up or '无'}")
    print(f"📄 页面文本: {text[:400]}")

    if st == "running" or up:
        return "running", up

    # 不在详情页时，一律先找 /a/d/<uuid> 进入（无论 stopped 还是 unknown）
    if not on_detail and not SERVER_URL:
        href = find_server_link(sb)
        if not href:
            print("⚠️ 未找到服务器链接 (/a/d/...)")
            dump_debug(sb, "no_server_link")
            return "unknown", ""
        print(f"➡️ 进入服务页: {href}")
        sb.open(BASE + href if href.startswith("/") else href)
        sb.wait_for_ready_state_complete()
        st, up, text = read_state(sb)
        on_detail = is_detail_page(sb)
        print(f"📊 服务页状态: {st}, 时长: {up or '无'} (detail={on_detail})")
        print(f"📄 服务页文本: {text[:400]}")
        if st == "running" or up:
            return "running", up

    if st != "stopped":
        print("⚠️ 状态无法确认，不执行开机")
        dump_debug(sb, "status_unknown")
        return "unknown", ""

    # 明确未运行 → 仅在详情页开机
    if not on_detail:
        print("⚠️ 未处于服务详情页，放弃开机")
        dump_debug(sb, "not_on_detail")
        return "start_failed", ""

    print("🔌 检测到未运行，尝试开机...")
    if not click_start_button(sb):
        print("⚠️ 未找到可用的 Start 按钮")
        dump_debug(sb, "no_start_btn")
        return "start_failed", ""

    for _ in range(5):  # 最多等待约 35 秒
        sb.sleep(7)
        text = body_text(sb)
        up = extract_app_uptime(text) or ""
        if up or detect_server_status(text) == "running":
            print(f"✅ 开机成功，运行时长: {up or '启动中'}")
            return "started", up or "启动中"
    print("⚠️ 已点 Start，但状态仍未变为 Running")
    save_debug_screenshot(sb, "start_pending")
    return "start_failed", ""


# ==================== Discord OAuth ====================
def capture_discord_state(sb) -> str:
    print("🔎 获取 Discord OAuth state...")
    sb.uc_open_with_reconnect(f"{BASE}/login/discord", reconnect_time=4)
    sb.sleep(2)

    url = sb.get_current_url()
    if "discord.com" not in url:
        print(f"⚠️ 未跳转到 Discord 页面，当前 URL：{url}")
        return ""
    m = STATE_RE.search(url)
    if not m:
        print("❌ 未能解析 state")
        return ""
    print("✅ 已捕获 state")
    return urllib.parse.unquote(m.group(1))


def discord_authorize(state: str) -> str:
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPE,
        "state": state,
    }
    query = urllib.parse.urlencode(params)
    headers = {
        "accept": "*/*",
        "authorization": DC_TOKEN,
        "content-type": "application/json",
        "origin": "https://discord.com",
        "referer": "https://discord.com/oauth2/authorize?" + query,
        "user-agent": DISCORD_UA,
        "x-discord-locale": "zh-CN",
    }
    body = json.dumps({
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {"guild_id": "10000", "channel_id": "10000", "channel_type": 10000},
    })

    proxies = None
    if os.environ.get("IS_PROXY", "false").lower() == "true":
        proxy_server = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
        proxies = {"http": proxy_server, "https": proxy_server}

    try:
        resp = requests.post(
            f"{DISCORD_API}?{query}", headers=headers, data=body, proxies=proxies, timeout=20
        )
        if resp.status_code != 200:
            print(f"❌ Discord OAuth 失败: HTTP {resp.status_code} - {resp.text[:300]}")
            return ""
        data = resp.json()
    except Exception as e:
        print(f"❌ Discord OAuth 异常: {e}")
        return ""

    location = data.get("location", "")
    if not location:
        print(f"❌ 授权响应无 location: {data}")
        return ""
    print(f"✅ 拿到回调 URL: {re.sub(r'code=[^&]+', 'code=***', location)}")
    return location


def do_discord_login(sb) -> bool:
    print("\n🔑 通过 Discord Token 登录...")
    state = capture_discord_state(sb)
    if not state:
        save_debug_screenshot(sb, "login_no_state")
        return False

    location = discord_authorize(state)
    if not location:
        return False

    print("↩️ 打开回调链接...")
    sb.uc_open_with_reconnect(location, reconnect_time=4)
    sb.sleep(3)

    url = sb.get_current_url()
    if "/error/banned" in url:
        print("🚫 账号已被封禁")
        save_debug_screenshot(sb, "login_banned")
        return False
    if "bot-hosting.net" not in url:
        print(f"❌ 回调后未跳转至 bot-hosting.net，当前 URL：{url}")
        save_debug_screenshot(sb, "login_no_redirect")
        return False
    if "fraud" in body_text(sb).lower():
        print("🚫 触发风控（fraud attempt），可能是 IP 被拦截")
        save_debug_screenshot(sb, "login_fraud")
        return False

    for _ in range(40):
        url = sb.get_current_url()
        path = urllib.parse.urlparse(url).path
        if "bot-hosting.net" in url and path != "/login" and not path.startswith("/login/discord"):
            print(f"✅ Discord OAuth 登录成功！当前页面：{url}")
            return True
        sb.sleep(0.5)

    print(f"❌ 登录超时，最终停留在：{url}")
    save_debug_screenshot(sb, "login_timeout")
    return False


# ==================== 续期核心 ====================
COUNTDOWN_RE = re.compile(r"Renew in (\d{2}:\d{2}:\d{2})")


def find_renew_button(sb):
    """返回 (selector, countdown)；二者至多一个非 None"""
    selectors = [
        'button:contains("Renew")',
        'a:contains("Renew")',
        '[class*="renew"]',
        '[class*="Renew"]',
    ]
    for selector in selectors:
        try:
            if not sb.is_element_visible(selector):
                continue
            text = sb.get_text(selector)
            m = COUNTDOWN_RE.search(text)
            if m:
                return None, m.group(1)
            if "Renew" in text:
                print(f"✅ 续期按钮可用: '{text}'")
                return selector, None
        except Exception:
            continue
    return None, None


def notify(status, **kw):
    send_telegram_message(format_notification(status, **kw))


def do_renew(sb, current_expiry: str | None) -> bool:
    global _FAILED
    expiry_text = current_expiry or "（未获取到）"
    outer_selector, countdown_text = find_renew_button(sb)

    if not outer_selector:
        if countdown_text:
            friendly = format_countdown(countdown_text)
            print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
            notify("⏳ 未到续期时间", extra=f"⏱️ 可续期时间: {friendly}后", expiry_date=expiry_text)
        else:
            print("ℹ️ 未找到续期按钮或倒计时")
            notify("ℹ️ 无需续期 / 状态未知", extra="请手动检查后台", expiry_date=expiry_text)
        return False

    print("🔄 点击外部续期按钮...")
    try:
        sb.click(outer_selector)
        sb.wait_for_element_visible('button:contains("Renew for 4 days")', timeout=20)
    except Exception as e:
        print(f"❌ 点击外部按钮或等待模态框失败: {e}")
        _FAILED = True
        save_debug_screenshot(sb, "renew_click_fail")
        notify("❌ 续期失败", error="点击外部续期按钮或模态框超时")
        return False

    print("🔒 处理 Turnstile 验证...")
    passed = False
    for attempt in range(1, 4):
        try:
            sb.uc_gui_click_captcha()
            sb.sleep(8)
        except Exception as e:
            print(f"⚠️ 第 {attempt} 次点击 Turnstile 出错: {e}")
        if wait_for_turnstile_pass(sb, timeout=18):
            passed = True
            break
        print(f"⏳ 第 {attempt} 次未通过，重试...")

    if not passed:
        _FAILED = True
        save_debug_screenshot(sb, "turnstile_fail")
        notify("❌ 续期失败", error="Turnstile 验证未通过")
        return False

    print("⏳ 点击「Renew for 4 days」...")
    try:
        sb.click('button:contains("Renew for 4 days")', timeout=10)
        print("✅ 已点击续期按钮")
    except Exception as e:
        print(f"❌ 点击续期按钮失败: {e}")
        _FAILED = True
        save_debug_screenshot(sb, "renew_confirm_fail")
        notify("❌ 续期失败", error="点击最终续期按钮失败")
        return False

    sb.sleep(6)
    new_page = sb.get_page_source()
    new_expiry = extract_expiry_date(new_page)
    new_match = COUNTDOWN_RE.search(new_page)

    if new_match:
        cd = new_match.group(1)
        print(f"✅ 续期成功！新倒计时: {cd}")
        notify("✅ 续期成功", extra=f"⏱️ 可续期时间: {format_countdown(cd)}后",
               expiry_date=new_expiry or "（未获取到）")
        return True

    if new_expiry and new_expiry != current_expiry:
        print(f"✅ 续期成功，到期日期更新为: {new_expiry}")
        notify("✅ 续期成功", extra="到期日期已更新", expiry_date=new_expiry)
        return True

    print("⚠️ 续期结果未知，到期日期未明显变化")
    _FAILED = True
    save_debug_screenshot(sb, "renew_result_unknown")
    notify("⚠️ 续期可能未成功", extra="请登录后台检查", expiry_date=expiry_text)
    return False


# ==================== 登录账号识别 ====================
SETTINGS_URL = f"{BASE}/a/settings"


def read_account_from_settings(sb) -> str:
    """
    打开设置页读取 Profile 里的 EMAIL。
    该字段看起来像输入框，但实际是普通元素，所以不能只扫 input：
    1) 以「EMAIL」标签为锚点，在其所在块（向上 3 层）里找邮箱，兼容 div / input
    2) 任意 input 的值
    3) 页面可见文本里的真实邮箱
    都读不到再取「Signed in as xxx」里的用户名。
    """
    try:
        sb.open(SETTINGS_URL)
        sb.wait_for_ready_state_complete()
        for _ in range(10):
            val = sb.execute_script(r"""
                const re = /[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}/;
                const labels = Array.from(document.querySelectorAll('*')).filter(
                    e => e.children.length === 0 &&
                         (e.textContent || '').trim().toLowerCase() === 'email');
                for (const lb of labels) {
                    let n = lb.parentElement;
                    for (let d = 0; d < 3 && n; d++, n = n.parentElement) {
                        const t = (n.innerText || '') + ' ' +
                            Array.from(n.querySelectorAll('input')).map(i => i.value).join(' ');
                        const m = t.match(re);
                        if (m) return m[0];
                    }
                }
                for (const el of document.querySelectorAll('input, textarea')) {
                    const m = (el.value || '').match(re);
                    if (m) return m[0];
                }
                return '';
            """) or ""
            if val and _is_real_email(val):
                return val
            for e in _EMAIL_RE.findall(body_text(sb)):
                if _is_real_email(e):
                    return e
            sb.sleep(1)
        m = re.search(r"Signed in as\s+(\S+)", body_text(sb), re.I)
        if m:
            print("⚠️ 设置页未读到邮箱，改用 Discord 用户名")
            return m.group(1)
    except Exception as e:
        print(f"⚠️ 读取设置页账号失败: {e}")
    return ""


def resolve_account(sb, page_source: str, visible_text: str) -> str:
    """优先级：EMAIL 环境变量 → 设置页邮箱/用户名 → 页面文本中的真实邮箱"""
    if EMAIL:
        print("👤 账号来源: EMAIL 环境变量")
        return EMAIL
    acc = read_account_from_settings(sb)
    if acc:
        print("👤 账号来源: 设置页")
        return acc
    acc = extract_account_email(page_source, visible_text) or ""
    if acc:
        print("👤 账号来源: 页面文本")
    return acc


# ==================== 主流程 ====================
def goto_billings(sb) -> bool:
    sb.open(BILLINGS_URL)
    sb.wait_for_ready_state_complete()
    sb.sleep(2)
    url = sb.get_current_url()
    return "/a/billings" in url and "/login" not in url and "error=" not in url


def run():
    global _START_TIME, _ACCOUNT, _APP_UPTIME, _SERVER_STATUS, _LOGIN_METHOD, _FAILED
    _START_TIME = time.time()
    _ACCOUNT = _APP_UPTIME = _SERVER_STATUS = ""

    print("#" * 28)
    print("   Bot-hosting 自动续期 v3.3")
    print("#" * 28)

    is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_server = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    headless = os.environ.get("HEADLESS", "false").lower() == "true"

    sb_kwargs = {"uc": True, "headless": headless}
    if is_proxy:
        print(f"🔗 挂载代理: {proxy_server}")
        sb_kwargs["proxy"] = proxy_server
    else:
        print("🍭 未使用代理，直连访问")

    with SB(**sb_kwargs) as sb:
        try:
            print(f"📍 当前出口 IP: {get_current_ip(proxy_server if is_proxy else '')}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False

        # ---------- 方式1：SESSION_TOKEN ----------
        if SESSION_TOKEN:
            print("🚀 启动浏览器并注入 Cookie...")
            sb.open(f"{BASE}/")
            sb.wait_for_ready_state_complete()
            sb.sleep(1.5)
            for name, value in COOKIES.items():
                if value:
                    sb.add_cookie({"name": name, "value": value, "domain": "bot-hosting.net"})
            login_ok = goto_billings(sb)
            print(f"📝 当前 URL: {sb.get_current_url()}")
            if login_ok:
                print("✅ SESSION_TOKEN 登录成功")
            else:
                print("❌ SESSION_TOKEN 登录失败")
                save_debug_screenshot(sb, "session_login_fail")

        # ---------- 方式2：Discord OAuth ----------
        if not login_ok and DC_TOKEN:
            _LOGIN_METHOD = "Discord Token"
            print("\n🔄 尝试 Discord OAuth 登录...")
            if do_discord_login(sb):
                login_ok = goto_billings(sb)
                if login_ok:
                    print("✅ Discord OAuth 登录成功")
                else:
                    print(f"❌ 登录后仍未到达账单页: {sb.get_current_url()}")
                    save_debug_screenshot(sb, "discord_billings_fail")
            else:
                print("❌ Discord OAuth 登录失败")

        if not login_ok:
            if not SESSION_TOKEN:
                error_msg = "Discord OAuth 登录失败"
            elif not DC_TOKEN:
                error_msg = "SESSION_TOKEN 失效且未配置 Discord Token"
            else:
                error_msg = "SESSION_TOKEN 和 Discord OAuth 均失败"
            _FAILED = True
            notify("❌ 登录失败", error=error_msg)
            return

        # ---------- 到期日期 & 账号 ----------
        sb.sleep(1.5)
        page_source = sb.get_page_source()
        current_expiry = extract_expiry_date(page_source)
        print(f"📅 当前到期日期: {current_expiry}" if current_expiry else "⚠️ 未能提取当前到期日期")

        _ACCOUNT = resolve_account(sb, page_source, body_text(sb))
        if _ACCOUNT:
            print(f"👤 登录账号: {mask_email(_ACCOUNT)}")
        else:
            print("⚠️ 未能识别登录账号，通知里将显示「未配置」")

        # ---------- 机器状态：运行中跳过，明确关机才开机 ----------
        print("🔎 检测机器状态...")
        try:
            _SERVER_STATUS, _APP_UPTIME = check_and_manage_server(sb)
        except Exception as e:
            print(f"⚠️ 机器状态检测异常: {e}")
            save_debug_screenshot(sb, "server_check_error")
            _SERVER_STATUS, _APP_UPTIME = "unknown", ""

        if _SERVER_STATUS == "running":
            print(f"✅ 机器运行中，运行时长: {_APP_UPTIME or '未知'}")
        elif _SERVER_STATUS == "started":
            print(f"✅ 已执行开机，运行时长: {_APP_UPTIME or '启动中'}")
        elif _SERVER_STATUS == "start_failed":
            _FAILED = True
            print("❌ 开机失败")
        else:
            print("⚠️ 未能确认机器状态")

        # ---------- 回账单页并续期 ----------
        try:
            sb.open(BILLINGS_URL)
            sb.wait_for_ready_state_complete()
            sb.sleep(1.5)
        except Exception:
            pass
        try:
            do_renew(sb, current_expiry)
        except Exception as e:
            _FAILED = True
            print(f"❌ 续期流程异常: {e}")
            traceback.print_exc()
            save_debug_screenshot(sb, "renew_exception")
            notify("❌ 续期失败", error=f"续期流程异常: {str(e)[:150]}")

        # ---------- 更新 SESSION_TOKEN ----------
        print("🔄 检查 SESSION_TOKEN 是否需要更新")
        new_token, token_expiry = get_cookie_info(sb, "session_token")
        if should_update_cookie(new_token, SESSION_TOKEN, token_expiry):
            print("🔄 SESSION_TOKEN 需要更新")
            if GH_TOKEN:
                if update_github_secret("SESSION_TOKEN", new_token):
                    print("✅ SESSION_TOKEN 更新成功")
                else:
                    print("⚠️ 更新失败，请检查 GH_TOKEN 权限")
            else:
                print("⚠️ 未设置 GH_TOKEN，无法自动更新")
        else:
            print("✅ SESSION_TOKEN 无需更新")

        print("🏁 脚本执行完毕")


def main():
    try:
        run()
    except SystemExit:
        raise
    except Exception as e:
        print(f"❌ 脚本异常: {e}")
        traceback.print_exc()
        try:
            notify("❌ 脚本异常", error=str(e)[:200])
        except Exception:
            pass
        sys.exit(1)
    if _FAILED:
        print("❌ 存在失败步骤，退出码 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
