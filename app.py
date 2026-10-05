#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot-hosting Auto Renew v2.6
优化点：
- 显式等待替代硬编码 sleep
- 更稳健的元素定位与到期日期解析
- 统一截图 + 失败时自动保存
- 时区用 zoneinfo
- 登录 / 续期 / 更新 Token 逻辑更清晰
- 代理与 IP 检测更友好
- TG 通知：邮箱脱敏 a***a@mail.com
- 自动提取登录账号；倒计时「小时/分」
- 运行时长 = 面板 App Running 时间；脚本耗时单独显示
"""

import os
import re
import sys
import time
import json
import requests
import subprocess
import urllib.parse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from seleniumbase import SB
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ==================== 配置 ====================
EMAIL = os.environ.get("EMAIL") or ""
SESSION_TOKEN = os.environ.get("SESSION_TOKEN") or ""
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or ""
GH_TOKEN = os.environ.get("GH_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""

# 解析 DISCORD_TOKEN（支持 "备注,token" 格式）
DC_TOKEN = ""
if DISCORD_TOKEN:
    parts = DISCORD_TOKEN.split(",", 1)
    DC_TOKEN = parts[-1].strip()

if not SESSION_TOKEN and not DC_TOKEN:
    print("ℹ️ 未配置 SESSION_TOKEN 和 DISCORD_TOKEN，脚本终止。")
    sys.exit(1)

COOKIES = {
    "session_token": SESSION_TOKEN,
    "login": "true",
    "theme": "system",
}

_LOGIN_METHOD = "SESSION_TOKEN"
_START_TIME = None  # 脚本启动时间
_ACCOUNT = ""       # 登录后从页面提取的账号
_APP_UPTIME = ""    # 面板上 App 的 Running 时长
_SERVER_STATUS = "" # running / started / start_failed / unknown
TZ_CN = ZoneInfo("Asia/Shanghai")

# Discord OAuth 常量
DISCORD_CLIENT_ID = "884382422530158623"
OAUTH_REDIRECT_URI = "https://bot-hosting.net/login"
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
    """脱敏格式：a***a@mail.com（首尾各保留1位）"""
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


def get_cookie_info(sb, name: str):
    for c in sb.get_cookies():
        if c.get("name") == name:
            value = c.get("value")
            expiry_ts = c.get("expiry")
            expiry_dt = (
                datetime.fromtimestamp(expiry_ts, tz=timezone.utc)
                if expiry_ts
                else None
            )
            return value, expiry_dt
    return None, None


def should_update_cookie(new_value, old_value, expiry_dt, days_threshold=3) -> bool:
    if not new_value:
        return False
    if new_value != old_value:
        return True
    if expiry_dt:
        remaining = (expiry_dt - datetime.now(timezone.utc)).total_seconds()
        if remaining < days_threshold * 24 * 3600:
            return True
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
        proc = subprocess.run(
            ["gh", "secret", "set", secret_name, "--body", new_value],
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
    """将秒数格式化为可读时长，如 1分23秒 / 45秒"""
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
    script_duration = ""
    if _START_TIME is not None:
        script_duration = format_duration(time.time() - _START_TIME)

    display_account = mask_email(account or _ACCOUNT or EMAIL)

    lines = [
        "🇫🇮 Bot-hosting 续期通知",
        "",
        status,
        f"👤 登录账户: {display_account}",
    ]
    if _LOGIN_METHOD != "SESSION_TOKEN":
        lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    # 机器状态 / 运行时长
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
    if script_duration:
        lines.append(f"🕒 脚本耗时: {script_duration}")
    lines.append(f"⏱️ 执行时间: {now_cn()}")
    return "\n".join(lines)


def wait_for_turnstile_pass(sb, timeout: int = 30) -> bool:
    indicators = ["verify you are human", "确认您是真人", "troubleshoot", "just a moment"]
    start = time.time()
    while time.time() - start < timeout:
        page_lower = sb.get_page_source().lower()
        if not any(x in page_lower for x in indicators):
            print("✅ Turnstile 验证已通过")
            return True
        sb.sleep(1)
    print("❌ Turnstile 验证超时未通过")
    return False


def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    resp.raise_for_status()
    return resp.text.strip()


def format_countdown(countdown_str: str) -> str:
    """将 23:27:00 转为 23小时27分"""
    try:
        h, m, _ = countdown_str.split(":")
        h, m = int(h), int(m)
        if h > 0 and m > 0:
            return f"{h}小时{m}分"
        if h > 0:
            return f"{h}小时"
        return f"{m}分"
    except Exception:
        return countdown_str


def extract_expiry_date(page_source: str) -> str | None:
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*(\d{4}/\d{2}/\d{2})",
        r"[Ee]xpires\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew",
        r"(\d{2}/\d{2}/\d{4})\s*[\-–]\s*renew",
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew manually to extend for 4 days",
        r"(\d{2}/\d{2}/\d{4})\s*[\-–]\s*renew manually to extend for 4 days",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            date_str = match.group(1)
            parts = date_str.split("/")
            # MM/DD/YYYY → YYYY/MM/DD
            if len(parts) == 3 and len(parts[0]) == 2 and len(parts[2]) == 4:
                return f"{parts[2]}/{parts[0]}/{parts[1]}"
            return date_str
    return None


def extract_account_email(page_source: str) -> str | None:
    """从页面中尽量提取登录邮箱/用户标识"""
    emails = re.findall(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        page_source,
    )
    skip = {"example.com", "sentry.io", "w3.org", "github.com", "google.com",
            "cloudflare.com", "discord.com", "bot-hosting.net"}
    for e in emails:
        domain = e.split("@")[-1].lower()
        if domain in skip or e.lower().startswith("noreply"):
            continue
        if any(x in e.lower() for x in ["support@", "admin@", "no-reply", "noreply"]):
            continue
        return e
    m = re.search(r"(?:logged in as|welcome|user(?:name)?)[:\s]+([\w.#-]{2,32})", page_source, re.I)
    if m:
        return m.group(1)
    return None


def extract_app_uptime(page_source: str) -> str | None:
    """
    从页面提取 Running / Uptime / Online 时长 → 中文。
    规则（防误匹配）：
    1. 只匹配紧跟在 Running / Uptime / Online • 后的「纯时长」片段
    2. 整段必须只由 数字+单位(d/h/m/s 或 天/小时/分/秒) 组成，如 1m 10s、1d 6h 7m
       排除 FREE-EU-RO-37、88.77 MiB 等
    3. 折算超过 400 天的结果丢弃，返回 None（显示未知）
    4. 不使用任何 API uptime / started_at 字段
    """
    # 捕获 Running / Uptime / Online 后的候选片段（限制长度，避免吞掉整段正文）
    lead_patterns = [
        r"Running\s+([0-9dhmsDHMS\s]{1,24})",
        r"Uptime\s*[:：]?\s*([0-9dhmsDHMS\s]{1,24})",
        r"Online\s*[·•]\s*([0-9dhmsDHMS\s]{1,24})",
        r"运行中\s*[·•]?\s*([0-9天小时分秒\s]{1,24})",
    ]

    # 整段必须是纯时长：可选的 d/h/m/s 组合，至少有一个单位
    pure_en = re.compile(
        r"^\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*$",
        re.I,
    )
    pure_zh = re.compile(
        r"^\s*(?:(\d+)\s*天)?\s*(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分)?\s*(?:(\d+)\s*秒)?\s*$",
    )

    def _parse_and_validate(raw: str) -> str | None:
        raw = raw.strip()
        if not raw:
            return None
        # 拒绝含小数点、字母混杂（除单位外）等
        if re.search(r"[^0-9\sdhms天小时分秒]", raw, re.I):
            return None

        days = hours = mins = secs = 0
        m = pure_en.match(raw)
        if m and any(g is not None for g in m.groups()):
            days = int(m.group(1) or 0)
            hours = int(m.group(2) or 0)
            mins = int(m.group(3) or 0)
            secs = int(m.group(4) or 0)
        else:
            m = pure_zh.match(raw)
            if not m or not any(g is not None for g in m.groups()):
                return None
            days = int(m.group(1) or 0)
            hours = int(m.group(2) or 0)
            mins = int(m.group(3) or 0)
            secs = int(m.group(4) or 0)

        # 至少有一个非零分量
        if days + hours + mins + secs <= 0:
            return None

        total_days = days + hours / 24 + mins / 1440 + secs / 86400
        if total_days > 400:
            print(f"⚠️ 运行时长异常过大已丢弃: {raw} (约 {total_days:.1f} 天)")
            return None

        parts = []
        if days:
            parts.append(f"{days}天")
        if hours:
            parts.append(f"{hours}小时")
        if mins:
            parts.append(f"{mins}分")
        if secs and not days:  # 有「天」时一般不再显示秒，更干净
            parts.append(f"{secs}秒")
        elif secs and not hours and not days:
            parts.append(f"{secs}秒")
        elif secs and (hours or days):
            # 有小时/天时仍可保留秒，按需；这里保留
            parts.append(f"{secs}秒")
        return "".join(parts) if parts else None

    for pat in lead_patterns:
        for m in re.finditer(pat, page_source, re.I):
            result = _parse_and_validate(m.group(1))
            if result:
                return result
    return None


def detect_server_status(page_source: str) -> str:
    """
    返回: running / stopped / offline / unknown
    注意：页面上的 Stop/Start 按钮文案不能当作状态。
    """
    # 强信号：Running 后面跟时长数字（徽章）
    if re.search(r"Running\s+\d+\s*[smhd]", page_source, re.I):
        return "running"
    if re.search(r"运行中\s*\d+", page_source):
        return "running"
    # 控制台常见文案
    if re.search(r"App is running", page_source, re.I):
        return "running"

    # 强信号：状态徽章 Stopped / Offline（避免匹配按钮 Stop）
    # 典型：>Stopped<  或  "Stopped" 作为独立状态词且附近无 Start 启用特征
    if re.search(r"(?:status|badge|state)[^>]{0,40}stopped", page_source, re.I):
        return "stopped"
    if re.search(r">\s*Stopped\s*<", page_source, re.I):
        return "stopped"
    if re.search(r">\s*Offline\s*<", page_source, re.I) or re.search(r"\bOffline\b", page_source):
        # Offline 较少出现在按钮上
        if not re.search(r"Running\s+\d", page_source, re.I):
            return "offline"
    if "已停止" in page_source or "已关机" in page_source:
        return "stopped"
    if "离线" in page_source and not re.search(r"Running\s+\d", page_source, re.I):
        return "offline"

    return "unknown"


def _click_start_button(sb) -> bool:
    """点击开机/Start 按钮"""
    selectors = [
        'button:contains("Start")',
        'button:contains("启动")',
        'button:contains("开机")',
        'a:contains("Start")',
        '[aria-label*="Start" i]',
        '[title*="Start" i]',
    ]
    for sel in selectors:
        try:
            if sb.is_element_visible(sel):
                # 避免点到 Restart：文本刚好是 Start
                text = ""
                try:
                    text = (sb.get_text(sel) or "").strip().lower()
                except Exception:
                    pass
                if text and text not in ("start", "启动", "开机"):
                    # 可能是 Restart / Start something else
                    if "restart" in text or "重启" in text:
                        continue
                sb.click(sel)
                print(f"✅ 已点击开机按钮: {sel} ({text or 'n/a'})")
                return True
        except Exception:
            continue

    # JS 兜底：找文本精确为 Start 的按钮
    try:
        clicked = sb.execute_script(r"""
            const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            for (const n of nodes) {
                const t = (n.innerText || n.textContent || '').trim().toLowerCase();
                if (t === 'start' || t === '启动' || t === '开机') {
                    n.click();
                    return true;
                }
            }
            return false;
        """)
        if clicked:
            print("✅ 已通过 JS 点击 Start")
            return True
    except Exception as e:
        print(f"⚠️ JS 点击 Start 失败: {e}")
    return False


def open_first_deployment(sb) -> bool:
    """从控制台进入第一个部署/服务详情页"""
    before = sb.get_current_url()
    try:
        clicked = sb.execute_script(r"""
            const skip = /billings|login|settings|billing|account|docs|pricing|changelog|support/i;
            const nodes = Array.from(document.querySelectorAll('a, button, [role="link"], [class*="card"], [class*="server"], [class*="deploy"], [class*="project"], [class*="item"]'));

            // 1) 卡片内含 Running/Stopped 且可点
            for (const n of nodes) {
                const t = (n.innerText || n.textContent || '');
                if (/Running[\s]+\d/i.test(t) || />?\s*Stopped\s*/i.test(t) || /Offline/i.test(t) || /运行中|已停止/.test(t)) {
                    const href = n.getAttribute && (n.getAttribute('href') || '');
                    if (href && skip.test(href)) continue;
                    n.click();
                    return 'status:' + t.slice(0, 40);
                }
            }

            // 2) 链接路径比当前更深（服务详情）
            const cur = location.pathname.replace(/\/$/, '') || '/';
            for (const n of nodes) {
                const href = n.getAttribute && (n.getAttribute('href') || '');
                if (!href || skip.test(href)) continue;
                try {
                    const u = new URL(href, location.origin);
                    if (u.origin !== location.origin) continue;
                    const path = u.pathname.replace(/\/$/, '') || '/';
                    // 排除点到自己
                    if (path === cur || path === '/a' || path === '/') continue;
                    if (path.startsWith('/a/') || path.includes('server') || path.includes('project') || path.includes('deploy')) {
                        n.click();
                        return href;
                    }
                } catch (e) {}
            }

            // 3) Manage / Open / 应用名样式
            for (const n of nodes) {
                const t = (n.innerText || n.textContent || '').trim().toLowerCase();
                if (['manage', 'open', '管理', '打开', 'console'].includes(t)) {
                    n.click();
                    return t;
                }
            }
            return false;
        """)
        if clicked:
            sb.sleep(4)
            sb.wait_for_ready_state_complete()
            after = sb.get_current_url()
            print(f"➡️ 进入服务页: {clicked} | {before} -> {after}")
            # URL 没变也再等一会儿（SPA）
            if after == before:
                sb.sleep(3)
            return True
    except Exception as e:
        print(f"⚠️ 进入服务页失败: {e}")
    return False


def check_and_manage_server(sb) -> tuple[str, str]:
    """
    检测机器状态：
      - running  → 记录运行时长，跳过开机
      - stopped/offline → 执行开机
    返回 (status, uptime_str)
    status: running / started / start_failed / unknown
    """
    status = "unknown"
    uptime = ""

    candidate_urls = [
        "https://bot-hosting.net/a/",
        "https://bot-hosting.net/",
        "https://bot-hosting.net/a/panel",
    ]

    for url in candidate_urls:
        try:
            print(f"🌐 访问: {url}")
            sb.open(url)
            sb.wait_for_ready_state_complete()
            # SPA 动态加载，多等一会儿
            sb.sleep(5)
        except Exception as e:
            print(f"⚠️ 访问失败: {e}")
            continue

        src = sb.get_page_source()
        st = detect_server_status(src)
        up = extract_app_uptime(src) or ""
        print(f"📊 页面状态: {st}, 时长: {up or '无'}")
        # 调试：打印页面可见文本片段
        try:
            body_text = sb.get_text("body") or ""
            snippet = " ".join(body_text.split())[:300]
            print(f"📄 页面摘要: {snippet}")
        except Exception:
            pass

        if st == "running" or up:
            return ("running", up)

        # 尝试进入具体服务页
        if open_first_deployment(sb):
            src = sb.get_page_source()
            st = detect_server_status(src)
            up = extract_app_uptime(src) or ""
            print(f"📊 服务页状态: {st}, 时长: {up or '无'}")
            try:
                body_text = sb.get_text("body") or ""
                snippet = " ".join(body_text.split())[:300]
                print(f"📄 服务页摘要: {snippet}")
            except Exception:
                pass
            if st == "running" or up:
                return ("running", up)

            if st in ("stopped", "offline", "unknown"):
                # 尝试开机
                print("🔌 检测到未运行，尝试开机...")
                if _click_start_button(sb):
                    sb.sleep(8)
                    sb.wait_for_ready_state_complete()
                    src2 = sb.get_page_source()
                    up2 = extract_app_uptime(src2) or ""
                    st2 = detect_server_status(src2)
                    if st2 == "running" or up2:
                        print(f"✅ 开机成功，运行时长: {up2 or '启动中'}")
                        return ("started", up2 or "启动中")
                    # 再等一轮
                    sb.sleep(5)
                    src3 = sb.get_page_source()
                    up3 = extract_app_uptime(src3) or ""
                    if up3 or detect_server_status(src3) == "running":
                        print(f"✅ 开机成功，运行时长: {up3 or '启动中'}")
                        return ("started", up3 or "启动中")
                    print("⚠️ 已点 Start，但状态仍未变为 Running")
                    save_debug_screenshot(sb, "start_pending")
                    return ("start_failed", "")
                else:
                    print("⚠️ 未找到 Start 按钮")
                    save_debug_screenshot(sb, "no_start_btn")
                    return ("start_failed", "")

        # 当前列表页就显示 stopped，直接点 Start
        if st in ("stopped", "offline"):
            print("🔌 列表页显示未运行，尝试开机...")
            if _click_start_button(sb):
                sb.sleep(8)
                src2 = sb.get_page_source()
                up2 = extract_app_uptime(src2) or ""
                if up2 or detect_server_status(src2) == "running":
                    return ("started", up2 or "启动中")
                return ("start_failed", "")

    return (status, uptime)


# ==================== Discord OAuth ====================
# ==================== Discord OAuth ====================
def capture_discord_state(sb) -> str:
    print("🔎 获取 Discord OAuth state...")
    sb.uc_open_with_reconnect("https://bot-hosting.net/login/discord", reconnect_time=4)
    sb.sleep(2)

    url = sb.get_current_url()
    if "discord.com" not in url:
        print(f"⚠️ 未跳转到 Discord 页面，当前 URL：{url}")
        return ""

    m = STATE_RE.search(url)
    if not m:
        print(f"❌ 未能解析 state，当前 URL：{url}")
        return ""

    state = urllib.parse.unquote(m.group(1))
    print(f"✅ 已捕获 state")
    return state


def discord_authorize(state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": DISCORD_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": OAUTH_SCOPE,
            "state": state,
        }
    )
    authorize_url = f"{DISCORD_API}?{query}"

    referer = (
        "https://discord.com/oauth2/authorize?"
        + urllib.parse.urlencode(
            {
                "client_id": DISCORD_CLIENT_ID,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "response_type": "code",
                "scope": OAUTH_SCOPE,
                "state": state,
            }
        )
    )

    headers = {
        "accept": "*/*",
        "authorization": DC_TOKEN,
        "content-type": "application/json",
        "origin": "https://discord.com",
        "referer": referer,
        "user-agent": DISCORD_UA,
        "x-discord-locale": "zh-CN",
    }

    body = json.dumps(
        {
            "permissions": "0",
            "authorize": True,
            "integration_type": 0,
            "location_context": {
                "guild_id": "10000",
                "channel_id": "10000",
                "channel_type": 10000,
            },
        }
    )

    proxies = None
    is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_server = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    if is_proxy:
        proxies = {"http": proxy_server, "https": proxy_server}

    try:
        resp = requests.post(
            authorize_url, headers=headers, data=body, proxies=proxies, timeout=20
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

    masked = re.sub(r"code=[^&]+", "code=***", location)
    print(f"✅ 拿到回调 URL: {masked}")
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

    try:
        body_text = sb.get_text("body")
    except Exception:
        body_text = ""
    if "fraud" in body_text.lower():
        print("🚫 触发风控（fraud attempt），可能是 IP 被拦截")
        save_debug_screenshot(sb, "login_fraud")
        return False

    for _ in range(40):
        url = sb.get_current_url()
        path = urllib.parse.urlparse(url).path
        if (
            "bot-hosting.net" in url
            and path != "/login"
            and not path.startswith("/login/discord")
        ):
            print(f"✅ Discord OAuth 登录成功！当前页面：{url}")
            return True
        sb.sleep(0.5)

    print(f"❌ 登录超时，最终停留在：{url}")
    save_debug_screenshot(sb, "login_timeout")
    return False


# ==================== 续期核心 ====================
def find_renew_button(sb):
    """返回 (outer_selector, countdown_text)"""
    possible = [
        'button:contains("Renew")',
        'button:contains("Renew free plan")',
        'a:contains("Renew")',
        '[class*="renew"]',
        '[class*="Renew"]',
    ]
    for selector in possible:
        try:
            if sb.is_element_visible(selector):
                text = sb.get_text(selector)
                if "Renew in" in text:
                    m = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", text)
                    if m:
                        return None, m.group(1)
                elif "Renew" in text and "in" not in text.lower():
                    print(f"✅ 续期按钮可用: '{text}'")
                    return selector, None
        except Exception:
            continue
    return None, None


def do_renew(sb, current_expiry: str | None) -> bool:
    outer_selector, countdown_text = find_renew_button(sb)

    if not outer_selector:
        if countdown_text:
            friendly = format_countdown(countdown_text)
            print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
            send_telegram_message(
                format_notification(
                    "⏳ 未到续期时间",
                    extra=f"⏱️ 可续期时间: {friendly}后",
                    expiry_date=current_expiry or "（未获取到）",
                )
            )
        else:
            print("ℹ️ 未找到续期按钮或倒计时")
            send_telegram_message(
                format_notification(
                    "ℹ️ 无需续期 / 状态未知",
                    extra="请手动检查后台",
                    expiry_date=current_expiry or "（未获取到）",
                )
            )
        return False

    print("🔄 点击外部续期按钮...")
    try:
        sb.click(outer_selector)
        # 等待模态框出现（最多 20 秒）
        sb.wait_for_element_visible('button:contains("Renew for 4 days")', timeout=20)
    except Exception as e:
        print(f"❌ 点击外部按钮或等待模态框失败: {e}")
        save_debug_screenshot(sb, "renew_click_fail")
        send_telegram_message(
            format_notification("❌ 续期失败", error="点击外部续期按钮或模态框超时")
        )
        return False

    # Turnstile 处理（最多 3 次）
    print("🔒 处理 Turnstile 验证...")
    turnstile_passed = False
    for attempt in range(1, 4):
        try:
            sb.uc_gui_click_captcha()
            sb.sleep(8)
        except Exception as e:
            print(f"⚠️ 第 {attempt} 次点击 Turnstile 出错: {e}")

        if wait_for_turnstile_pass(sb, timeout=18):
            turnstile_passed = True
            break
        print(f"⏳ 第 {attempt} 次未通过，重试...")

    if not turnstile_passed:
        print("❌ Turnstile 最终未通过")
        save_debug_screenshot(sb, "turnstile_fail")
        send_telegram_message(
            format_notification("❌ 续期失败", error="Turnstile 验证未通过")
        )
        return False

    # 点击最终续期按钮
    print("⏳ 点击「Renew for 4 days」...")
    try:
        sb.click('button:contains("Renew for 4 days")', timeout=10)
        print("✅ 已点击续期按钮")
    except Exception as e:
        print(f"❌ 点击续期按钮失败: {e}")
        save_debug_screenshot(sb, "renew_confirm_fail")
        send_telegram_message(
            format_notification("❌ 续期失败", error="点击最终续期按钮失败")
        )
        return False

    # 等待结果
    sb.sleep(6)
    new_page = sb.get_page_source()
    new_expiry = extract_expiry_date(new_page)
    new_match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", new_page)

    if new_match:
        new_countdown = new_match.group(1)
        print(f"✅ 续期成功！新倒计时: {new_countdown}")
        if new_expiry:
            print(f"📅 新到期日期: {new_expiry}")
        send_telegram_message(
            format_notification(
                "✅ 续期成功",
                extra=f"⏱️ 可续期时间: {format_countdown(new_countdown)}后",
                expiry_date=new_expiry or "（未获取到）",
            )
        )
        return True

    if new_expiry and new_expiry != current_expiry:
        print(f"✅ 续期成功，到期日期更新为: {new_expiry}")
        send_telegram_message(
            format_notification(
                "✅ 续期成功",
                extra="到期日期已更新",
                expiry_date=new_expiry,
            )
        )
        return True

    print("⚠️ 续期结果未知，到期日期未明显变化")
    save_debug_screenshot(sb, "renew_result_unknown")
    send_telegram_message(
        format_notification(
            "⚠️ 续期可能未成功",
            extra="请登录后台检查",
            expiry_date=current_expiry or "（未获取到）",
        )
    )
    return False


# ==================== 主流程 ====================
def main():
    global _START_TIME, _ACCOUNT, _APP_UPTIME, _SERVER_STATUS
    _START_TIME = time.time()
    _ACCOUNT = ""
    _APP_UPTIME = ""
    _SERVER_STATUS = ""

    print("#" * 28)
    print("   Bot-hosting 自动续期 v2.6")
    print("#" * 28)

    is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_server = (
        os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    )
    headless = os.environ.get("HEADLESS", "false").lower() == "true"

    sb_kwargs = {"uc": True, "headless": headless}
    if is_proxy:
        print(f"🔗 挂载代理: {proxy_server}")
        sb_kwargs["proxy"] = proxy_server
    else:
        print("🍭 未使用代理，直连访问")

    global _LOGIN_METHOD

    with SB(**sb_kwargs) as sb:
        try:
            ip = get_current_ip(proxy_server if is_proxy else "")
            print(f"📍 当前出口 IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False

        # ---------- 方式1：SESSION_TOKEN ----------
        if SESSION_TOKEN:
            print("🚀 启动浏览器并注入 Cookie...")
            sb.open("https://bot-hosting.net/")
            sb.wait_for_ready_state_complete()
            sb.sleep(1.5)

            for name, value in COOKIES.items():
                if value:
                    sb.add_cookie(
                        {"name": name, "value": value, "domain": "bot-hosting.net"}
                    )

            print("🌐 访问账单页...")
            sb.open("https://bot-hosting.net/a/billings")
            sb.wait_for_ready_state_complete()
            sb.sleep(2)

            current_url = sb.get_current_url()
            print(f"📝 当前 URL: {current_url}")

            if (
                "/a/billings" in current_url
                and "/login" not in current_url
                and "error=" not in current_url
            ):
                login_ok = True
                print("✅ SESSION_TOKEN 登录成功")
            else:
                print(f"❌ SESSION_TOKEN 登录失败")
                save_debug_screenshot(sb, "session_login_fail")

        # ---------- 方式2：Discord OAuth ----------
        if not login_ok and DC_TOKEN:
            _LOGIN_METHOD = "Discord Token"
            print("\n🔄 尝试 Discord OAuth 登录...")
            if do_discord_login(sb):
                sb.open("https://bot-hosting.net/a/billings")
                sb.wait_for_ready_state_complete()
                sb.sleep(2)
                current_url = sb.get_current_url()
                if "a/billings" in current_url:
                    login_ok = True
                    print("✅ Discord OAuth 登录成功")
                else:
                    print(f"❌ 登录后仍未到达账单页: {current_url}")
                    save_debug_screenshot(sb, "discord_billings_fail")
            else:
                print("❌ Discord OAuth 登录失败")

        if not login_ok:
            error_msg = "SESSION_TOKEN 和 Discord OAuth 均失败"
            if not SESSION_TOKEN and DC_TOKEN:
                error_msg = "Discord OAuth 登录失败"
            elif SESSION_TOKEN and not DC_TOKEN:
                error_msg = "SESSION_TOKEN 失效且未配置 Discord Token"
            send_telegram_message(format_notification("❌ 登录失败", error=error_msg))
            return

        if _LOGIN_METHOD == "Discord Token":
            print("ℹ️ 本次使用 Discord 登录，将尝试更新 SESSION_TOKEN")

        # ---------- 提取到期日期 & 登录账号 ----------
        sb.sleep(1.5)
        page_source = sb.get_page_source()
        current_expiry = extract_expiry_date(page_source)
        if current_expiry:
            print(f"📅 当前到期日期: {current_expiry}")
        else:
            print("⚠️ 未能提取当前到期日期")

        _ACCOUNT = extract_account_email(page_source) or ""
        if _ACCOUNT:
            print(f"👤 登录账号: {_ACCOUNT}")
        else:
            print("⚠️ 未能从页面提取登录账号，将使用 EMAIL Secret")

        # ---------- 检测机器状态：运行中跳过，关机则开机 ----------
        print("🔎 检测机器状态...")
        _SERVER_STATUS, _APP_UPTIME = check_and_manage_server(sb)
        if _SERVER_STATUS == "running":
            print(f"✅ 机器运行中，运行时长: {_APP_UPTIME or '未知'}")
        elif _SERVER_STATUS == "started":
            print(f"✅ 已执行开机，运行时长: {_APP_UPTIME or '启动中'}")
        elif _SERVER_STATUS == "start_failed":
            print("❌ 开机失败或未找到 Start 按钮")
            save_debug_screenshot(sb, "server_start_fail")
        else:
            print("⚠️ 未能确认机器状态")

        # 回到账单页，保证后续续期逻辑正常
        try:
            sb.open("https://bot-hosting.net/a/billings")
            sb.wait_for_ready_state_complete()
            sb.sleep(1.5)
        except Exception:
            pass

        # ---------- 执行续期 ----------
        do_renew(sb, current_expiry)

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
                print(f"📋 请手动设置 SESSION_TOKEN = {mask_token(new_token)}")
        else:
            print("✅ SESSION_TOKEN 无需更新")

        print("🏁 脚本执行完毕")


if __name__ == "__main__":
    main()
