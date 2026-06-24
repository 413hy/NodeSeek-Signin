# -*- coding: utf-8 -*-
import os
import re
import sys
from urllib.parse import urljoin

import requests


BASE_URL = "https://forum.naixi.net"
SIGN_PAGE = f"{BASE_URL}/k_misign-sign.html"
SIGN_API = f"{BASE_URL}/plugin.php?id=k_misign:sign&operation=qiandao&formhash={{formhash}}&format=empty"


def get_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def load_notify():
    try:
        from notify import send
        return send
    except Exception:
        return None


def get_formhash(session: requests.Session) -> str:
    """
    优先从环境变量 NAIXI_FORMHASH 读取；
    若未配置，则访问签到页自动提取 formhash。
    """
    env_formhash = get_env("NAIXI_FORMHASH")
    if env_formhash:
        return env_formhash

    resp = session.get(SIGN_PAGE, timeout=30)
    resp.raise_for_status()

    # Discuz 常见 formhash 形态
    patterns = [
        r"formhash=([a-fA-F0-9]{8})",
        r'name="formhash"\s+value="([a-fA-F0-9]{8})"',
        r"formhash['\"]?\s*[:=]\s*['\"]([a-fA-F0-9]{8})",
    ]

    for pattern in patterns:
        match = re.search(pattern, resp.text)
        if match:
            return match.group(1)

    raise RuntimeError("未能从签到页提取 formhash，请手动配置 NAIXI_FORMHASH")


def sign_one(cookie: str, index: int = 1) -> tuple[bool, str]:
    session = requests.Session()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": SIGN_PAGE,
        "Cookie": cookie,
    }
    session.headers.update(headers)

    formhash = get_formhash(session)
    url = SIGN_API.format(formhash=formhash)

    resp = session.get(url, timeout=30)
    text = resp.text.strip()

    # k_misign format=empty 常见返回是 HTML/JS/短文本，不一定是 JSON
    success_keywords = ["签到成功", "恭喜", "今日签到", "已签到", "已经签到", "succeed", "success"]
    fail_keywords = ["请先登录", "未登录", "login", "formhash", "失败", "错误"]

    if resp.status_code != 200:
        return False, f"账号{index}: HTTP {resp.status_code}, 返回: {text[:300]}"

    if any(k in text for k in success_keywords):
        return True, f"账号{index}: 签到请求完成，返回: {text[:300] or '空响应'}"

    if any(k.lower() in text.lower() for k in fail_keywords):
        return False, f"账号{index}: 可能签到失败，返回: {text[:300] or '空响应'}"

    # 有些 Discuz 插件 format=empty 成功时可能返回空
    if text == "":
        return True, f"账号{index}: 签到请求完成，服务器返回空内容"

    return True, f"账号{index}: 签到请求完成，返回: {text[:300]}"


def main():
    send = load_notify()

    cookie_raw = get_env("NAIXI_COOKIE")
    if not cookie_raw:
        msg = "未配置 NAIXI_COOKIE，无法签到。请在 GitHub Secrets 添加 NAIXI_COOKIE。"
        print(msg)
        if send:
            send("奶昔论坛签到失败", msg)
        sys.exit(1)

    # 支持多账号：用换行或 & 分隔
    cookies = []
    for part in cookie_raw.replace("\n", "&").split("&"):
        part = part.strip()
        if part:
            cookies.append(part)

    results = []
    ok_count = 0

    for i, cookie in enumerate(cookies, start=1):
        try:
            ok, msg = sign_one(cookie, i)
        except Exception as e:
            ok, msg = False, f"账号{i}: 异常: {e}"

        print(msg)
        results.append(msg)
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
