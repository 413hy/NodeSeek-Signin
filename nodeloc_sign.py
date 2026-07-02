# -*- coding: utf-8 -*-

import hashlib
import os
import re
import subprocess
import sys
import time
from typing import Callable

import requests
import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


DOMAIN = "www.nodeloc.com"
BASE_URL = f"https://{DOMAIN}"
USER_PAGE = f"{BASE_URL}/u/"
SESSION_CURRENT_URL = f"{BASE_URL}/session/current.json"

CHECKIN_BUTTON = "li.header-dropdown-toggle.checkin-icon button.checkin-button"
LOGIN_BUTTON = "button.login-button"
LOGIN_OK_SELECTOR = "div.directory-table__row.me"
USERNAME_SELECTOR = "div.directory-table__row.me a[data-user-card]"
ALREADY_SIGNED_TOOLTIP = "您今天已经签到过了"

BLOCKED_KEYWORDS = (
    "发帖后签到",
    "回帖后签到",
    "今天发帖",
    "今天回帖",
    "签到失败",
    "check-in failed",
    "try again later",
)


def get_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def cookie_fingerprint(cookie_raw: str) -> str:
    return hashlib.sha256(cookie_raw.encode("utf-8")).hexdigest()[:12]


def load_cookie_raw() -> tuple[str, str, str]:
    nodeloc_cookie = get_env("NODELOC_COOKIE")
    nl_cookie = get_env("NL_COOKIE")

    if nodeloc_cookie:
        return nodeloc_cookie, "NODELOC_COOKIE", ""

    if nl_cookie:
        return nl_cookie, "NL_COOKIE", ""

    return "", "", "未配置 NODELOC_COOKIE 或 NL_COOKIE，无法进行 NodeLoc 签到。"


def load_notify() -> Callable[[str, str], None] | None:
    try:
        from notify import send

        return send
    except Exception:
        return None


def github_token() -> str:
    return get_env("GH_PAT") or get_env("GITHUB_TOKEN")


def save_cookie_to_github_secret(secret_name: str, cookie: str) -> bool:
    token = github_token()
    repo = get_env("GITHUB_REPOSITORY")
    if not token or not repo:
        print("GH_PAT/GITHUB_TOKEN 或 GITHUB_REPOSITORY 未设置，跳过 GitHub Secret 更新")
        return False

    try:
        from nacl import encoding, public
    except Exception as e:
        print(f"PyNaCl 不可用，无法更新 GitHub Secret: {e}")
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    key_url = f"https://api.github.com/repos/{repo}/actions/secrets/public-key"
    key_response = requests.get(key_url, headers=headers, timeout=30)
    if key_response.status_code != 200:
        print(f"获取 GitHub Secret 公钥失败: {key_response.status_code}, {key_response.text}")
        return False

    key_data = key_response.json()
    public_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted_value = sealed_box.encrypt(cookie.encode("utf-8"), encoder=encoding.Base64Encoder()).decode("utf-8")

    secret_url = f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}"
    payload = {"encrypted_value": encrypted_value, "key_id": key_data["key_id"]}
    response = requests.put(secret_url, headers=headers, json=payload, timeout=30)
    if response.status_code in (201, 204):
        print(f"GitHub Secret: {secret_name} 更新成功")
        return True

    print(f"GitHub Secret 更新失败: {response.status_code}, {response.text}")
    return False


def save_cookie_to_github_var(var_name: str, cookie: str) -> bool:
    token = github_token()
    repo = get_env("GITHUB_REPOSITORY")
    if not token or not repo:
        print("GH_PAT/GITHUB_TOKEN 或 GITHUB_REPOSITORY 未设置，跳过 GitHub 变量更新")
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = {"name": var_name, "value": cookie}

    check_url = f"https://api.github.com/repos/{repo}/actions/variables/{var_name}"
    create_url = f"https://api.github.com/repos/{repo}/actions/variables"
    response = requests.patch(check_url, headers=headers, json=data, timeout=30)
    if response.status_code == 204:
        print(f"GitHub 变量: {var_name} 更新成功")
        return True

    if response.status_code == 404:
        response = requests.post(create_url, headers=headers, json=data, timeout=30)
        if response.status_code == 201:
            print(f"GitHub 变量: {var_name} 创建成功")
            return True

    print(f"GitHub 变量更新失败: {response.status_code}, {response.text}")
    return False


def save_refreshed_cookie(cookie: str) -> bool:
    if not cookie:
        print("刷新后的 Cookie 为空，跳过保存")
        return False

    secret_ok = save_cookie_to_github_secret("NODELOC_COOKIE", cookie)
    var_ok = save_cookie_to_github_var("NODELOC_COOKIE", cookie)
    return secret_ok or var_ok


