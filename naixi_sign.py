# -*- coding: utf-8 -*-
import os
import re
import sys
import html
import time
import random
import hashlib
import xml.etree.ElementTree as ET

import requests


BASE_URL = "https://forum.naixi.net"
SIGN_PAGE = f"{BASE_URL}/k_misign-sign.html"
CREDIT_PAGE = f"{BASE_URL}/home.php?mod=spacecp&ac=credit&showcredit=1"
SIGN_API = (
    f"{BASE_URL}/plugin.php"
    "?id=k_misign:sign"
    "&operation=qiandao"
    "&formhash={formhash}"
    "&format=empty"
    "&inajax=1"
    "&ajaxtarget="
)
SIGN_URL_PATTERN = re.compile(
    r"^https://forum\.naixi\.net/plugin\.php\?id=k_misign:sign&operation=qiandao&formhash=([a-fA-F0-9]{8})&format=empty(?:&inajax=1&ajaxtarget=)?$"
)


DIGIT_CLASS_MAP = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}

RETRY_STATUS_CODES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


def get_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def cookie_fingerprint(cookie_raw: str) -> str:
    return hashlib.sha256(cookie_raw.encode("utf-8")).hexdigest()[:12]


def load_notify():
    try:
        from notify import send
        return send
    except Exception:
        return None


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    retries: int = 6,
    timeout: int = 30,
    **kwargs,
) -> requests.Response:
    last_response = None
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            last_response = response
            if response.status_code not in RETRY_STATUS_CODES:
                return response

            if attempt < retries:
                wait_seconds = min(12 * attempt, 60) + random.randint(0, 5)
                print(
                    f"请求 {url} 返回 HTTP {response.status_code}，"
                    f"{wait_seconds} 秒后重试 {attempt}/{retries}",
                    flush=True,
                )
                time.sleep(wait_seconds)
                continue

            raise RuntimeError(
                f"请求 {url} 连续 {retries} 次返回 HTTP {response.status_code}，"
                "目标站或 CDN 当前不稳定"
            )
        except requests.RequestException as e:
            last_error = e
            if attempt < retries:
                wait_seconds = min(12 * attempt, 60) + random.randint(0, 5)
                print(
                    f"请求 {url} 异常: {e}，{wait_seconds} 秒后重试 {attempt}/{retries}",
                    flush=True,
                )
                time.sleep(wait_seconds)
                continue
            raise

    if last_response is not None:
        return last_response

    if last_error is not None:
        raise last_error

    raise RuntimeError(f"请求 {url} 失败")


