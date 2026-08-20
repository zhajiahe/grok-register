"""Grok / x.ai 批量注册入口：Chrome + DuckMail，把 sso cookie 写入文本。"""
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
import argparse
import fcntl
import glob as _glob_mod
import multiprocessing as mp
import platform
import datetime
import logging
import threading
import time
import os
import secrets
import sys

from email_register import get_email_and_token, get_oai_code


def setup_run_logger() -> logging.Logger:
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"run_{ts}_{os.getpid()}.log")

    logger = logging.getLogger("grok_register")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("日志文件: %s", log_path)
    return logger


run_logger: logging.Logger = None



def ensure_stable_python_runtime():
    # 优先自动切到更稳定的 3.12 / 3.13，避免 3.14 下 Mail.tm 偶发 TLS/兼容问题。
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}")
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    # 中文提示：避免把底层 TLS 兼容问题误判成脚本逻辑错误。
    if sys.version_info >= (3, 14):
        print("[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。")


ensure_stable_python_runtime()
warn_runtime_compatibility()

# 无头服务器 / Cloud Agent 的 VNC 桌面容易被 Cloudflare 判成交互验证，优先用 Xvfb。
# 多进程 worker 继承父进程 DISPLAY，不要再起一套 Xvfb。
_virtual_display = None
_skip_browser = any(arg == "--push-sso" or arg.startswith("--push-sso=") for arg in sys.argv[1:])
_is_mp_worker = (
    os.environ.get("GROK_REGISTER_MP_WORKER") == "1"
    or mp.current_process().name != "MainProcess"
)
_force_xvfb = (os.environ.get("USE_XVFB") == "1" or bool(os.environ.get("CURSOR_AGENT"))) and not _is_mp_worker
if (not _skip_browser) and os.environ.get("DISABLE_XVFB") != "1" and (not os.environ.get("DISPLAY") or _force_xvfb):
    try:
        from pyvirtualdisplay import Display
        _virtual_display = Display(visible=0, size=(1920, 1080))
        _virtual_display.start()
        print(f"[*] Xvfb 虚拟显示器已启动: {os.environ.get('DISPLAY')}")
    except Exception as e:
        print(f"[Warn] Xvfb 启动失败: {e}，将尝试直接运行")

# 对齐 AaronL725/grok-register：每轮新建 ChromiumOptions，让 auto_port 自己分配端口和 profile。
# DrissionPage 4.1.1+ 里 set_user_data_path() 会把 auto_port 关掉并清空 address，导致启动崩溃。
EXTENSION_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "turnstilePatch"))

_browser_proxy = ""
_proxy_pool: list = []
_proxy_fallback_pool: list = []
_proxy_index = 0
_proxy_dead: set = set()
_proxy_stats: dict = {}
_PROXY_FAIL_STRIKES = 2
_CF_ZERO_TOKEN_SECONDS = 8
_email_prefetch_lock = threading.Lock()
_email_prefetch_future = None
_email_prefetch_pool = None


def _normalize_proxy(raw) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    return value


def _load_proxy_pool() -> list:
    # browser_proxies 优先轮询；browser_proxy 作为补充。不要把 DuckMail 的 proxy 混进浏览器出口。
    proxies = []
    try:
        import json as _json_mod
        cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as handle:
                cfg = _json_mod.load(handle)
            for item in cfg.get("browser_proxies") or []:
                proxy = _normalize_proxy(item)
                if proxy and proxy not in proxies:
                    proxies.append(proxy)
            single = _normalize_proxy(cfg.get("browser_proxy", ""))
            if single and single not in proxies:
                proxies.append(single)
    except Exception:
        pass
    return proxies