def split_cookies(cookie_raw: str) -> list[str]:
    cookies = []

    for line in cookie_raw.splitlines():
        line = line.strip()
        if not line:
            continue

        line = line.split("#", 1)[0].strip()
        if not line:
            continue

        parts = [part.strip() for part in line.split("&") if part.strip()]
        cookies.extend(parts)

    return cookies


def parse_chrome_major(version_text: str) -> int | None:
    match = re.search(r"\b(\d+)\.\d+\.\d+\.\d+\b", version_text)
    if not match:
        return None

    return int(match.group(1))


def detect_chrome_major() -> int | None:
    env_version = get_env("NODELOC_CHROME_VERSION_MAIN")
    if env_version.isdigit():
        return int(env_version)

    for command in (
        ("google-chrome", "--version"),
        ("google-chrome-stable", "--version"),
        ("chromium-browser", "--version"),
        ("chromium", "--version"),
    ):
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            continue

        version_text = f"{result.stdout}\n{result.stderr}"
        major = parse_chrome_major(version_text)
        if major:
            print(f"检测到 Chrome 主版本: {major} ({version_text.strip()})")
            return major

    print("未检测到 Chrome 主版本，使用 undetected-chromedriver 默认驱动选择")
    return None


def create_browser():
    options = uc.ChromeOptions()
    for arg in (
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1920,1080",
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
        "--headless=new",
    ):
        options.add_argument(arg)

    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
    )

    chrome_major = detect_chrome_major()
    if chrome_major:
        driver = uc.Chrome(options=options, version_main=chrome_major)
    else:
        driver = uc.Chrome(options=options)

    driver.set_window_size(1920, 1080)
    driver.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>false})")
    driver.execute_script("window.chrome={runtime:{}}")
    driver.execute_script("Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh']})")
    driver.execute_script("Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]})")
    return driver


def parse_cookie_header(cookie_header: str) -> list[tuple[str, str]]:
    cookies = []
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue

        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            cookies.append((name, value.strip()))

    return cookies


def check_cookie_session(cookie_header: str) -> tuple[bool, str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": BASE_URL,
        "Cookie": cookie_header,
    }

    try:
        response = requests.get(SESSION_CURRENT_URL, headers=headers, timeout=30)
    except requests.RequestException as e:
        return False, "", f"requests session/current 请求异常: {e}"

    preview = response.text[:160].replace("\n", " ").strip()
    detail = f"requests session/current HTTP {response.status_code}"
    if preview:
        detail = f"{detail}, body={preview}"

    try:
        data = response.json()
    except ValueError:
        return False, "", detail

    current_user = data.get("current_user") if isinstance(data, dict) else None
    if current_user:
        username = current_user.get("username") or current_user.get("name") or "unknown"
        return True, str(username), f"requests session/current HTTP {response.status_code}"

    return False, "", detail


def inject_cookies(driver, cookie_header: str) -> int:
    driver.get(BASE_URL)
    injected = 0

    for name, value in parse_cookie_header(cookie_header):
        variants = (
            {"name": name, "value": value, "domain": DOMAIN, "path": "/", "secure": True},
            {"name": name, "value": value, "domain": ".nodeloc.com", "path": "/", "secure": True},
            {"name": name, "value": value, "path": "/", "secure": True},
        )

        for cookie in variants:
            try:
                driver.add_cookie(cookie)
                injected += 1
                break
            except Exception:
                continue

    return injected


def export_browser_cookie(driver, fallback_cookie: str) -> str:
    try:
        browser_cookies = driver.get_cookies()
    except Exception as e:
        print(f"读取浏览器 Cookie 失败，保留原 Cookie: {e}")
        return fallback_cookie

    cookie_map = {}
    for item in browser_cookies:
        domain = item.get("domain") or ""
        name = item.get("name")
        value = item.get("value")
        if name and value is not None and "nodeloc.com" in domain:
            cookie_map[name] = value

    if not cookie_map:
        return fallback_cookie

    original_order = [name for name, _value in parse_cookie_header(fallback_cookie)]
    ordered_names = []
    for name in original_order:
        if name in cookie_map and name not in ordered_names:
            ordered_names.append(name)

    for name in cookie_map:
        if name not in ordered_names:
            ordered_names.append(name)

    return "; ".join(f"{name}={cookie_map[name]}" for name in ordered_names)


