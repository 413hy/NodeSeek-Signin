# -*- coding: utf-8 -*-

import os
import sys
import hashlib
from typing import Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_URL = "https://www.nodeloc.com/"
LOGIN_BUTTON_XPATH = '//*[@id="ember3"]/div[3]/header/div/div/div[3]/span/span/button'
CHECKIN_XPATH = '//*[@id="ember3"]/div[3]/header/div/div/div[3]/ul/li[2]/button'
CHECKIN_BUTTON_XPATH = '//*[@id="ember3"]/div[3]/header/div/div/div[4]/ul/li[2]/button'
CHECKIN_ICON_XPATH = '//*[@id="ember3"]/div[3]/header/div/div/div[4]/ul/li[2]/button/svg'
CHECKIN_SELECTOR = ".checkin-button"
ALREADY_SIGNED_TOOLTIP = "您今天已经签到过了"
SIGNED_KEYWORDS = (
    "已签到",
    "今日已签到",
    "今天已签到",
    "签到成功",
    "连续签到",
    ALREADY_SIGNED_TOOLTIP,
)
CHECKIN_BLOCKED_KEYWORDS = (
    "发帖后签到",
    "回帖后签到",
    "今天发帖",
    "今天回帖",
    "签到失败",
    "try again later",
    "check-in failed",
)
LOGIN_KEYWORDS = (
    "登录到您的账户",
    "电子邮件或用户名",
    "请输入密码",
    "创建账户",
)
LOGGED_IN_KEYWORDS = (
    "欢迎回来",
    "Welcome back",
)


def get_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def cookie_fingerprint(cookie_raw: str) -> str:
    return hashlib.sha256(cookie_raw.encode("utf-8")).hexdigest()[:12]


def load_cookie_raw() -> tuple[str, str, str]:
    nodeloc_cookie = get_env("NODELOC_COOKIE")
    nl_cookie = get_env("NL_COOKIE")

    if nodeloc_cookie and nl_cookie and nodeloc_cookie != nl_cookie:
        return (
            "",
            "conflict",
            "同时配置了 NODELOC_COOKIE 和 NL_COOKIE，且内容不同。请删除旧的 secret，只保留一个。",
        )

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


def split_cookies(cookie_raw: str) -> list[str]:
    cookies = []

    for line in cookie_raw.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = [part.strip() for part in line.split("&") if part.strip()]
        cookies.extend(parts)

    return cookies


def parse_cookie_header(cookie_header: str) -> list[dict[str, str]]:
    cookies = []

    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue

        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue

        cookies.append({
            "name": name,
            "value": value.strip(),
            "url": BASE_URL,
        })

    return cookies


def has_signed_text(page) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return False

    return any(keyword in body_text for keyword in SIGNED_KEYWORDS)


def collect_page_status_text(page) -> str:
    texts = []

    for selector in (
        "[role='alert']",
        ".Alert",
        ".alert",
        ".toast",
        ".Toast",
        ".modal",
        ".Modal",
        ".dialog",
        ".Dialog",
        ".d-modal",
        ".d-modal__body",
        ".checkInSuccessModal",
        ".checkInFailedModal",
    ):
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 5)
            for index in range(count):
                text = locator.nth(index).inner_text(timeout=1000).strip()
                if text:
                    texts.append(text)
        except Exception:
            continue

    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        for keyword in CHECKIN_BLOCKED_KEYWORDS + LOGIN_KEYWORDS:
            if keyword in body_text:
                texts.append(keyword)
    except Exception:
        pass

    unique_texts = []
    for text in texts:
        normalized = " ".join(text.split())
        if normalized and normalized not in unique_texts:
            unique_texts.append(normalized)

    return "；".join(unique_texts[:5])


def has_checkin_blocked_text(text: str) -> bool:
    lower_text = text.lower()
    return any(keyword.lower() in lower_text for keyword in CHECKIN_BLOCKED_KEYWORDS)


def has_login_prompt(page) -> bool:
    try:
        login_button = page.locator(f"xpath={LOGIN_BUTTON_XPATH}")
        if login_button.count() > 0 and login_button.first.is_visible(timeout=2000):
            return True
    except Exception:
        pass

    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return False

    return any(keyword in body_text for keyword in LOGIN_KEYWORDS)


