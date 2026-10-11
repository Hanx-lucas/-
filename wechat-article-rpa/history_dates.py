"""原资料页导航的日期范围扩展，最终日期仍以原文时间为准。"""
import re
import unicodedata
from datetime import date, timedelta


def explicit_label_date(value: str, today: date) -> date | None:
    text = unicodedata.normalize("NFKC", value or "").strip()
    if text.startswith("今天") or re.fullmatch(r"\d{1,2}:\d{2}", text):
        return today
    if text.startswith("昨天"):
        return today - timedelta(days=1)
    match = re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", text)
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
