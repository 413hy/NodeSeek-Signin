# -*- coding: utf-8 -*-
import os
import re
import sys
import html
import xml.etree.ElementTree as ET

import requests


BASE_URL = "https://forum.naixi.net"
SIGN_PAGE = f"{BASE_URL}/k_misign-sign.html"
SIGN_API = (
    f"{BASE_URL}/plugin.php"
    "?id=k_misign:sign"
    "&operation=qiandao"
    "&formhash={formhash}"
    "&format=empty"
)


def get_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def load_notify():
    try:
        from notify import send
        return send
    except Exception:
        return None


def extract_cdata_or_text(raw_text: str) -> str:
    if not raw_text:
        return ""

    text = raw_text.strip()

    if text.startswith("<?xml") or text.startswith("<root"):
        try:
            root = ET.fromstring(text)
            if root.text:
                return html.unescape(root.text.strip())
        except Exception:
            pass

    cdata_match = re.search(r"<!\[CDATA\[(.*?)\]\]>", text, re.S)
    if cdata_match:
        return html.unescape(cdata_match.group(1).strip())

    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text.strip())


def get_formhash(session: requests.Session) -> str:
    env_formhash = get_env("NAIXI_FORMHASH")
    if env_formhash:
        return env_formhash

    resp = session.get(SIGN_PAGE, timeout=30)
    resp.raise_for_status()

    page_text = resp.text

    patterns = [
        r"formhash=([a-fA-F0-9]{8})",
        r"name=['\"]formhash['\"]\s+value=['\"]([a-fA-F0-9]{8})['\"]",
        r"value=['\"]([a-fA-F0-9]{8})['\"]\s+name=['\"]formhash['\"]",
        r"formhash['\"]?\s*[:=]\s*['\"]([a-fA-F0-9]{8})",
    ]

    for pattern in patterns:
        match = re.search(pattern, page_text)
        if match:
            return match.group(1)

    raise RuntimeError("未能从签到页提取 formhash，请手动配置 NAIXI_FORMHASH")


def classify_sign_result(status_code: int, raw_text: str, index: int) -> tuple[bool, str]:
    cleaned_text = extract_cdata_or_text(raw_text)

    preview = cleaned_text or raw_text.strip()
    preview = preview[:300] if preview else "空响应"

    if status_code != 200:
        return False, f"账号{index}: 请求失败，HTTP {status_code}，返回: {preview}"

    already_signed_keywords = [
        "今日已签",
        "今天已签",
        "已经签到",
        "已签到",
        "今日已签到",
        "您今天已经签到过了",
    ]

    success_keywords = [
        "签到成功",
        "恭喜",
        "打卡成功",
        "签到完毕",
        "qiandao success",
        "success",
        "succeed",
    ]

    login_fail_keywords = [
        "请先登录",
        "未登录",
        "需要登录",
        "member.php?mod=logging",
    ]

    formhash_fail_keywords = [
        "请求来路不正确",
        "提交请求来路不正确",
        "formhash",
    ]

    fail_keywords = [
        "签到失败",
        "失败",
        "错误",
        "error",
        "非法",
    ]

    lower_preview = preview.lower()

    if any(keyword in preview for keyword in already_signed_keywords):
        return True, f"账号{index}: 今日已签到"

    if any(keyword in preview for keyword in success_keywords) or any(
        keyword in lower_preview for keyword in ["success", "succeed"]
    ):
        return True, f"账号{index}: 签到成功，返回: {preview}"

    if any(keyword in preview for keyword in login_fail_keywords) or "login" in lower_preview:
        return False, f"账号{index}: 签到失败，Cookie 可能失效或未登录，返回: {preview}"

    if any(keyword in preview for keyword in formhash_fail_keywords):
        return False, f"账号{index}: 签到失败，formhash 可能失效，返回: {preview}"

    if any(keyword in preview for keyword in fail_keywords) or "error" in lower_preview:
        return False, f"账号{index}: 签到失败，返回: {preview}"

    if not cleaned_text and not raw_text.strip():
        return True, f"账号{index}: 签到请求完成，服务器返回空内容"

    return True, f"账号{index}: 签到请求完成，返回: {preview}"


def sign_one(cookie: str, index: int = 1) -> tuple[bool, str]:
    session = requests.Session()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": SIGN_PAGE,
        "Cookie": cookie,
    }
    session.headers.update(headers)

    formhash = get_formhash(session)
    sign_url = SIGN_API.format(formhash=formhash)

    resp = session.get(sign_url, timeout=30)

    return classify_sign_result(resp.status_code, resp.text, index)


def split_cookies(cookie_raw: str) -> list[str]:
    cookies = []

    for line in cookie_raw.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = [part.strip() for part in line.split("&") if part.strip()]
        cookies.extend(parts)

    return cookies


def main():
    send = load_notify()

    cookie_raw = get_env("NAIXI_COOKIE")
    if not cookie_raw:
        msg = "未配置 NAIXI_COOKIE，无法签到。请在 GitHub Secrets 添加 NAIXI_COOKIE。"
        print(msg)

        if send:
            try:
                send("奶昔论坛签到失败", msg)
            except Exception as e:
                print(f"发送通知失败: {e}")

        sys.exit(1)

    cookies = split_cookies(cookie_raw)

    if not cookies:
        msg = "NAIXI_COOKIE 为空或格式不正确，无法签到。"
        print(msg)

        if send:
            try:
                send("奶昔论坛签到失败", msg)
            except Exception as e:
                print(f"发送通知失败: {e}")

        sys.exit(1)

    results = []
    ok_count = 0

    for index, cookie in enumerate(cookies, start=1):
        try:
            ok, message = sign_one(cookie, index)
        except Exception as e:
            ok = False
            message = f"账号{index}: 签到异常: {e}"

        print(message)
        results.append(message)

        if ok:
            ok_count += 1

    title = "奶昔论坛签到"
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