def get_current_user(page) -> tuple[bool, str]:
    try:
        result = page.evaluate(
            """async () => {
                const response = await fetch('/session/current.json', {
                    credentials: 'include',
                    headers: { 'accept': 'application/json' }
                });
                const text = await response.text();
                let data = null;
                try {
                    data = text ? JSON.parse(text) : null;
                } catch (e) {
                    data = null;
                }
                return { status: response.status, data };
            }"""
        )
        data = result.get("data") if isinstance(result, dict) else None
        current_user = data.get("current_user") if isinstance(data, dict) else None
        if current_user:
            username = current_user.get("username") or current_user.get("name") or "unknown"
            return True, str(username)
    except Exception:
        pass

    return False, ""


def get_checkin_state(page) -> dict | None:
    try:
        result = page.evaluate(
            """() => {
                const user = window.app && window.app.session && window.app.session.user;
                if (!user || typeof user.attribute !== 'function') {
                    return null;
                }

                const attr = (name) => {
                    try {
                        return user.attribute(name);
                    } catch (e) {
                        return null;
                    }
                };

                return {
                    canCheckin: attr('canCheckin'),
                    isPostToday: attr('isPostToday'),
                    lastCheckinTime: attr('lastCheckinTime'),
                    totalContinuousCheckIn: attr('totalContinuousCheckIn')
                };
            }"""
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


def is_logged_in(page) -> bool:
    ok, _ = get_current_user(page)
    if ok:
        return True

    try:
        body_text = page.locator("body").inner_text(timeout=5000)
        if any(keyword in body_text for keyword in LOGGED_IN_KEYWORDS):
            return True
    except Exception:
        pass

    for selector in (
        ".current-user",
        ".header-dropdown-toggle.current-user",
        ".user-menu",
        ".user-menu-trigger",
    ):
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue

    return False


def get_button_hint_text(page, button) -> str:
    texts = []

    for attr in (
        "title",
        "aria-label",
        "data-tooltip",
        "data-original-title",
        "data-title",
    ):
        try:
            value = button.get_attribute(attr, timeout=2000)
        except Exception:
            value = None
        if value:
            texts.append(value)

    try:
        texts.append(button.inner_text(timeout=2000))
    except Exception:
        pass

    try:
        button.hover(timeout=10000)
        page.wait_for_timeout(800)
    except Exception:
        pass

    for selector in (
        "[role='tooltip']",
        ".tooltip",
        ".d-tooltip",
        ".d-tooltip-content",
        ".ember-tooltip",
    ):
        try:
            tooltip = page.locator(selector)
            count = min(tooltip.count(), 5)
            for index in range(count):
                text = tooltip.nth(index).inner_text(timeout=1000).strip()
                if text:
                    texts.append(text)
        except Exception:
            continue

    try:
        if page.get_by_text(ALREADY_SIGNED_TOOLTIP).count() > 0:
            texts.append(ALREADY_SIGNED_TOOLTIP)
    except Exception:
        pass

    return "\n".join(text.strip() for text in texts if text and text.strip())


def is_checkin_button(button, hint_text: str) -> bool:
    fields = [hint_text]

    for attr in ("class", "title", "aria-label", "data-tooltip", "data-original-title"):
        try:
            value = button.get_attribute(attr, timeout=2000)
        except Exception:
            value = None
        if value:
            fields.append(value)

    combined = "\n".join(fields).lower()
    return (
        "checkin" in combined
        or "check-in" in combined
        or "签到" in combined
        or ALREADY_SIGNED_TOOLTIP in combined
    )


def find_checkin_target(page):
    candidates = [
        page.locator(f"xpath={CHECKIN_ICON_XPATH}"),
        page.locator(f"xpath={CHECKIN_BUTTON_XPATH}"),
        page.locator(f"xpath={CHECKIN_XPATH}"),
        page.locator(CHECKIN_SELECTOR),
    ]

    for candidate in candidates:
        if candidate.count() <= 0:
            continue

        target = candidate.first
        try:
            target.wait_for(state="visible", timeout=10000)
        except Exception:
            continue

        hint_text = get_button_hint_text(page, target)
        if not is_checkin_button(target, hint_text):
            continue

        try:
            click_target = target.locator("xpath=ancestor-or-self::button[1]")
            if click_target.count() > 0:
                return click_target.first, hint_text
        except Exception:
            pass

        return target, hint_text

    return None, ""


def sign_one(cookie: str, index: int = 1) -> tuple[str, str]:
    browser = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )

            parsed_cookies = parse_cookie_header(cookie)
            if not parsed_cookies:
                return "failed", f"账号{index}: Cookie 为空或格式不正确"

            context.add_cookies(parsed_cookies)
            page = context.new_page()
            page.goto(BASE_URL, wait_until="commit", timeout=60000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(10000)

            if has_login_prompt(page):
                return "failed", f"账号{index}: Cookie 已失效或未登录，页面显示登录按钮"

            logged_in, username = get_current_user(page)
            if not logged_in:
                return "failed", f"账号{index}: Cookie 已失效或未登录，未检测到登录态"

            state_result = classify_checkin_state(get_checkin_state(page), clicked=False)
            if state_result:
                status, text = state_result
                return status, f"账号{index}: {text}"

            checkin_button, hint_text = find_checkin_target(page)
            if not checkin_button:
                if has_login_prompt(page):
                    return "failed", f"账号{index}: Cookie 已失效或未登录，页面要求登录"
                if has_signed_text(page):
                    return "already", f"账号{index}: NodeLoc 今日已签到"
                return "failed", f"账号{index}: 未找到签到按钮，可能 Cookie 失效或页面未登录"

            if ALREADY_SIGNED_TOOLTIP in hint_text or "已经签到过了" in hint_text:
                return "already", f"账号{index}: NodeLoc 今日已签到，{ALREADY_SIGNED_TOOLTIP}"

            if has_checkin_blocked_text(hint_text):
                return "failed", f"账号{index}: NodeLoc 签到失败，页面提示: {hint_text}"

            if has_login_prompt(page):
                return "failed", f"账号{index}: Cookie 已失效或未登录，页面要求登录"

            checkin_button.click(timeout=30000)
            page.wait_for_timeout(5000)

            status_text = collect_page_status_text(page)
            if has_checkin_blocked_text(status_text):
                return "failed", f"账号{index}: NodeLoc 签到失败，页面提示: {status_text}"

            state_result = classify_checkin_state(get_checkin_state(page), clicked=True)
            if state_result:
                status, text = state_result
                return status, f"账号{index}: {text}"

            after_click_hint = get_button_hint_text(page, checkin_button)
            if ALREADY_SIGNED_TOOLTIP in after_click_hint or "已经签到过了" in after_click_hint:
                return "already", f"账号{index}: NodeLoc 今日已签到，{ALREADY_SIGNED_TOOLTIP}"

            if has_signed_text(page):
                return "success", f"账号{index}: NodeLoc 签到成功"

            if has_login_prompt(page):
                return "failed", f"账号{index}: Cookie 已失效或未登录，点击后页面要求登录"

            page.reload(wait_until="commit", timeout=60000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(5000)

            if has_login_prompt(page):
                return "failed", f"账号{index}: Cookie 已失效或未登录，刷新后页面要求登录"

            state_result = classify_checkin_state(get_checkin_state(page), clicked=True)
            if state_result:
                status, text = state_result
                return status, f"账号{index}: {text}"

            checkin_button, reload_hint = find_checkin_target(page)
            reload_status = collect_page_status_text(page)
            combined_status = "；".join(
                text for text in (status_text, after_click_hint, reload_hint, reload_status) if text
            )

            if has_checkin_blocked_text(combined_status):
                return "failed", f"账号{index}: NodeLoc 签到失败，页面提示: {combined_status}"

            if (
                ALREADY_SIGNED_TOOLTIP in combined_status
                or "已经签到过了" in combined_status
                or has_signed_text(page)
            ):
                return "success", f"账号{index}: NodeLoc 签到成功，刷新后已显示今日已签到"

            detail = combined_status or "页面没有返回成功、已签到或失败提示"
            return "failed", f"账号{index}: 已点击签到按钮，但未确认签到成功，页面提示: {detail}"
    except PlaywrightTimeoutError:
        return "failed", f"账号{index}: NodeLoc 页面加载或按钮等待超时，可能 Cookie 失效或页面未登录"
    except Exception as e:
        return "failed", f"账号{index}: NodeLoc 签到异常: {e}"
    finally:
        if browser:
            try:
                browser.close()
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

    for index, cookie in enumerate(cookies, start=1):
        status, message = sign_one(cookie, index)
        print(message)
        results.append(message)
        if status == "success":
            success_count += 1
        elif status == "already":
            already_count += 1
        else:
            failed_count += 1

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

    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