def wait_login_success(driver, timeout: int = 20) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            EC.any_of(
                EC.presence_of_element_located((By.CSS_SELECTOR, LOGIN_OK_SELECTOR)),
                EC.presence_of_element_located((By.CSS_SELECTOR, CHECKIN_BUTTON)),
            )
        )
        return True
    except TimeoutException:
        return False


def page_requires_login(driver) -> bool:
    try:
        if driver.find_elements(By.CSS_SELECTOR, LOGIN_BUTTON):
            return True
    except Exception:
        pass

    try:
        text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return False

    return any(keyword in text for keyword in ("登录到您的账户", "电子邮件或用户名", "请输入密码"))


def get_current_user(driver) -> tuple[bool, str]:
    logged_in, username, _detail = get_current_user_detail(driver)
    return logged_in, username


def get_current_user_detail(driver) -> tuple[bool, str, str]:
    try:
        result = driver.execute_async_script(
            """
            const done = arguments[arguments.length - 1];
            fetch('/session/current.json', {
                credentials: 'include',
                headers: { 'accept': 'application/json' }
            }).then(async (response) => {
                const text = await response.text();
                let data = null;
                try { data = text ? JSON.parse(text) : null; } catch (e) {}
                done({ status: response.status, data });
            }).catch((error) => done({ status: 0, error: String(error) }));
            """
        )
        data = result.get("data") if isinstance(result, dict) else None
        current_user = data.get("current_user") if isinstance(data, dict) else None
        if current_user:
            username = current_user.get("username") or current_user.get("name") or "unknown"
            return True, str(username), f"session/current HTTP {result.get('status')}"

        status = result.get("status") if isinstance(result, dict) else "unknown"
        error = result.get("error") if isinstance(result, dict) else ""
        return False, "", f"session/current HTTP {status}{f', {error}' if error else ''}"
    except Exception as e:
        return False, "", f"session/current 检测异常: {e}"


def wait_current_user(driver, timeout: int = 45) -> tuple[bool, str, str]:
    deadline = time.time() + timeout
    last_detail = ""

    while time.time() < deadline:
        logged_in, username, detail = get_current_user_detail(driver)
        last_detail = detail
        if logged_in:
            return True, username, detail

        time.sleep(3)

    return False, "", last_detail or "session/current 未返回用户"


def describe_page(driver) -> str:
    parts = []

    try:
        parts.append(f"url={driver.current_url}")
    except Exception:
        pass

    try:
        title = driver.title.strip()
        if title:
            parts.append(f"title={title}")
    except Exception:
        pass

    try:
        text = driver.find_element(By.TAG_NAME, "body").text
        text = " ".join(text.split())
        if text:
            parts.append(f"body={text[:160]}")
    except Exception:
        pass

    return "；".join(parts) if parts else "页面状态不可读取"


def get_username(driver) -> str:
    logged_in, username = get_current_user(driver)
    if logged_in:
        return username

    try:
        element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, USERNAME_SELECTOR))
        )
        return element.get_attribute("data-user-card") or "未知用户"
    except Exception:
        return "未知用户"


def get_checkin_state(driver) -> dict | None:
    try:
        result = driver.execute_script(
            """
            const user = window.app && window.app.session && window.app.session.user;
            if (!user || typeof user.attribute !== 'function') {
                return null;
            }

            const attr = (name) => {
                try { return user.attribute(name); } catch (e) { return null; }
            };

            return {
                canCheckin: attr('canCheckin'),
                isPostToday: attr('isPostToday'),
                lastCheckinTime: attr('lastCheckinTime')
            };
            """
        )
    except Exception:
        return None

    return result if isinstance(result, dict) else None


def classify_checkin_state(state: dict | None, clicked: bool) -> tuple[str, str] | None:
    if not state:
        return None

    can_checkin = state.get("canCheckin")
    is_post_today = state.get("isPostToday")
    last_checkin_time = state.get("lastCheckinTime")

    if is_post_today is False:
        return "failed", "NodeLoc 签到失败，页面要求今天先发帖或回帖后再签到"

    if can_checkin is False and is_post_today is True:
        if clicked:
            return "success", "NodeLoc 签到成功，页面状态已变为今日不可重复签到"

        detail = f"，上次签到时间: {last_checkin_time}" if last_checkin_time else ""
        return "already", f"NodeLoc 今日已签到{detail}"

    return None


def hover_checkin(driver, button=None) -> str:
    texts = []

    try:
        target = button or WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, CHECKIN_BUTTON))
        )
        ActionChains(driver).move_to_element(target).perform()
        time.sleep(1)
    except Exception:
        return ""

    for selector in ("[role='tooltip']", ".tooltip", ".d-tooltip", ".ember-tooltip"):
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector)[:5]:
                text = element.text.strip()
                if text:
                    texts.append(text)
        except Exception:
            continue

    return "；".join(dict.fromkeys(texts))