def _preflight_proxy(proxy: str, timeout: float = 8.0) -> bool:
    # 只验证代理能否建立 HTTPS CONNECT。x.ai 对 curl 常返回 403，不能当作代理已死。
    import subprocess
    try:
        result = subprocess.run(
            [
                "curl", "-sS", "-o", "/dev/null", "--max-time", str(int(timeout)),
                "--connect-timeout", "5", "-x", proxy, "https://ifconfig.me",
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
        return result.returncode == 0
    except Exception:
        return False


def _preflight_proxy_pool(proxies: list) -> list:
    if not proxies:
        return []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    alive = []
    print(f"[*] 预检 {len(proxies)} 条浏览器代理...")
    with ThreadPoolExecutor(max_workers=min(8, len(proxies))) as pool:
        futures = {pool.submit(_preflight_proxy, proxy): proxy for proxy in proxies}
        for future in as_completed(futures):
            proxy = futures[future]
            ok = False
            try:
                ok = bool(future.result())
            except Exception:
                ok = False
            print(f"    {'OK' if ok else 'DEAD'} {proxy}")
            if ok:
                alive.append(proxy)
    if alive:
        # 保持配置顺序，方便按国家/来源轮询。
        order = {item: idx for idx, item in enumerate(proxies)}
        alive.sort(key=lambda item: order.get(item, 999))
        print(f"[*] 预检通过 {len(alive)}/{len(proxies)} 条，将按此列表轮询")
        return alive
    print("[Warn] 预检全部失败，仍按原列表在启动浏览器时轮询")
    return proxies


def _proxy_stat(proxy: str) -> dict:
    stat = _proxy_stats.get(proxy)
    if stat is None:
        stat = {"success": 0, "fail_streak": 0}
        _proxy_stats[proxy] = stat
    return stat


def _proxy_skipped(proxy: str) -> bool:
    return _proxy_stat(proxy)["fail_streak"] >= _PROXY_FAIL_STRIKES


def _alive_proxies(pool: list) -> list:
    return [item for item in pool if item and not _proxy_skipped(item)]


def select_browser_proxy(force_rotate: bool = False) -> str:
    # 成功则粘滞；失败才换。先用 worker 自己的切片，切片全挂再借用共享池。
    global _browser_proxy, _proxy_index
    alive = _alive_proxies(_proxy_pool)
    if not alive:
        alive = _alive_proxies(_proxy_fallback_pool)
        if alive:
            print("[Warn] 独占切片出口已跳过，改用共享池")
    if not alive:
        pools = list(dict.fromkeys(list(_proxy_pool) + list(_proxy_fallback_pool)))
        if any(_proxy_stat(item)["fail_streak"] for item in pools):
            print("[Warn] 当前可用代理都跳过过，清空连续失败后重试")
            for item in pools:
                _proxy_stat(item)["fail_streak"] = 0
        alive = list(_proxy_pool) or list(_proxy_fallback_pool)
    if not alive:
        _browser_proxy = ""
        return ""
    if _browser_proxy and _browser_proxy in alive and not force_rotate:
        return _browser_proxy

    ranked = sorted(
        alive,
        key=lambda item: (
            -_proxy_stat(item)["success"],
            _proxy_pool.index(item) if item in _proxy_pool else 999,
        ),
    )
    if force_rotate and _browser_proxy in ranked and len(ranked) > 1:
        ranked = [item for item in ranked if item != _browser_proxy] or ranked
    proxy = ranked[0]
    _proxy_index += 1
    _browser_proxy = proxy
    stat = _proxy_stat(proxy)
    print(
        f"[*] 本轮浏览器代理: {proxy} "
        f"(成功 {stat['success']} / 连续失败 {stat['fail_streak']}，候选 {len(alive)}/{len(_proxy_pool) or len(_proxy_fallback_pool)})"
    )
    return proxy


def note_proxy_success() -> None:
    if not _browser_proxy:
        return
    stat = _proxy_stat(_browser_proxy)
    stat["success"] += 1
    stat["fail_streak"] = 0


def mark_proxy_dead(reason: str = "") -> None:
    # 连续两次 CF/表单失败才跳过，避免好 IP 被单次超时误伤。
    if not _browser_proxy:
        return
    stat = _proxy_stat(_browser_proxy)
    stat["fail_streak"] += 1
    extra = f" ({reason})" if reason else ""
    if stat["fail_streak"] >= _PROXY_FAIL_STRIKES:
        print(f"[Warn] 代理连续失败 {stat['fail_streak']} 次，暂时跳过: {_browser_proxy}{extra}")
    else:
        print(
            f"[Warn] 代理本轮失败 {stat['fail_streak']}/{_PROXY_FAIL_STRIKES}，下次换出口: "
            f"{_browser_proxy}{extra}"
        )


_proxy_pool = _load_proxy_pool()
if _proxy_pool:
    print(f"[*] 浏览器代理池: {len(_proxy_pool)} 条")


def _detect_chrome_path() -> str:
    candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    if platform.system() == "Linux":
        candidates.extend(
            _glob_mod.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
        )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return ""


_chrome_path = _detect_chrome_path()
if _chrome_path:
    print(f"[*] 浏览器路径: {_chrome_path}")


def create_browser_options():
    # 参考 AaronL725/browser_runtime.create_browser_options：参数尽量少，贴近普通 Chrome。
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    try:
        options.headless(False)
    except Exception:
        pass
    # 容器里仍需要这两项才能拉起 Chrome；不要再加 disable-gpu / AutomationControlled。
    if platform.system() == "Linux":
        options.set_argument("--no-sandbox")
        options.set_argument("--disable-dev-shm-usage")
    if _chrome_path:
        options.set_browser_path(_chrome_path)
    if _browser_proxy:
        options.set_proxy(_browser_proxy)
    if os.path.isdir(EXTENSION_PATH):
        options.add_extension(EXTENSION_PATH)
    return options


browser = None
page = None

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_sso_dir = os.path.join(os.path.dirname(__file__), "sso")
_sso_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
DEFAULT_SSO_FILE = os.path.join(_sso_dir, f"sso_{_sso_ts}.txt")


def start_browser(force_rotate: bool = False):
    # 对齐参考实现：每次启动都新建 ChromiumOptions；失败才换代理。
    global browser, page
    last_exc = None
    attempts = max(4, len(_proxy_pool) or 1)
    for attempt in range(1, attempts + 1):
        select_browser_proxy(force_rotate=force_rotate or attempt > 1)
        try:
            browser = Chromium(create_browser_options())
            tabs = browser.get_tabs()
            page = tabs[-1] if tabs else browser.new_tab()
            if attempt > 1:
                print(f"[*] 浏览器第 {attempt} 次启动成功")
            return browser, page
        except Exception as exc:
            last_exc = exc
            print(f"[Debug] 浏览器启动失败(第{attempt}/{attempts}次): {exc}")
            mark_proxy_dead(str(exc).split("\n", 1)[0][:120])
            try:
                if browser is not None:
                    browser.quit(del_data=True)
            except Exception:
                pass
            browser = None
            page = None
            time.sleep(min(1.2 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试{attempts}次: {last_exc}")


def stop_browser():
    # 完整关闭浏览器，del_data=True 让 DrissionPage 清掉 auto_port 分配的临时 profile。
    global browser, page
    if browser is not None:
        try:
            try:
                browser.quit(del_data=True)
            except TypeError:
                browser.quit()
        except Exception:
            pass
    browser = None
    page = None


def restart_browser(force_rotate: bool = True):
    # 失败换代理时完整重启；成功轮次优先 reset_signup_session。
    stop_browser()
    start_browser(force_rotate=force_rotate)


def reset_signup_session():
    # 成功后清 cookie / storage，复用同一 Chrome 和代理，比整轮 quit 快。
    global page
    try:
        if page is not None:
            try:
                page.set.cookies.clear()
            except Exception:
                pass
            try:
                page.run_js("try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}")
            except Exception:
                pass
        if browser is not None:
            try:
                browser.set.cookies.clear()
            except Exception:
                pass
        refresh_active_page()
        if page is not None:
            page.get("about:blank")
        return
    except Exception as exc:
        print(f"[Debug] 会话重置失败，改为重启浏览器: {exc}")
        restart_browser(force_rotate=False)


def refresh_active_page():
    # 验证码确认后页面会跳转，旧 page 句柄可能断开，这里统一重新获取当前活动标签页。
    global browser, page
    if browser is None:
        start_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
    except Exception:
        restart_browser()
    return page


def open_signup_page():
    # 每轮开始时打开注册页，并切到“使用邮箱注册”流程。
    global page
    refresh_active_page()
    last_error = None
    for attempt in range(3):
        try:
            page.get(SIGNUP_URL)
            time.sleep(0.6)
            click_email_signup_button(timeout=15)
            return
        except Exception as exc:
            last_error = exc
            print(f"[Debug] 打开注册页失败({attempt + 1}/3): {exc}")
            refresh_active_page()
            time.sleep(0.8)
    raise Exception(f"打开注册页失败: {last_error}")


def close_current_page():
    # 兼容旧调用名，实际行为改为整轮重启浏览器。
    restart_browser()


def has_profile_form():
    # 最终注册页只要出现姓名和密码输入框，就认为已经成功进入资料填写阶段。
    refresh_active_page()
    try:
        return bool(page.run_js(
            """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
        ))
    except Exception:
        return False


def click_email_signup_button(timeout=15):
    # 页面打开后，自动点击“使用邮箱注册”按钮。
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            clicked = page.run_js(r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text.includes('使用邮箱注册') || text.includes('signupwithemail') || text.includes('signupemail') || text.includes('continuewith email') || text.includes('email');
});

if (!target) {
    return false;
}

target.click();
return true;
            """)
        except Exception:
            refresh_active_page()
            time.sleep(0.5)
            continue

        if clicked:
            return True

        time.sleep(0.5)

    raise Exception('未找到“使用邮箱注册”按钮')


def _visible_page_text() -> str:
    refresh_active_page()
    try:
        return str(page.run_js("return (document.body && document.body.innerText) || ''") or "")
    except Exception:
        return ""


def _signup_email_feedback() -> str:
    # 提交邮箱后立刻判断：域名被拒 / 进入验证码 / 进入资料页。
    text = _visible_page_text()
    lower = text.lower()
    if (
        "has been rejected" in lower
        or "use a different email" in lower
        or ("邮箱域名" in text and "拒绝" in text)
        or "已被拒绝" in text
    ):
        return "rejected"
    if has_profile_form():
        return "profile"
    try:
        otp_ready = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const otp = Array.from(document.querySelectorAll(
    'input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]'
)).some((node) => isVisible(node) && !node.disabled);
return otp;
            """
        )
        if otp_ready:
            return "otp"
    except Exception:
        pass
    return "pending"


def _email_prefetch_executor():
    global _email_prefetch_pool
    if _email_prefetch_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _email_prefetch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="email-prefetch")
    return _email_prefetch_pool


def prefetch_signup_email(exclude_domains=None):
    # 等 Turnstile / OTP 时后台先开下一封 DuckMail，下一轮少等 1–3 秒。
    global _email_prefetch_future
    excluded = list(exclude_domains or [])
    with _email_prefetch_lock:
        if _email_prefetch_future is not None and not _email_prefetch_future.done():
            return
        _email_prefetch_future = _email_prefetch_executor().submit(get_email_and_token, excluded)


def take_signup_email(exclude_domains=None):
    global _email_prefetch_future
    with _email_prefetch_lock:
        future = _email_prefetch_future
        _email_prefetch_future = None
    excluded = [str(item).strip().lstrip("@").lower() for item in (exclude_domains or []) if item]
    if future is not None:
        try:
            email, token = future.result(timeout=20)
            domain = email.split("@")[-1].lower() if email and "@" in email else ""
            if email and token and domain not in excluded:
                prefetch_signup_email(exclude_domains)
                return email, token
        except Exception as exc:
            print(f"[Debug] 预取邮箱不可用，改为现取: {exc}")
    email, token = get_email_and_token(exclude_domains=exclude_domains)
    prefetch_signup_email(exclude_domains)
    return email, token


def fill_email_and_submit(timeout=45):
    # 复用 `email_register.py` 里的邮箱获取逻辑；x.ai 会拒绝 duckmail.sbs，被拒后自动换域重试。
    excluded_domains: list = []
    last_error = "获取邮箱失败"
    attempt_deadline = time.time() + timeout

    while time.time() < attempt_deadline:
        email, dev_token = take_signup_email(exclude_domains=excluded_domains)
        if not email or not dev_token:
            raise Exception(last_error)

        write_deadline = min(time.time() + 15, attempt_deadline)
        submitted = False
        while time.time() < write_deadline:
            filled = page.run_js(
            """
const email = arguments[0];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input) {
    return 'not-ready';
}

input.focus();
input.click();

// 不能只写 `input.value = xxx`，否则 React / 受控表单可能没有同步内部状态。
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) {
    tracker.setValue('');
}
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) {
    return false;
}

input.blur();
return 'filled';
            """,
            email,
            )

            if filled == 'not-ready':
                time.sleep(0.5)
                continue

            if filled != 'filled':
                print(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
                time.sleep(0.5)
                continue

            time.sleep(0.8)
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input || !input.checkValidity() || !(input.value || '').trim()) {
    return false;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '注册' || text.includes('注册') || t === 'signup' || t === 'sign up' || t.includes('sign up');
});

if (!submitButton || submitButton.disabled) {
    return false;
}

submitButton.click();
return true;
                """
            )

            if clicked:
                print(f"[*] 已填写邮箱并点击注册: {email}")
                submitted = True
                break

            time.sleep(0.5)

        if not submitted:
            last_error = "未找到邮箱输入框或注册按钮"
            continue

        feedback = "pending"
        wait_deadline = min(time.time() + 8, attempt_deadline)
        while time.time() < wait_deadline:
            feedback = _signup_email_feedback()
            if feedback in ("otp", "profile", "rejected"):
                break
            time.sleep(0.4)

        if feedback in ("otp", "profile"):
            return email, dev_token

        if feedback == "rejected":
            domain = email.split("@")[-1].lower() if "@" in email else email
            if domain not in excluded_domains:
                excluded_domains.append(domain)
            last_error = f"邮箱域名已被 x.ai 拒绝: {domain}"
            print(f"[Warn] {last_error}，改用其它 DuckMail 域名重试")
            continue

        # 没有明确错误也没有验证码页时，仍把当前邮箱交给后续 OTP 轮询。
        return email, dev_token

    raise Exception(last_error)



def fill_code_and_submit(email, dev_token, timeout=180):
    # 复用 `email_register.py` 里的验证码轮询逻辑，等待邮件到达后自动填写 OTP。
    prefetch_signup_email()
    code = get_oai_code(dev_token, email, timeout=180)
    if not code:
        raise Exception("获取验证码失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || code.length || 6) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (!input && otpBoxes.length < code.length) {
    return 'not-ready';
}

if (input) {
    input.focus();
    input.click();
    setNativeValue(input, code);
    dispatchInputEvents(input, code);

    const normalizedValue = String(input.value || '').trim();
    const expectedLength = Number(input.maxLength || code.length || 6);
    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;

    if (normalizedValue !== code) {
        return 'aggregate-mismatch';
    }

    if (expectedLength > 0 && normalizedValue.length !== expectedLength) {
        return 'aggregate-length-mismatch';
    }

    if (slots.length && filledSlots && filledSlots !== normalizedValue.length) {
        return 'aggregate-slot-mismatch';
    }

    input.blur();
    return 'filled';
}

const orderedBoxes = otpBoxes.slice(0, code.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = code[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged === code ? 'filled' : 'box-mismatch';
                """,
                code,
            )
        except PageDisconnectedError:
            # 点击确认邮箱后如果刚好发生跳转，旧页面句柄会断开；此时切到新页继续判断即可。
            refresh_active_page()
            if has_profile_form():
                print("[*] 验证码提交后已跳转到最终注册页。")
                return code
            time.sleep(1)
            continue

        if filled == 'not-ready':
            if has_profile_form():
                print("[*] 已直接进入最终注册页，跳过验证码按钮确认。")
                return code
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 验证码输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(1.2)
            try:
                clicked = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const expectedLength = Number(aggregateInput.maxLength || value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) {
        return false;
    }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    if (slots.length) {
        const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;
        if (filledSlots && filledSlots !== value.length) {
            return false;
        }
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) {
            return false;
        }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) {
        return false;
    }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '确认邮箱' || text.includes('确认邮箱') || text === '继续' || text.includes('继续') || text === '下一步' || text.includes('下一步') || t.includes('confirm') || t.includes('continue') || t.includes('next') || t.includes('verify');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
confirmButton.click();
return 'clicked';
                    """
                )
            except PageDisconnectedError:
                refresh_active_page()
                if has_profile_form():
                    print("[*] 确认邮箱后页面跳转成功，已进入最终注册页。")
                    return code
                clicked = 'disconnected'

            if clicked == 'clicked':
                print(f"[*] 已填写验证码并点击确认邮箱: {code}")
                time.sleep(2)
                refresh_active_page()
                if has_profile_form():
                    print("[*] 验证码确认完成，最终注册页已就绪。")
                return code

            if clicked == 'no-button':
                current_url = page.url
                if 'sign-up' in current_url or 'signup' in current_url:
                    print(f"[*] 已填写验证码，页面已自动跳转到下一步: {current_url}")
                    return code

            if clicked == 'disconnected':
                time.sleep(1)
                continue

        time.sleep(0.5)

    debug_snapshot = page.run_js(
        r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible).map((node) => ({
    type: node.type || '',
    name: node.name || '',
    testid: node.getAttribute('data-testid') || '',
    autocomplete: node.autocomplete || '',
    maxLength: Number(node.maxLength || 0),
    value: String(node.value || ''),
}));

const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim(),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
}));

