# -*- coding: utf-8 -*-

import os
import sys
from typing import Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_URL = "https://www.nodeloc.com/"
CHECKIN_XPATH = '//*[@id="ember3"]/div[3]/header/div/div/div[3]/ul/li[2]/button'
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
LOGIN_KEYWORDS = (
    "登录到您的账户",
    "电子邮件或用户名",
    "请输入密码",
    "创建账户",
)


def get_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


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


def has_login_prompt(page) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return False

    return any(keyword in body_text for keyword in LOGIN_KEYWORDS)


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


def find_checkin_button(page):
    candidates = [
        page.locator(CHECKIN_SELECTOR),
        page.locator(f"xpath={CHECKIN_XPATH}"),
    ]

    for candidate in candidates:
        if candidate.count() <= 0:
            continue

        button = candidate.first
        try:
            button.wait_for(state="visible", timeout=10000)
        except Exception:
            continue

        hint_text = get_button_hint_text(page, button)
        if is_checkin_button(button, hint_text):
            return button, hint_text

    return None, ""


def sign_one(cookie: str, index: int = 1) -> tuple[bool, str]:
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
                return False, f"账号{index}: Cookie 为空或格式不正确"

            context.add_cookies(parsed_cookies)
            page = context.new_page()
            page.goto(BASE_URL, wait_until="networkidle", timeout=60000)

            checkin_button, hint_text = find_checkin_button(page)
            if not checkin_button:
                if has_login_prompt(page):
                    return False, f"账号{index}: Cookie 已失效或未登录，页面要求登录"
                if has_signed_text(page):
                    return True, f"账号{index}: NodeLoc 今日已签到"
                return False, f"账号{index}: 未找到签到按钮，可能 Cookie 失效或页面未登录"

            if ALREADY_SIGNED_TOOLTIP in hint_text or "已经签到过了" in hint_text:
                return True, f"账号{index}: NodeLoc 今日已签到，{ALREADY_SIGNED_TOOLTIP}"

            if has_login_prompt(page):
                return False, f"账号{index}: Cookie 已失效或未登录，页面要求登录"

            checkin_button.click(timeout=30000)
            page.wait_for_timeout(5000)

            after_click_hint = get_button_hint_text(page, checkin_button)
            if ALREADY_SIGNED_TOOLTIP in after_click_hint or "已经签到过了" in after_click_hint:
                return True, f"账号{index}: NodeLoc 签到成功"

            if has_signed_text(page):
                return True, f"账号{index}: NodeLoc 签到成功"

            if has_login_prompt(page):
                return False, f"账号{index}: Cookie 已失效或未登录，点击后页面要求登录"

            return False, f"账号{index}: 已点击签到按钮，但未确认签到成功"
    except PlaywrightTimeoutError:
        return False, f"账号{index}: NodeLoc 签到按钮等待超时，可能 Cookie 失效或页面未登录"
    except Exception as e:
        return False, f"账号{index}: NodeLoc 签到异常: {e}"
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass


def main():
    send = load_notify()

    cookie_raw = get_env("NODELOC_COOKIE") or get_env("NL_COOKIE")
    if not cookie_raw:
        msg = "未配置 NODELOC_COOKIE 或 NL_COOKIE，无法进行 NodeLoc 签到。"
        print(msg)
        if send:
            try:
                send("NodeLoc 签到失败", msg)
            except Exception as e:
                print(f"发送通知失败: {e}")
        sys.exit(1)

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
    ok_count = 0

    for index, cookie in enumerate(cookies, start=1):
        ok, message = sign_one(cookie, index)
        print(message)
        results.append(message)
        if ok:
            ok_count += 1

    title = "NodeLoc 签到"
    summary = f"完成：{ok_count}/{len(cookies)} 个账号成功或已签到\n\n" + "\n".join(results)

    if send:
        try:
            send(title, summary)
        except Exception as e:
            print(f"发送通知失败: {e}")

    if ok_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