def collect_page_status_text(driver) -> str:
    texts = []

    for selector in (
        "[role='alert']",
        ".alert",
        ".toast",
        ".modal",
        ".dialog",
        ".d-modal",
        ".d-modal__body",
    ):
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector)[:5]:
                text = element.text.strip()
                if text:
                    texts.append(text)
        except Exception:
            continue

    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        for keyword in BLOCKED_KEYWORDS:
            if keyword.lower() in body_text.lower():
                texts.append(keyword)
    except Exception:
        pass

    return "；".join(dict.fromkeys(" ".join(text.split()) for text in texts if text.strip()))


def has_blocked_text(text: str) -> bool:
    lower_text = text.lower()
    return any(keyword.lower() in lower_text for keyword in BLOCKED_KEYWORDS)


def already_checked_in(button) -> bool:
    class_value = button.get_attribute("class") or ""
    disabled = button.get_attribute("disabled")
    aria_disabled = button.get_attribute("aria-disabled")
    return "checked-in" in class_value or disabled is not None or aria_disabled == "true"


def find_checkin_button(driver):
    return WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, CHECKIN_BUTTON))
    )


def sign_one(cookie: str, index: int = 1) -> tuple[str, str, str]:
    driver = None

    try:
        def failed(message: str) -> tuple[str, str, str]:
            return "failed", message, ""

        def finished(status: str, message: str) -> tuple[str, str, str]:
            return status, message, export_browser_cookie(driver, cookie) if driver else cookie

        parsed_cookies = parse_cookie_header(cookie)
        if not parsed_cookies:
            return failed(f"账号{index}: Cookie 为空或格式不正确")

        cookie_valid, cookie_username, cookie_session_detail = check_cookie_session(cookie)
        print(f"账号{index}: Cookie 预检结果: {cookie_session_detail}", flush=True)

        driver = create_browser()
        injected_count = inject_cookies(driver, cookie)
        if injected_count <= 0:
            return failed(f"账号{index}: Cookie 注入失败")

        last_login_detail = ""
        logged_in = False
        detected_username = ""

        for attempt in range(1, 4):
            driver.get(USER_PAGE)
            logged_in, detected_username, last_login_detail = wait_current_user(driver)
            if logged_in:
                break

            if page_requires_login(driver):
                if cookie_valid:
                    return failed(
                        f"账号{index}({cookie_username}): Cookie 预检有效，但浏览器页面显示登录按钮，"
                        f"{last_login_detail}，{describe_page(driver)}"
                    )

                return failed(
                    f"账号{index}: Cookie 已失效或未登录，页面显示登录按钮，"
                    f"{last_login_detail}，{describe_page(driver)}"
                )

            print(
                f"账号{index}: 第 {attempt}/3 次登录态检测未确认，"
                f"{last_login_detail}，{describe_page(driver)}",
                flush=True,
            )
            time.sleep(5)

        if not logged_in:
            if cookie_valid:
                return failed(
                    f"账号{index}({cookie_username}): Cookie 预检有效，但浏览器未能确认登录态，"
                    f"{last_login_detail}，{describe_page(driver)}"
                )

            return failed(
                f"账号{index}: 未能确认登录态，不直接判定 Cookie 失效，"
                f"{last_login_detail}，{describe_page(driver)}"
            )

        if page_requires_login(driver):
            if cookie_valid:
                return failed(
                    f"账号{index}({cookie_username}): Cookie 预检有效，但页面显示登录按钮，"
                    f"{describe_page(driver)}"
                )

            return failed(
                f"账号{index}: Cookie 已失效或未登录，页面显示登录按钮，"
                f"{describe_page(driver)}"
            )

        username = detected_username or cookie_username or get_username(driver)
        driver.get(BASE_URL)
        time.sleep(3)

        state_result = classify_checkin_state(get_checkin_state(driver), clicked=False)
        if state_result:
            status, text = state_result
            return finished(status, f"账号{index}({username}): {text}")

        try:
            button = find_checkin_button(driver)
        except TimeoutException:
            if page_requires_login(driver):
                return failed(f"账号{index}({username}): Cookie 已失效或未登录，页面要求登录")
            return failed(f"账号{index}({username}): 未找到签到按钮")

        hint_text = hover_checkin(driver, button)
        if ALREADY_SIGNED_TOOLTIP in hint_text or "已经签到过了" in hint_text:
            return finished("already", f"账号{index}({username}): NodeLoc 今日已签到，{ALREADY_SIGNED_TOOLTIP}")

        if has_blocked_text(hint_text):
            return failed(f"账号{index}({username}): NodeLoc 签到失败，页面提示: {hint_text}")

        if already_checked_in(button):
            return finished("already", f"账号{index}({username}): NodeLoc 今日已签到")

        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
        time.sleep(1)
        driver.execute_script("arguments[0].click();", button)
        time.sleep(5)

        status_text = collect_page_status_text(driver)
        if has_blocked_text(status_text):
            return failed(f"账号{index}({username}): NodeLoc 签到失败，页面提示: {status_text}")

        state_result = classify_checkin_state(get_checkin_state(driver), clicked=True)
        if state_result:
            status, text = state_result
            return finished(status, f"账号{index}({username}): {text}")

        try:
            button = find_checkin_button(driver)
            after_hint = hover_checkin(driver, button)
            if ALREADY_SIGNED_TOOLTIP in after_hint or "已经签到过了" in after_hint:
                return finished("success", f"账号{index}({username}): NodeLoc 签到成功，已显示今日已签到")
            if already_checked_in(button):
                return finished("success", f"账号{index}({username}): NodeLoc 签到成功")
        except Exception:
            pass

        driver.refresh()
        time.sleep(5)

        if page_requires_login(driver):
            return failed(f"账号{index}({username}): Cookie 已失效或未登录，刷新后页面要求登录")

        state_result = classify_checkin_state(get_checkin_state(driver), clicked=True)
        if state_result:
            status, text = state_result
            return finished(status, f"账号{index}({username}): {text}")

        detail = status_text or "页面没有返回成功、已签到或失败提示"
        return failed(f"账号{index}({username}): 已点击签到按钮，但未确认签到成功，页面提示: {detail}")
    except TimeoutException:
        return "failed", f"账号{index}: NodeLoc 页面加载或按钮等待超时，可能 Cookie 失效或页面未登录", ""
    except Exception as e:
        return "failed", f"账号{index}: NodeLoc 签到异常: {e}", ""
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def main():
    send = load_notify()

    cookie_raw, cookie_source, cookie_error = load_cookie_raw()
    if not cookie_raw:
        msg = cookie_error
        print(msg)
        if send:
            try:
                send("NodeLoc 签到失败", msg)
            except Exception as e:
                print(f"发送通知失败: {e}")
        sys.exit(1)

    fingerprint = cookie_fingerprint(cookie_raw)
    print(f"使用 Cookie 来源: {cookie_source}，指纹: {fingerprint}")

    cookies = split_cookies(cookie_raw)
    if not cookies:
        msg = "NodeLoc Cookie 为空或格式不正确，无法签到。"
        print(msg)
        if send:
            try:
                send("NodeLoc 签到失败", msg)
            except Exception as e:
                print(f"发送通知失败: {e}")
        sys.exit(1)

    results = []
    success_count = 0
    already_count = 0
    failed_count = 0
    refreshed_cookies = []
    cookies_updated = False

    for index, cookie in enumerate(cookies, start=1):
        status, message, refreshed_cookie = sign_one(cookie, index)
        print(message)
        results.append(message)

        if status == "success":
            success_count += 1
            refreshed_cookies.append(refreshed_cookie or cookie)
            cookies_updated = True
        elif status == "already":
            already_count += 1
            refreshed_cookies.append(refreshed_cookie or cookie)
            cookies_updated = True
        else:
            failed_count += 1
            refreshed_cookies.append(cookie)

        if index < len(cookies):
            time.sleep(5)

    title = "NodeLoc 签到"
    summary = (
        f"成功：{success_count}，已签到：{already_count}，失败：{failed_count}，"
        f"总计：{len(cookies)}\n\n"
        + "\n".join(results)
    )

    if send:
        try:
            send(title, summary)
        except Exception as e:
            print(f"发送通知失败: {e}")

    if cookies_updated:
        refreshed_cookie_raw = "&".join(cookie for cookie in refreshed_cookies if cookie.strip())
        if refreshed_cookie_raw and cookie_fingerprint(refreshed_cookie_raw) != fingerprint:
            print(f"检测到 Cookie 已刷新，新指纹: {cookie_fingerprint(refreshed_cookie_raw)}")
        if save_refreshed_cookie(refreshed_cookie_raw):
            print("NodeLoc Cookie 已写回 GitHub，后续运行将使用刷新后的 Cookie")
        else:
            print("NodeLoc Cookie 写回 GitHub 失败，请检查 GH_PAT 权限")

    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