return { url: location.href, inputs, buttons };
        """
    )
    print(f"[Debug] 验证码页 DOM 摘要: {debug_snapshot}")
    raise Exception("未找到验证码输入框或确认邮箱按钮")


def getTurnstileToken(max_tries=8):
    # 对齐 AaronL725/grok-register：token 长度 >= 80 才算通过；先等自动签发，卡住再点 checkbox。
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    try:
        page.run_js(
            "try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {}"
        )
    except Exception:
        pass

    last_error = ""
    for i in range(max(1, int(max_tries))):
        try:
            if page is None:
                refresh_active_page()
            token = page.run_js(
                """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    return String(turnstile.getResponse() || '').trim();
  }
  return '';
} catch(e) { return ''; }
                """
            )
            token = str(token or "").strip()
            if len(token) >= 80:
                print(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            # 对齐参考实现：用默认超时找 iframe，不要额外 timeout 把页面拖死。
            challenge_input = page.ele("@name=cf-turnstile-response")
            if challenge_input:
                iframe = None
                try:
                    iframe = challenge_input.parent().shadow_root.ele("tag:iframe")
                except Exception:
                    iframe = None
                if iframe:
                    try:
                        iframe.run_js(
                            """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                            """
                        )
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input")
                        if btn:
                            btn.click()
                    except Exception as error:
                        last_error = str(error)
            else:
                page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter((n) => {
  const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
  return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
                    """
                )
            if i == 0 or i % 4 == 3:
                print(f"[*] 等待 Turnstile token（{i + 1}/{max_tries}）")
        except PageDisconnectedError as error:
            last_error = str(error)
            print("[Debug] Turnstile 检测时页面断开，刷新标签页后继续")
            refresh_active_page()
        except Exception as error:
            last_error = str(error)
        time.sleep(1)

    raise Exception(f"Turnstile 获取 token 失败{': ' + last_error if last_error else ''}")


