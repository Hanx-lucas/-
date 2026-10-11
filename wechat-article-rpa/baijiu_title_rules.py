"""行情标题初筛：市场评论中的“价格/飞天”不能替代每日价格报道。"""
import re
import unicodedata


def is_price_title(title: str) -> bool:
    title = re.sub(r"\s+", "", unicodedata.normalize("NFKC", title or ""))
    if re.search(r"酒价|行情|参考价|价格表|批发价", title):
        return True
    has_day = re.search(r"\d{1,2}[月./-]\d{1,2}(?:日)?|今日|今天|每日", title)
    return bool(has_day and re.search(r"价格|飞天|报价", title))
