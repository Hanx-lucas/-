import argparse
import csv
import json
import re
import time
import sys
from http.client import HTTPException
import unicodedata
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen


DATE_RE = re.compile(r"(?:(20\d{2})[年/\-.])?\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})\s*日?")
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)


def norm_text(value) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    return text.replace("\u00a0", " ").strip()


def compact(value: str) -> str:
    return re.sub(r"\s+", "", norm_text(value)).lower()


def strip_html(text: str) -> str:
    text = unescape(text)
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|tr|li|h\d|table)\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(td|th)\s*>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def decode_bytes(data: bytes, content_type: str = "") -> str:
    encodings = []
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if match:
        encodings.append(match.group(1))
    head = data[:2000].decode("ascii", errors="ignore")
    match = re.search(r"charset=[\"']?([\w-]+)", head, re.I)
    if match:
        encodings.append(match.group(1))
    encodings.extend(["utf-8", "gb18030"])
    for enc in encodings:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="ignore")


def http_get(url: str, timeout: int = 20) -> tuple[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }
    req = Request(quote(url, safe=":/?&=%#;+,$@[]!()*"), headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        return decode_bytes(data, resp.headers.get("content-type", "")), resp.geturl()


def parse_date(text: str, default_year: int) -> date | None:
    match = DATE_RE.search(text or "")
    if not match:
        return None
    year = int(match.group(1) or default_year)
    month = int(match.group(2))
    day = int(match.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def sanitize_filename(value: str, max_len: int = 80) -> str:
    value = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", norm_text(value))
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return (value or "article")[:max_len]


def extract_title(html: str) -> str:
    patterns = [
        r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:title["\']',
        r"var\s+msg_title\s*=\s*['\"]([^'\"]+)['\"]",
        r"<title[^>]*>(.*?)</title>",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.I | re.S)
        if match:
            return norm_text(unescape(match.group(1)))
    text = strip_html(html)
    return text.splitlines()[0] if text.splitlines() else ""


def looks_blocked(html: str) -> bool:
    probe = compact(strip_html(html)[:3000])
    blocked_terms = ["验证码", "安全验证", "请输入验证码", "访问过于频繁", "antispider", "访问环境异常"]
    return any(compact(term) in probe for term in blocked_terms)


def extract_wechat_links(search_html: str) -> list[str]:
    links = []
    seen = set()
    for raw in HREF_RE.findall(search_html):
        href = unescape(raw)
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("/link?"):
            href = "https://weixin.sogou.com" + href
        parsed = urlparse(href)
        if parsed.hostname == "mp.weixin.qq.com":
            url = href
        elif parsed.hostname == "weixin.sogou.com" and parsed.path.startswith("/link"):
            qs = parse_qs(parsed.query)
            candidate = unquote(qs.get("url", [""])[0])
            url = candidate if urlparse(candidate).hostname == "mp.weixin.qq.com" else href
        else:
            continue
        if url and url not in seen:
            seen.add(url)
            links.append(url)
    return links


def sogou_search(query: str, timeout: int) -> tuple[list[str], str]:
    url = "https://weixin.sogou.com/weixin?type=2&query=" + quote(query)
    html, final_url = http_get(url, timeout=timeout)
    if looks_blocked(html):
        return [], "blocked"
    return extract_wechat_links(html), final_url


def date_variants(day: date) -> list[str]:
    return [f"{day.month}月{day.day}日", f"{day:%m月%d日}", f"{day.month}.{day.day}", f"{day.month}/{day.day}"]


def search_queries(account: str, day: date, rules: list[str]) -> list[str]:
    queries = []
    for day_text in date_variants(day):
        for rule in rules:
            queries.append(f"{account} {day.year}年 {day_text} {rule}")
    return queries


def save_article(html: str, final_url: str, account: str, trade_date: date, output_dir: Path, overwrite: bool) -> Path:
    title = extract_title(html)
    safe_title = sanitize_filename(title)
    account_dir = output_dir / account
    account_dir.mkdir(parents=True, exist_ok=True)
    path = account_dir / f"{trade_date:%Y-%m-%d}_{safe_title}.html"
    if path.exists() and not overwrite:
        return path
    source_note = f"\n<!-- source_url: {final_url} -->\n"
    path.write_text(html + source_note, encoding="utf-8")
    return path


def fetch_direct_url(url: str, account: str, trade_date: date, output_dir: Path, timeout: int, overwrite: bool) -> dict:
    try:
        html, final_url = http_get(url, timeout=timeout)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, HTTPException) as exc:
        return {"date": trade_date.isoformat(), "account": account, "url": url, "status": "download_failed", "note": str(exc)}
    if looks_blocked(html):
        return {"date": trade_date.isoformat(), "account": account, "url": final_url, "status": "blocked", "note": "页面触发验证或访问受限"}

    if urlparse(final_url).hostname != "mp.weixin.qq.com":
        return {"date": trade_date.isoformat(), "account": account, "url": final_url,
                "status": "redirect_unresolved", "note": "搜索跳转未到达微信文章，未保存"}
    title = extract_title(html)
    publisher = re.search(r"""(?:var\s+nickname\s*=\s*["']([^"']+)|id=["']js_name["'][^>]*>(.*?)</)""", html, re.S)
    publisher_name = norm_text(strip_html(next((x for x in publisher.groups() if x), ""))) if publisher else ""
    stamp = re.search(r"""var\s+ct\s*=\s*["']?(\d{10})""", html)
    from datetime import datetime, timezone
    publication = datetime.fromtimestamp(int(stamp.group(1)), timezone(timedelta(hours=8))).date() if stamp else None
    if compact(publisher_name) != compact(account) or publication != trade_date or parse_date(title, trade_date.year) != trade_date:
        return {"date": trade_date.isoformat(), "account": account, "url": final_url,
                "status": "metadata_mismatch", "note": f"公众号/发布日期/标题日期未核验通过: {publisher_name} {publication} {title}"}
    path = save_article(html, final_url, account, trade_date, output_dir, overwrite)
    return {"date": trade_date.isoformat(), "account": account, "url": final_url, "status": "saved", "note": str(path)}


def read_url_list(path: Path, default_year: int) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        has_header = bool(sample.splitlines()) and "url" in sample.splitlines()[0].lower()
        if has_header:
            reader = csv.DictReader(f)
            for row in reader:
                url = norm_text(row.get("url"))
                if not url:
                    continue
                trade_date = parse_date(row.get("date") or row.get("日期") or "", default_year)
                rows.append({"url": url, "account": norm_text(row.get("account") or row.get("公众号")), "date": trade_date})
        else:
            for line in f:
                url = norm_text(line)
                if url and not url.startswith("#"):
                    rows.append({"url": url, "account": "", "date": None})
    return rows


def write_report(rows: list[dict], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"fetch_report_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "account", "url", "status", "note"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def resolve_path(base: Path, raw: str, prefer_base: bool = False) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    cwd_path = (Path.cwd() / path).resolve()
    base_path = (base / path).resolve()
    if prefer_base:
        return base_path if base_path.exists() else cwd_path
    return cwd_path if cwd_path.exists() else base_path


def main():
    parser = argparse.ArgumentParser(description="自动搜索并下载微信公众号白酒行情文章。")
    parser.add_argument("--config", default="config/baijiu_price_config.json", help="配置文件路径")
    parser.add_argument("--output-dir", default="article_inputs", help="文章保存目录")
    parser.add_argument("--url-list", default="", help="可选：包含 url/account/date 的 CSV 或纯 URL 文本")
    parser.add_argument("--start", default="", help="开始日期 YYYY-MM-DD；默认最近七天")
    parser.add_argument("--end", default="", help="结束日期 YYYY-MM-DD；默认今天")
    parser.add_argument("--timeout", type=int, default=20, help="单次请求超时秒数")
    parser.add_argument("--sleep", type=float, default=1.5, help="搜索/下载间隔秒数")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已保存文章")
    parser.add_argument("--no-search", action="store_true", help="只下载 --url-list，不自动搜索")
    parser.add_argument("--max-queries", type=int, default=2, help="每个公众号每日最多查询次数")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent
    config_path = resolve_path(base, args.config, prefer_base=True)
    output_dir = resolve_path(base, args.output_dir)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=6)
    if start > end or args.max_queries < 1:
        parser.error("日期区间或最大查询数无效")

    report = []
    url_rows = read_url_list(resolve_path(base, args.url_list), end.year) if args.url_list else []
    for item in url_rows:
        account = item["account"] or "未识别公众号"
        trade_date = item["date"] or parse_date(item["url"], end.year) or end
        report.append(fetch_direct_url(item["url"], account, trade_date, output_dir, args.timeout, args.overwrite))
        time.sleep(args.sleep)

    if not args.no_search:
        current = start
        while current <= end:
            for account in config["accounts"]:
                found = False
                for query in search_queries(account, current, config.get("title_rules", {}).get(account, []))[:args.max_queries]:
                    try:
                        links, note = sogou_search(query, args.timeout)
                    except (HTTPError, URLError, TimeoutError, OSError, ValueError, HTTPException) as exc:
                        report.append({"date": current.isoformat(), "account": account, "url": "", "status": "search_failed", "note": f"{query}: {exc}"})
                        continue
                    if note == "blocked":
                        report.append({"date": current.isoformat(), "account": account, "url": "", "status": "search_blocked", "note": query})
                        break
                    for url in links[:3]:
                        result = fetch_direct_url(url, account, current, output_dir, args.timeout, args.overwrite)
                        result["note"] = f"{query} | {result['note']}"
                        report.append(result)
                        if result["status"] == "saved":
                            found = True
                            break
                        time.sleep(args.sleep)
                    if found:
                        break
                    time.sleep(args.sleep)
                if not found:
                    report.append({"date": current.isoformat(), "account": account, "url": "", "status": "not_found", "note": "公开搜索未找到可下载文章"})
            current += timedelta(days=1)

    report_path = write_report(report, base / "outputs")
    saved = sum(1 for r in report if r["status"] == "saved")
    blocked = sum(1 for r in report if "blocked" in r["status"])
    print(f"保存文章: {saved}")
    print(f"验证/访问受限: {blocked}")
    print(f"抓取报告: {report_path}")
    return 0 if saved else 2


if __name__ == "__main__":
    sys.exit(main())