def build_profile():
    # 对齐参考实现：随机姓名，避免固定 Neo Lin 被风控。
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = secrets.choice(given_name_pool)
    family_name = secrets.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=75):
    # 对齐 AaronL725：资料只填一次，避免反复写入把 Turnstile 冲掉；token 长度>=80 再提交。
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        try:
            refresh_active_page()
            if page is None:
                time.sleep(1)
                continue

            if not form_filled_once:
                filled = page.run_js(
            """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) {
        return false;
    }
    input.focus();
    input.click();

    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }

    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }

    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));

    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return 'not-ready';
}

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);

if (!givenOk || !familyOk || !passwordOk) {
    return 'filled-failed';
}

return [
    String(givenInput.value || '').trim() === String(givenName || '').trim(),
    String(familyInput.value || '').trim() === String(familyName || '').trim(),
    String(passwordInput.value || '') === String(password || ''),
].every(Boolean) ? 'filled' : 'verify-failed';
            """,
            given_name,
            family_name,
            password,
        )

                if filled == 'not-ready':
                    time.sleep(0.5)
                    continue

                if filled != 'filled':
                    print(f"[Debug] 最终注册页输入框已出现，但姓名/密码写入失败: {filled}")
                    time.sleep(0.5)
                    continue

                values_ok = page.run_js(
                    """
const expectedGiven = arguments[0];
const expectedFamily = arguments[1];
const expectedPassword = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return false;
}

return String(givenInput.value || '').trim() === String(expectedGiven || '').trim()
    && String(familyInput.value || '').trim() === String(expectedFamily || '').trim()
    && String(passwordInput.value || '') === String(expectedPassword || '');
                    """,
                    given_name,
                    family_name,
                    password,
                )
                if not values_ok:
                    print("[Debug] 最终注册页字段值校验失败，继续重试填写。")
                    time.sleep(0.5)
                    continue

                form_filled_once = True
                print(f"[*] 资料已填写: {given_name} {family_name}")

            turnstile_state = page.run_js(
                """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!challengeInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (!cfPresent) {
    return 'not-found';
}
const value = String((challengeInput && challengeInput.value) || '').trim();
return value.length >= 80 ? 'ready' : ('pending:' + value.length);
                """
            )

            if isinstance(turnstile_state, str) and turnstile_state.startswith("pending"):
                token_len = turnstile_state.split(":", 1)[1] if ":" in turnstile_state else "0"
                now = time.time()
                try:
                    token_len_int = int(token_len)
                except Exception:
                    token_len_int = 0
                if wait_cf_since is None:
                    wait_cf_since = now
                    print(f"[*] 资料已填写，等待 Cloudflare 自动通过... 当前token长度={token_len}")
                    prefetch_signup_email()
                if token_len_int < 80 and last_cf_retry_at == 0.0:
                    print("[*] Cloudflare 未自动签发，立即复用 Turnstile...")
                    try:
                        turnstile_token = getTurnstileToken(max_tries=4)
                        if turnstile_token:
                            synced = page.run_js(
                                """
const token = String(arguments[0] || '').trim();
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput || !token) {
    return 0;
}
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) {
    nativeSetter.call(challengeInput, token);
} else {
    challengeInput.value = token;
}
challengeInput.dispatchEvent(new Event('input', { bubbles: true }));
challengeInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(challengeInput.value || '').trim().length;
                                """,
                                turnstile_token,
                            )
                            print(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                            if int(synced or 0) >= 80:
                                last_cf_retry_at = now
                                continue
                    except Exception as cf_exc:
                        print(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                    last_cf_retry_at = now
                if token_len_int < 80 and now - wait_cf_since >= _CF_ZERO_TOKEN_SECONDS:
                    raise Exception("Cloudflare token 持续为 0，换代理")
                time.sleep(0.5)
                continue

            time.sleep(1.2)

            try:
                submit_button = page.ele('tag:button@@text()=完成注册') or page.ele('tag:button@@text():Create Account') or page.ele('tag:button@@text():Sign up')
            except Exception:
                submit_button = None

            if not submit_button:
                clicked = page.run_js(
                    r"""
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (challengeInput && String(challengeInput.value || '').trim().length < 80) {
    return false;
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '完成注册' || text.includes('完成注册') || t.includes('create account') || t.includes('sign up') || t.includes('complete');
});
if (!submitButton || submitButton.disabled || submitButton.getAttribute('aria-disabled') === 'true') {
    return false;
}
submitButton.focus();
submitButton.click();
return true;
                    """
                )
            else:
                challenge_value = page.run_js(
                    """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
return challengeInput ? String(challengeInput.value || '').trim() : 'not-found';
                    """
                )
                if len(str(challenge_value or "")) >= 80 or challenge_value == 'not-found':
                    submit_button.click()
                    clicked = True
                else:
                    clicked = False

            if clicked:
                print(f"[*] 已填写注册资料并点击完成注册: {given_name} {family_name} / {password}")
                return {
                    "given_name": given_name,
                    "family_name": family_name,
                    "password": password,
                }

            time.sleep(0.5)
        except PageDisconnectedError:
            print("[Debug] 资料页断开，刷新标签页后继续")
            refresh_active_page()
            time.sleep(1)

    raise Exception("未找到最终注册表单或完成注册按钮")


def extract_visible_numbers(timeout=60):
    # 登录/注册完成后，提取页面上可见的普通数字文本，不处理任何敏感 Cookie。
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = page.run_js(
            r"""
function isVisible(el) {
    if (!el) {
        return false;
    }
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const selector = [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'div', 'span', 'p', 'strong', 'b', 'small',
    '[data-testid]', '[class]', '[role="heading"]'
].join(',');

const seen = new Set();
const matches = [];
for (const node of document.querySelectorAll(selector)) {
    if (!isVisible(node)) {
        continue;
    }
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text) {
        continue;
    }
    const found = text.match(/\d+(?:\.\d+)?/g);
    if (!found) {
        continue;
    }
    for (const value of found) {
        const key = `${value}@@${text}`;
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        matches.push({ value, text });
    }
}

return matches.slice(0, 30);
            """
        )

        if result:
            print("[*] 页面可见数字文本提取结果:")
            for item in result:
                try:
                    print(f"    - 数字: {item['value']} | 上下文: {item['text']}")
                except Exception:
                    pass
            return result

        time.sleep(1)

    raise Exception("登录后未提取到可见数字文本")


def wait_for_sso_cookie(timeout=30):
    # 必须在注册完成后再取 sso，优先抓取精确的 sso cookie。
    deadline = time.time() + timeout
    last_seen_names = set()

    while time.time() < deadline:
        try:
            refresh_active_page()
            if page is None:
                time.sleep(1)
                continue

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    print("[*] 注册完成后已获取到 sso cookie。")
                    return value

        except PageDisconnectedError:
            refresh_active_page()
        except Exception:
            pass

        time.sleep(1)

    raise Exception(f"注册完成后未获取到 sso cookie，当前已见 cookie: {sorted(last_seen_names)}")


def append_sso_to_txt(sso_value, output_path=DEFAULT_SSO_FILE):
    # 按用户要求，一行写一个 sso 值，持续追加。
    normalized = str(sso_value or "").strip()
    if not normalized:
        raise Exception("待写入的 sso 为空")

    parent_dir = os.path.dirname(os.path.abspath(output_path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    with open(output_path, "a", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        try:
            file.write(normalized + "\n")
            file.flush()
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    print(f"[*] 已追加写入 sso 到文件: {output_path}")


def _load_api_config() -> dict:
    import json
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r", encoding="utf-8") as handle:
        conf = json.load(handle)
    api_conf = dict(conf.get("api") or {})
    # 兼容 AaronL725 风格的顶层字段。
    if not api_conf.get("base"):
        api_conf["base"] = conf.get("grok2api_remote_base", "")
    if not api_conf.get("admin_username"):
        api_conf["admin_username"] = conf.get("grok2api_remote_admin_username", "")
    if not api_conf.get("admin_password"):
        api_conf["admin_password"] = conf.get("grok2api_remote_admin_password", "")
    return api_conf


def _normalize_sso_token(raw) -> str:
    value = str(raw or "").strip()
    if value.lower().startswith("sso="):
        value = value.split(";", 1)[0][4:].strip()
    return value.strip().strip("\"'")


def _grok2api_go_admin_base(base: str) -> str:
    import urllib.parse
    normalized = str(base or "").strip().rstrip("/")
    if not normalized:
        return ""
    for suffix in ("/api/admin/v1", "/admin/api", "/admin"):
        if normalized.lower().endswith(suffix):
            normalized = normalized[: -len(suffix)].rstrip("/")
            break
    host = (urllib.parse.urlsplit(normalized).hostname or "").lower()
    if urllib.parse.urlsplit(normalized).scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise Exception("新版 grok2api 非本机地址必须使用 HTTPS")
    return normalized + "/api/admin/v1"


_grok2api_admin_token = ""
_grok2api_admin_expires = 0.0
_grok2api_admin_session = ""


def _grok2api_admin_login(api_base: str, username: str, password: str, force: bool = False) -> str:
    global _grok2api_admin_token, _grok2api_admin_expires, _grok2api_admin_session
    import hashlib
    import requests

    session_key = "%s\n%s\n%s" % (api_base, username, hashlib.sha256(password.encode("utf-8")).hexdigest())
    if (
        not force
        and _grok2api_admin_session == session_key
        and _grok2api_admin_token
        and _grok2api_admin_expires > time.time() + 30
    ):
        return _grok2api_admin_token

    resp = requests.post(
        api_base + "/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    if not 200 <= resp.status_code < 300:
        raise Exception(f"grok2api 管理员登录失败: HTTP {resp.status_code}")
    tokens = (resp.json().get("data") or {}).get("tokens") or {}
    token = str(tokens.get("accessToken") or "").strip()
    if not token:
        raise Exception("grok2api 登录响应缺少 accessToken")
    expiry = str(tokens.get("accessTokenExpiresAt") or "").strip()
    try:
        expires_at = datetime.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        _grok2api_admin_expires = expires_at.timestamp()
    except Exception:
        _grok2api_admin_expires = time.time() + 300
    _grok2api_admin_token = token
    _grok2api_admin_session = session_key
    return token


def _parse_grok2api_sse(text: str, action: str) -> dict:
    import json
    event = ""
    completed = None
    error_message = ""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip()) if line[5:].strip().startswith("{") else {}
        except Exception:
            data = {}
        if event == "error":
            error_message = str((data or {}).get("message") or (data or {}).get("error") or line[5:200])
        if event == "complete" and isinstance(data, dict):
            completed = data
    if error_message:
        raise Exception(f"grok2api {action}失败: " + error_message[:200])
    if completed is None:
        raise Exception(f"grok2api {action}响应缺少 complete 事件")
    return completed


def _read_grok2api_sse(resp, action: str) -> dict:
    chunks = []
    for line in resp.iter_lines(decode_unicode=True):
        if line:
            chunks.append(line)
    return _parse_grok2api_sse("\n".join(chunks), action)


def _convert_to_build_enabled(api_conf: dict) -> bool:
    if os.environ.get("GROK_REGISTER_CONVERT_TO_BUILD") == "0":
        return False
    if "convert_to_build" not in api_conf:
        return True
    return bool(api_conf.get("convert_to_build"))


def _parse_iso_datetime(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except Exception:
        pass
    if "." in text:
        head, rest = text.split(".", 1)
        frac = ""
        tz = ""
        for index, char in enumerate(rest):
            if char.isdigit():
                frac += char
            else:
                tz = rest[index:]
                break
        text = head + "." + (frac[:6].ljust(6, "0")) + tz
        try:
            return datetime.datetime.fromisoformat(text)
        except Exception:
            return None
    return None


def _list_unlinked_web_ids(api_base: str, access: str, *, recent_seconds: int = 0, limit: int = 1000) -> list:
    import requests

    ids = []
    page = 1
    page_size = 100
    cutoff = None
    if recent_seconds > 0:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=recent_seconds)
    while len(ids) < limit:
        resp = requests.get(
            api_base + "/accounts",
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
            params={
                "page": page,
                "pageSize": page_size,
                "provider": "grok_web",
                "association": "buildUnlinked",
                "sortBy": "createdAt",
                "sortOrder": "desc",
            },
            timeout=30,
        )
        if not 200 <= resp.status_code < 300:
            raise Exception(f"grok2api 读取未关联 Web 账号失败: HTTP {resp.status_code}")
        payload = resp.json().get("data") or {}
        items = payload.get("items") or []
        if not items:
            break
        stop = False
        for item in items:
            created_at = _parse_iso_datetime(str(item.get("createdAt") or ""))
            if created_at is not None and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=datetime.timezone.utc)
            if cutoff is not None and created_at is not None and created_at < cutoff:
                stop = True
                break
            account_id = str(item.get("id") or "").strip()
            if account_id:
                ids.append(account_id)
            if len(ids) >= limit:
                break
        if stop or len(items) < page_size:
            break
        page += 1
    return ids


def _convert_web_to_build(api_base: str, access: str, *, ids=None, convert_all: bool = False) -> dict:
    import requests

    body = {"strategy": "missing"}
    if convert_all:
        body["all"] = True
        timeout = 6 * 60 * 60
    else:
        ids = [str(item).strip() for item in (ids or []) if str(item).strip()]
        if not ids:
            return {"created": 0, "linked": 0, "skipped": 0, "failed": 0, "synced": 0, "syncFailed": 0}
        body["ids"] = ids
        timeout = min(max(180, 90 * len(ids)), 30 * 60)
    last_error = ""
    for attempt in range(3):
        try:
            resp = requests.post(
                api_base + "/accounts/web/convert-to-build",
                headers={
                    "Authorization": f"Bearer {access}",
                    "Accept": "text/event-stream",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=timeout,
                stream=True,
            )
            if not 200 <= resp.status_code < 300:
                raise Exception(f"grok2api Web 转 Build 失败: HTTP {resp.status_code}")
            return _read_grok2api_sse(resp, "Web 转 Build")
        except Exception as exc:
            last_error = str(exc)
            if attempt >= 2:
                break
            time.sleep(2 * (attempt + 1))
    raise Exception(last_error or "grok2api Web 转 Build 失败")


def _convert_imported_web_to_build(api_base: str, access: str, imported: int) -> None:
    recent_ids = _list_unlinked_web_ids(api_base, access, recent_seconds=30 * 60, limit=1000)
    if recent_ids:
        result = _convert_web_to_build(api_base, access, ids=recent_ids)
    elif imported >= 20:
        result = _convert_web_to_build(api_base, access, convert_all=True)
    elif imported > 0:
        result = _convert_web_to_build(
            api_base,
            access,
            ids=_list_unlinked_web_ids(api_base, access, limit=min(imported, 1000)),
        )
    else:
        return
    print(
        "[*] 已转换 grok2api Grok Build: created={created} linked={linked} "
        "skipped={skipped} failed={failed} synced={synced} syncFailed={syncFailed}".format(
            created=int(result.get("created") or 0),
            linked=int(result.get("linked") or 0),
            skipped=int(result.get("skipped") or 0),
            failed=int(result.get("failed") or 0),
            synced=int(result.get("synced") or 0),
            syncFailed=int(result.get("syncFailed") or 0),
        )
    )
    if int(result.get("failed") or 0) or int(result.get("syncFailed") or 0):
        created = int(result.get("created") or 0)
        linked = int(result.get("linked") or 0)
        if created + linked <= 0:
            raise Exception("Web 已导入，但转 Build 未全部成功")
        print("[Warn] 部分 Web 转 Build 未成功，下一轮会重试未关联号")


def push_sso_to_grok2api_go(new_tokens: list, api_conf: dict) -> bool:
    import requests

    tokens = [_normalize_sso_token(item) for item in new_tokens]
    tokens = [item for item in tokens if item]
    if not tokens:
        return False
    api_base = _grok2api_go_admin_base(str(api_conf.get("base") or ""))
    username = str(api_conf.get("admin_username") or "").strip()
    password = str(api_conf.get("admin_password") or "")
    payload = ("\n".join(tokens) + "\n").encode("utf-8")
    last_error = ""
    for attempt in range(2):
        access = _grok2api_admin_login(api_base, username, password, force=attempt > 0)
        resp = requests.post(
            api_base + "/accounts/web/import",
            headers={"Authorization": f"Bearer {access}", "Accept": "text/event-stream"},
            files={"file": ("grok-web-sso.txt", payload, "text/plain; charset=utf-8")},
            timeout=120,
        )
        if resp.status_code == 401 and attempt == 0:
            continue
        if not 200 <= resp.status_code < 300:
            raise Exception(f"grok2api Web SSO 导入失败: HTTP {resp.status_code}")
        result = _parse_grok2api_sse(resp.text, "导入")
        created = int(result.get("created") or 0)
        updated = int(result.get("updated") or 0)
        skipped = int(result.get("skipped") or 0)
        synced = int(result.get("synced") or 0)
        sync_failed = int(result.get("syncFailed") or 0)
        print(
            f"[*] 已导入 grok2api Grok Web: created={created} updated={updated} "
            f"skipped={skipped} synced={synced} syncFailed={sync_failed}"
        )
        if sync_failed:
            raise Exception("SSO 已导入，但初始同步失败")
        if _convert_to_build_enabled(api_conf):
            _convert_imported_web_to_build(api_base, access, created + updated)
        return True
    raise Exception(last_error or "grok2api 管理员认证已失效")


def push_sso_to_api(new_tokens: list):
    # 优先走新版 grok2api（管理员登录 + /accounts/web/import，默认再转 Build）；
    # 否则回退旧版 Python grok2api 的 ssoBasic 全量保存。
    import json
    import urllib3
    import requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        api_conf = _load_api_config()
    except Exception as e:
        print(f"[Warn] 读取 config.json 失败，跳过推送: {e}")
        return

    tokens_to_push = [_normalize_sso_token(item) for item in new_tokens]
    tokens_to_push = [item for item in tokens_to_push if item]
    if not tokens_to_push:
        return

    base = str(api_conf.get("base") or "").strip()
    username = str(api_conf.get("admin_username") or "").strip()
    password = str(api_conf.get("admin_password") or "")
    if base and username and password:
        try:
            push_sso_to_grok2api_go(tokens_to_push, api_conf)
        except Exception as exc:
            print(f"[Warn] 推送 grok2api(Go) 失败: {exc}")
        return

    endpoint = str(api_conf.get("endpoint", "")).strip()
    api_token = str(api_conf.get("token", "")).strip()
    append_mode = api_conf.get("append", True)

    if not endpoint or not api_token:
        return

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    tokens_to_push = [t for t in new_tokens if t]

    if append_mode:
        try:
            get_resp = requests.get(endpoint, headers=headers, timeout=15, verify=False)
            if get_resp.status_code == 200:
                data = get_resp.json()
                # 兼容两种响应格式：
                # 新版: {"tokens": {"ssoBasic": [...]}}
                # 旧版: {"ssoBasic": [...]}
                if isinstance(data, dict) and isinstance(data.get("tokens"), dict):
                    existing = data["tokens"].get("ssoBasic", [])
                else:
                    existing = data.get("ssoBasic", []) if isinstance(data, dict) else []
                existing_tokens = [
                    item["token"] if isinstance(item, dict) else str(item)
                    for item in existing if item
                ]
                seen = set()
                deduped = []
                for t in existing_tokens + tokens_to_push:
                    if t not in seen:
                        seen.add(t)
                        deduped.append(t)
                tokens_to_push = deduped
                print(f"[*] 查询到线上 {len(existing_tokens)} 个 token，合并本次 {len(new_tokens)} 个，共 {len(deduped)} 个")
            else:
                print(f"[Error] 查询线上 token 失败: HTTP {get_resp.status_code}，放弃推送以保护存量数据")
                return
        except Exception as e:
            print(f"[Error] 查询线上 token 异常: {e}，放弃推送以保护存量数据")
            return

    try:
        resp = requests.post(
            endpoint,
            json={"ssoBasic": tokens_to_push},
            headers=headers,
            timeout=60,
            verify=False,
        )
        if resp.status_code == 200:
            print(f"[*] SSO token 已推送到 API（共 {len(tokens_to_push)} 个）: {endpoint}")
        else:
            print(f"[Warn] 推送 API 返回异常: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[Warn] 推送 API 失败: {e}")


def run_single_registration(output_path=DEFAULT_SSO_FILE, extract_numbers=False):
    # 单轮流程：打开注册页 -> 完成注册 -> 获取 sso -> 写 txt。
    prefetch_signup_email()
    open_signup_page()
    email, dev_token = fill_email_and_submit()
    fill_code_and_submit(email, dev_token)
    profile = fill_profile_and_submit()
    sso_value = wait_for_sso_cookie()
    append_sso_to_txt(sso_value, output_path)

    if extract_numbers:
        extract_visible_numbers()

    result = {
        "email": email,
        "sso": sso_value,
        **profile,
    }

    if run_logger:
        run_logger.info(
            "注册成功 | email=%s | password=%s | given=%s | family=%s | proxy=%s",
            email,
            profile.get("password", ""),
            profile.get("given_name", ""),
            profile.get("family_name", ""),
            _browser_proxy,
        )

    print(f"[*] 本轮注册完成，邮箱: {email}")
    return result


def _load_run_section() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
        section = conf.get("run") or {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def load_run_count() -> int:
    # 从 config.json 读取默认执行轮数，配置不存在时返回 10。
    v = _load_run_section().get("count")
    if isinstance(v, int) and v >= 0:
        return v
    return 10


def load_run_workers() -> int:
    v = _load_run_section().get("workers")
    if isinstance(v, int) and v >= 1:
        return v
    return 1


BROWSER_RECYCLE_EVERY = 20
SINGLE_ROUND_TIMEOUT = 150


def _run_single_registration_with_timeout(output_path, extract_numbers=False, timeout=SINGLE_ROUND_TIMEOUT):
    # Chrome/CDP 卡住时 Python 自己的 while 超时不会触发。到点强制 quit。
    stop_event = threading.Event()

    def _kill_hung_browser():
        if stop_event.wait(timeout):
            return
        print(f"[Warn] 单轮超过 {timeout}s，强制关闭 Chrome")
        try:
            stop_browser()
        except Exception:
            pass

    watcher = threading.Thread(target=_kill_hung_browser, daemon=True)
    watcher.start()
    try:
        return run_single_registration(output_path, extract_numbers=extract_numbers)
    finally:
        stop_event.set()


def run_batch_rounds(count, output_path, extract_numbers=False, worker_id=None):
    # 单进程内循环注册。成功则清会话复用 Chrome；失败才换代理并重启。
    collected_sso: list = []
    success_count = 0
    fail_count = 0
    current_round = 0
    prefix = f"[W{worker_id}] " if worker_id is not None else ""

    try:
        start_browser()
        reuse_ok = False
        while True:
            if count > 0 and current_round >= count:
                break

            current_round += 1
            print(f"\n[*] {prefix}开始第 {current_round} 轮注册（成功 {success_count} / 失败 {fail_count}）")
            more_rounds = count == 0 or current_round < count
            reuse_ok = False

            try:
                result = _run_single_registration_with_timeout(output_path, extract_numbers=extract_numbers)
                collected_sso.append(result["sso"])
                success_count += 1
                reuse_ok = True
                note_proxy_success()
                try:
                    push_sso_to_api([result["sso"]])
                except Exception as push_exc:
                    print(f"[Warn] {prefix}本轮推送 grok2api 失败: {push_exc}")
            except KeyboardInterrupt:
                print(f"\n[Info] {prefix}收到中断信号，停止后续轮次。")
                break
            except Exception as error:
                fail_count += 1
                print(f"[Error] {prefix}第 {current_round} 轮失败: {error}")
                err_text = str(error)
                email_only = any(
                    marker in err_text
                    for marker in ("DuckMail", "验证码", "邮箱被拒", "邮箱域名", "邮件")
                )
                if not email_only:
                    mark_proxy_dead(err_text.split("\n", 1)[0][:120])
                if run_logger:
                    run_logger.error(
                        "注册失败 | round=%s | worker=%s | proxy=%s | error=%s",
                        current_round,
                        worker_id,
                        _browser_proxy,
                        error,
                    )
            finally:
                if more_rounds:
                    recycle = reuse_ok and success_count > 0 and success_count % BROWSER_RECYCLE_EVERY == 0
                    if reuse_ok and not recycle:
                        try:
                            reset_signup_session()
                        except Exception as reset_exc:
                            print(f"[Warn] {prefix}会话重置失败，改为重启浏览器: {reset_exc}")
                            restart_browser(force_rotate=False)
                    else:
                        if recycle:
                            print(f"[*] {prefix}已成功 {success_count} 轮，重启 Chrome 防止会话发粘")
                        restart_browser(force_rotate=not reuse_ok)

            if more_rounds:
                time.sleep(0.3 if reuse_ok else 0.5)
        print(f"\n[*] {prefix}本批结束：成功 {success_count}，失败 {fail_count}，共 {current_round} 轮")
        return success_count, fail_count, collected_sso
    finally:
        try:
            api_conf = _load_api_config()
        except Exception:
            api_conf = {}
        go_mode = bool(
            str(api_conf.get("base") or "").strip()
            and str(api_conf.get("admin_username") or "").strip()
            and api_conf.get("admin_password")
        )
        if collected_sso and not go_mode:
            print(f"\n[*] {prefix}注册完成，推送 {len(collected_sso)} 个 token 到旧版 API...")
            push_sso_to_api(collected_sso)
        stop_browser()


def _mp_worker_main(worker_id, count, output_path, extract_numbers, proxies, fallback, result_queue):
    global run_logger, _proxy_pool, _proxy_fallback_pool, _proxy_index, _browser_proxy, _proxy_dead, _proxy_stats
    os.environ["GROK_REGISTER_MP_WORKER"] = "1"
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    run_logger = setup_run_logger()
    _proxy_pool = list(proxies or [])
    _proxy_fallback_pool = list(fallback or [])
    _proxy_index = 0
    _browser_proxy = ""
    _proxy_dead = set()
    _proxy_stats.clear()
    print(
        f"[W{worker_id}] 启动，配额 {count} 轮，独占代理 {len(_proxy_pool)} 条，"
        f"共享兜底 {len(_proxy_fallback_pool)} 条"
    )
    success = fail = 0
    try:
        success, fail, _ = run_batch_rounds(
            count,
            output_path,
            extract_numbers=extract_numbers,
            worker_id=worker_id,
        )
    except KeyboardInterrupt:
        print(f"[W{worker_id}] 收到中断")
    except Exception as exc:
        print(f"[W{worker_id}] 进程异常: {exc}")
        if run_logger:
            run_logger.exception("worker 异常")
    finally:
        try:
            result_queue.put((worker_id, success, fail))
        except Exception:
            pass


def _run_multiprocess(args):
    n = args.workers
    counts = [args.count // n] * n
    for i in range(args.count % n):
        counts[i] += 1

    ctx = mp.get_context("spawn")
    result_queue = ctx.Queue()
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.environ["GROK_REGISTER_MP_WORKER"] = "1"
    procs = []
    try:
        for i, quota in enumerate(counts):
            if quota <= 0:
                continue
            assigned = _proxy_pool[i::n] if _proxy_pool else []
            if _proxy_pool and not assigned:
                assigned = list(_proxy_pool)
            proc = ctx.Process(
                target=_mp_worker_main,
                args=(i, quota, args.output, args.extract_numbers, assigned, list(_proxy_pool), result_queue),
                name=f"register-w{i}",
            )
            proc.start()
            procs.append(proc)
            print(f"[*] 已启动 worker {i}，配额 {quota}，独占代理 {len(assigned)} 条")
            if i + 1 < n:
                time.sleep(2)
    finally:
        os.environ.pop("GROK_REGISTER_MP_WORKER", None)

    try:
        for proc in procs:
            proc.join()
    except KeyboardInterrupt:
        print("\n[Info] 收到中断，正在结束各 worker...")
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.join(timeout=8)

    total_success = total_fail = 0
    finished = 0
    while finished < len(procs):
        try:
            worker_id, success, fail = result_queue.get(timeout=3)
        except Exception:
            break
        finished += 1
        total_success += success
        total_fail += fail
        print(f"[*] worker {worker_id} 结束：成功 {success}，失败 {fail}")
    print(f"\n[*] 全部进程结束：成功 {total_success}，失败 {total_fail}，目标 {args.count}")


def main():
    # 默认循环执行；每轮完成后关闭当前页，再自动进入下一轮。
    global run_logger, _proxy_pool
    run_logger = setup_run_logger()

    config_count = load_run_count()

    parser = argparse.ArgumentParser(description="Grok / x.ai 自动注册并采集 sso")
    parser.add_argument("--count", type=int, default=config_count, help=f"执行轮数，0 表示无限循环（默认读取 config.json run.count，当前 {config_count}）")
    parser.add_argument("--output", default=DEFAULT_SSO_FILE, help="sso 输出 txt 路径")
    parser.add_argument("--extract-numbers", action="store_true", help="注册完成后额外提取页面数字文本")
    parser.add_argument("--push-sso", default="", help="只把已有 sso txt（一行一个）导入 grok2api，不跑注册")
    parser.add_argument(
        "--no-convert-to-build",
        action="store_true",
        help="导入 grok2api 后不要转成 Grok Build（默认会转）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=load_run_workers(),
        help="并行浏览器进程数（默认读取 config.json run.workers，本机建议 2–3）",
    )
    args = parser.parse_args()
    if args.no_convert_to_build:
        os.environ["GROK_REGISTER_CONVERT_TO_BUILD"] = "0"

    if args.push_sso:
        path = os.path.abspath(args.push_sso)
        with open(path, "r", encoding="utf-8") as handle:
            tokens = [line.strip() for line in handle if line.strip() and not line.strip().startswith("#")]
        print(f"[*] 从文件导入 {len(tokens)} 个 SSO: {path}")
        push_sso_to_api(tokens)
        return

    if args.workers < 1:
        raise SystemExit("--workers 必须 >= 1")
    if args.workers > 1 and args.count <= 0:
        raise SystemExit("多进程模式必须指定 --count（不支持无限循环）")

    if _proxy_pool:
        _proxy_pool = _preflight_proxy_pool(_proxy_pool)
        if run_logger:
            run_logger.info("代理池预检后剩余 %s 条", len(_proxy_pool))

    if args.workers > 1:
        print(f"[*] 多进程注册：workers={args.workers} count={args.count}")
        _run_multiprocess(args)
        return

    run_batch_rounds(args.count, args.output, extract_numbers=args.extract_numbers)


if __name__ == "__main__":
    main()