def strip_tags(text: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<style\b[^>]*>.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_cdata_or_text(raw_text: str) -> str:
    if not raw_text:
        return ""

    text = raw_text.strip()

    if text.startswith("<?xml") or text.startswith("<root"):
        try:
            root = ET.fromstring(text)
            if root.text:
                return html.unescape(root.text.strip())
            return ""
        except Exception:
            pass

    cdata_match = re.search(r"<!\[CDATA\[(.*?)\]\]>", text, re.S)
    if cdata_match:
        return html.unescape(cdata_match.group(1).strip())

    return strip_tags(text)


def decode_digit_spans(fragment: str) -> str:
    """
    解析这种数字：

    <span class="three"></span> -> 3
    <span class="one"></span><span class="two"></span> -> 12
    """
    digits = []

    span_pattern = re.compile(
        r"<span\b[^>]*class=['\"]([^'\"]+)['\"][^>]*>(.*?)</span>",
        flags=re.S | re.I,
    )

    for class_value, inner_text in span_pattern.findall(fragment):
        classes = class_value.split()

        found_digit = None

        for cls in classes:
            if cls in DIGIT_CLASS_MAP:
                found_digit = DIGIT_CLASS_MAP[cls]
                break

            number_match = re.search(r"(\d)", cls)
            if number_match:
                found_digit = number_match.group(1)
                break

        if found_digit is not None:
            digits.append(found_digit)
            continue

        text = strip_tags(inner_text)
        if text.isdigit():
            digits.append(text)

    return "".join(digits)


def extract_today_points_from_sign_page(page_text: str) -> str:
    """
    提取今日签到积分。

    你之前给的 XPath：
    //*[@id="wp"]/div[2]/div[1]/div[2]/div/ul/li[3]/p/b[1]/span

    页面里实际数字可能是：
    <span class="three"></span>

    所以这里从签到统计区域里的 li 里解析 class 数字。
    """
    li_blocks = re.findall(r"<li\b[^>]*>(.*?)</li>", page_text, flags=re.S | re.I)

    candidates = []

    for li in li_blocks:
        if "<span" not in li:
            continue

        number = decode_digit_spans(li)
        if not number:
            continue

        label = strip_tags(li)
        candidates.append((label, number))

    # 优先找“今日”相关项
    for label, number in candidates:
        if any(keyword in label for keyword in ["今日", "今天", "本次", "获得"]):
            return number

    # 兼容你给的 li[3]：取第 3 个带数字 span 的统计项
    if len(candidates) >= 3:
        return candidates[2][1]

    # 兜底取第一个
    if candidates:
        return candidates[0][1]

    return ""


def extract_total_points_from_sign_page(page_text: str) -> str:
    """
    提取总积分。

    你给的 HTML：
    <a href="home.php?mod=space&amp;uid=14497&amp;do=profile" class="xi2">34</a>

    XPath：
    //*[@id="favatar172497"]/div[4]/table/tbody/tr/td/p/a
    """
    text = html.unescape(page_text)

    patterns = [
        # href 在前，class 在后
        r"<a\b[^>]*href=['\"]home\.php\?mod=space&uid=\d+&do=profile['\"][^>]*class=['\"][^'\"]*\bxi2\b[^'\"]*['\"][^>]*>\s*(\d+)\s*</a>",
        # class 在前，href 在后
        r"<a\b[^>]*class=['\"][^'\"]*\bxi2\b[^'\"]*['\"][^>]*href=['\"]home\.php\?mod=space&uid=\d+&do=profile['\"][^>]*>\s*(\d+)\s*</a>",
        # 宽松兜底：profile 链接中的数字
        r"<a\b[^>]*do=profile[^>]*>\s*(\d+)\s*</a>",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.S | re.I)
        if match:
            return match.group(1)

    return ""


def extract_total_credit_from_credit_page(page_text: str) -> str:
    """
    从总积分页面读取：
    //*[@id="ct"]/div[1]/div/ul[2]/li[2]/text()
    """
    ct_match = re.search(
        r"<div\b[^>]*id=['\"]ct['\"][^>]*>(.*)",
        page_text,
        flags=re.S | re.I,
    )
    scope = ct_match.group(1) if ct_match else page_text

    ul_blocks = re.findall(r"<ul\b[^>]*>(.*?)</ul>", scope, flags=re.S | re.I)
    if len(ul_blocks) < 2:
        return ""

    li_blocks = re.findall(r"<li\b[^>]*>(.*?)</li>", ul_blocks[1], flags=re.S | re.I)
    if len(li_blocks) < 2:
        return ""

    return strip_tags(li_blocks[1])


def get_total_credit(session: requests.Session) -> str:
    resp = request_with_retry(session, "GET", CREDIT_PAGE)
    resp.raise_for_status()
    return extract_total_credit_from_credit_page(resp.text)


def get_formhash(session: requests.Session) -> str:
    env_sign_url = get_env("NAIXI_SIGN_URL")
    if env_sign_url:
        match = SIGN_URL_PATTERN.match(env_sign_url)
        if not match:
            raise RuntimeError("NAIXI_SIGN_URL 格式不正确，请填写完整奶昔签到 URL")
        return match.group(1)

    env_formhash = get_env("NAIXI_FORMHASH")
    if env_formhash:
        return env_formhash

    resp = request_with_retry(session, "GET", SIGN_PAGE)
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
        return True, f"账号{index}: 签到成功"

    if any(keyword in preview for keyword in login_fail_keywords) or "login" in lower_preview:
        return False, f"账号{index}: 签到失败，Cookie 可能失效或未登录，返回: {preview}"

    if any(keyword in preview for keyword in formhash_fail_keywords):
        return False, f"账号{index}: 签到失败，formhash 可能失效，返回: {preview}"

    if any(keyword in preview for keyword in fail_keywords) or "error" in lower_preview:
        return False, f"账号{index}: 签到失败，返回: {preview}"

    if not cleaned_text and not raw_text.strip():
        return True, f"账号{index}: 签到请求完成，服务器返回空内容"

    if not cleaned_text and raw_text.strip():
        return True, f"账号{index}: 签到请求完成，接口返回空 CDATA"

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
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Referer": SIGN_PAGE,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Upgrade-Insecure-Requests": "1",
        "Cookie": cookie,
    }
    session.headers.update(headers)

    formhash = get_formhash(session)
    sign_url = get_env("NAIXI_SIGN_URL") or SIGN_API.format(formhash=formhash)

    resp = request_with_retry(session, "GET", sign_url)
    ok, message = classify_sign_result(resp.status_code, resp.text, index)

    try:
        page_resp = request_with_retry(session, "GET", SIGN_PAGE)
        page_resp.raise_for_status()
        today_points = extract_today_points_from_sign_page(page_resp.text)

        total_credit = get_total_credit(session)

        extra_parts = []

        if today_points:
            extra_parts.append(f"今日签到积分: {today_points}")
        else:
            extra_parts.append("今日签到积分: 未解析到")

        if total_credit:
            extra_parts.append(f"总积分: {total_credit}")
        else:
            extra_parts.append("总积分: 未能从总积分页解析到")

        message = f"{message}，" + "，".join(extra_parts)

    except Exception as e:
        message = f"{message}，获取总积分失败: {e}"

    return ok, message


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

    print(f"使用 Cookie 指纹: {cookie_fingerprint(cookie_raw)}，账号数: {len(cookies)}")

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
