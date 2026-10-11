"""原资料页导航的日期范围扩展，最终日期仍以原文时间为准。"""
import re
import unicodedata
from datetime import date, datetime, timedelta


def explicit_label_date(value: str, today: date) -> date | None:
    text = unicodedata.normalize("NFKC", value or "").strip()
    if text.startswith("今天") or re.fullmatch(r"\d{1,2}:\d{2}", text):
        return today
    if text.startswith("昨天"):
        return today - timedelta(days=1)
    match = re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日(?:\s+\d{1,2}:\d{2})?", text)
    if match:
        try:
            value = date(int(match[1] or today.year), int(match[2]), int(match[3]))
            return value if match[1] or value <= today else value.replace(year=today.year - 1)
        except ValueError:
            return None
    return None


def eligible_label(value: str, start: date, end: date, today: date) -> bool:
    parsed = explicit_label_date(value, today)
    if parsed:
        return start <= parsed <= end
    # 星期标签缺少年/月/日，仅作为候选；打开后再验证原文，不能据此提前停止。
    return bool(re.fullmatch(r"(?:星期|周)[一二三四五六日天1-7]", value.strip()))


def older_label(value: str, start: date, today: date) -> bool:
    parsed = explicit_label_date(value, today)
    return bool(parsed and parsed < start)


def verified_older_article(record: dict, group: str, start: date) -> bool:
    """星期标签只作候选；原文身份和真实日期核验后才可停止历史翻页。"""
    if record.get('status') != 'skipped_outside_scan_range' or not re.fullmatch(r'(?:星期|周)[一二三四五六日天1-7]', group.strip()):
        return False
    evidence = record.get('verification') or {}
    if not all(evidence.get(key) is True for key in ('url_stable', 'title_matched', 'account_matched')):
        return False
    try:
        return datetime.fromisoformat(str(record.get('publish_time'))).date() < start
    except ValueError:
        return False


def newer_price_title(title: str, end: date, today: date) -> bool:
    """Only skip clearly newer dated price cards; never stop on an unverified title.

    Weekday groups remain ambiguous. A full month/day in the price headline
    lets us skip newer cards without opening every recent weekday article.
    Missing, invalid or conflicting dates retain original-page verification.
    """
    text = unicodedata.normalize("NFKC", title or "")
    matches = re.findall(r"(?:(20\d{2})[年./-])?(\d{1,2})[月./-](\d{1,2})(?:日)?", text)
    days = set()
    for year, month, day in matches:
        try:
            parsed = date(int(year or today.year), int(month), int(day))
            if not year and parsed > today:
                parsed = parsed.replace(year=today.year - 1)
            days.add(parsed)
        except ValueError:
            return False
    return len(days) == 1 and next(iter(days)) > end
