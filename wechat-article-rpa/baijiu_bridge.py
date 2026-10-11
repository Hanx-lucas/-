"""复用微信桌面采集、原文章解析和已有模板更新器的酒价入口。"""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from article_ingest import parse_page, parse_page_html, parse_publish_time, ingest
from article_assets import capture_images, ocr_rows, table_grid
from baijiu_title_rules import is_price_title
from run_control import wait_if_paused

BASE = Path(__file__).resolve().parent
WORKSPACE = BASE.parent
PRICE_APP = WORKSPACE / "baijiu_price_automation"
CONFIG = PRICE_APP / "config/baijiu_price_config.json"


def progress(percent, message):
    print("RPA_PROGRESS " + json.dumps({"percent": percent, "message": message}, ensure_ascii=False), flush=True)


def compact(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).lower().replace("°", "度")


def today_header(text, day):
    value = compact(text)
    if value in {"今日", "今天", "今日价格", "今日价", "今天价格", "最新价", "现价", "批发参考价", "今日行情", "今日参考价", "行情价", "今日行情价"}:
        return True
    if value == f"{day.day}日":
        return True
    return bool(re.fullmatch(rf"(?:{day.year}[年./-])?0?{day.month}[月./-]0?{day.day}(?:日)?(?:价格|价)?", value))


def price_number(value):
    value = compact(value).replace("元/瓶", "").replace("元", "").replace(",", "")
    value = re.sub(r"[⬇⬆↓↑]+$", "", value)
    if re.fullmatch(r"\d{3,4}(?:\.0+)?", value) and 100 <= float(value) <= 5000:
        return int(float(value))
    return None


def match_product(text, account, config):
    matches = []
    for name, spec in config["products"].items():
        for alias in spec["aliases"].get(account, []):
            if alias != "-" and compact(alias) in compact(text):
                # 短别名不能吞掉明确的容量/包装变体，例如“精品茅台375ml品鉴装”。
                value, normalized_alias = compact(text), compact(alias)
                if normalized_alias[-1:].isdigit():
                    pattern = ''.join(re.escape(ch) + r'\s*' for ch in normalized_alias[:-1]) + re.escape(normalized_alias[-1]) + r'(?!\d)'
                    raw = unicodedata.normalize('NFKC', str(text)).lower().replace('°', '度')
                    if not re.search(pattern, raw):
                        continue
                if any(term in value and term not in normalized_alias for term in ("品鉴装", "礼盒", "12瓶", "雅致版")):
                    continue
                volumes = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)(ml|毫升|l|升)", value)
                expected_ml = 1000 if "公斤" in name else 100 if "100ml" in compact(name) else 500
                if any(float(number) * (1000 if unit in {"l", "升"} else 1) != expected_ml for number, unit in volumes):
                    continue
                matches.append((len(compact(alias)), name, alias))
    # 最长名称优先，例如飞天100ml不会误映射普通飞天。
    matches.sort(reverse=True)
    if not matches or len({m[1] for m in matches if m[0] == matches[0][0]}) != 1:
        return None
    return matches[0][1:]


_RETRY_ENGINE = None
_RETRY_CACHE = {}


def refine_product_rows(picture, account, config):
    """Retry a low-confidence product row at original resolution, keeping coordinates.

    Long price posters are downscaled by the first OCR pass. Only replace an
    entire row when a separate cropped pass confirms the same product and all
    cells pass the existing 0.92 threshold; never reduce the acceptance threshold.
    """
    global _RETRY_ENGINE
    import copy
    from PIL import Image
    rows = ocr_rows(picture.get("ocr_blocks", []))
    key = (picture.get("sha256"), account, json.dumps(config["products"], ensure_ascii=False, sort_keys=True))
    if key in _RETRY_CACHE:
        return copy.deepcopy(_RETRY_CACHE[key])
    retries = []
    candidates = [(i, row, match_product(row["text"], account, config)) for i, row in enumerate(rows)
                  if row["confidence"] < 0.92]
    candidates = [(i, row, product) for i, row, product in candidates if product][:12]
    if candidates:
        with Image.open(picture["local_path"]) as image:
            for index, row, product in candidates:
                top = max(0, int(min(p[1] for c in row["cells"] for p in c["box"]) - 8))
                bottom = min(image.height, int(max(p[1] for c in row["cells"] for p in c["box"]) + 9))
                if _RETRY_ENGINE is None:
                    from rapidocr_onnxruntime import RapidOCR
                    _RETRY_ENGINE = RapidOCR()
                import numpy as np
                from PIL import ImageOps
                # Re-read original name cells with padding around small symbols.
                cell_blocks = []
                for cell_index, cell in enumerate(row['cells']):
                    previous = row['cells'][cell_index - 1] if cell_index else None
                    following = row['cells'][cell_index + 1] if cell_index + 1 < len(row['cells']) else None
                    left = int((max(p[0] for p in previous['box']) + min(p[0] for p in cell['box'])) / 2) if previous else 0
                    right = int((max(p[0] for p in cell['box']) + min(p[0] for p in following['box'])) / 2) if following else image.width
                    left = max(left, int(min(p[0] for p in cell['box']) - (bottom - top) * 0.75))
                    import math
                    right = min(right, math.ceil(max(p[0] for p in cell['box']) + (bottom - top)))
                    original_crop = image.crop((left, top, right, bottom)).convert('RGB')
                    cell_scale = 2
                    crop = ImageOps.expand(original_crop.resize((original_crop.width * cell_scale, original_crop.height * cell_scale)), border=30, fill='white')
                    recognized, _ = _RETRY_ENGINE(np.asarray(crop))
                    for b in (recognized or []):
                        cell_blocks.append({'box': [[(p[0] - 30) / 2 + left, (p[1] - 30) / 2 + top] for p in b[0]], 'text': b[1], 'confidence': float(b[2])})
                blocks, _ = _RETRY_ENGINE(np.asarray(image.crop((0, top, image.width, bottom)).convert("RGB")))
                shifted = [{"box": [[p[0], p[1] + top] for p in b[0]], "text": b[1], "confidence": float(b[2])} for b in (blocks or [])]
                replacement = ocr_rows(shifted)
                if cell_blocks and all(c['confidence'] >= 0.92 for c in cell_blocks):
                    replacement = ocr_rows(cell_blocks)
                retry_product = match_product(replacement[0]["text"], account, config) if len(replacement) == 1 else None
                original_prices = [price_number(c['text']) for c in row['cells'] if price_number(c['text']) is not None]
                retry_prices = [price_number(c['text']) for c in replacement[0]['cells'] if price_number(c['text']) is not None] if len(replacement) == 1 else []
                accepted = len(replacement) == 1 and replacement[0]["confidence"] >= 0.92 and retry_product is not None and retry_product[0] == product[0] and retry_prices == original_prices
                retries.append({"product": product[0], "crop": [0, top, image.width, bottom], "accepted": accepted,
                                "original": row["text"], "retry": [r["text"] for r in replacement],
                                "confidence": [r["confidence"] for r in replacement]})
                if accepted:
                    rows[index] = replacement[0]
    result = (rows, retries)
    _RETRY_CACHE[key] = copy.deepcopy(result)
    return result


def extract_rows(rows, account, day, config, kind):
    quotes, review = [], []
    current_column = None
    current_x = None
    for row in rows:
        cells = row["cells"]
        texts = [str(c["text"]) for c in cells]
        header_candidates = [index for index, text in enumerate(texts)
                             if len(cells) >= 2 and today_header(text, day)
                             and cells[index].get("confidence", 1) >= 0.92]
        # colspan 展开后的日期横跨行情、零售与涨跌三列，不能取最后一列。
        # 等待下一层唯一的“行情价”或“今日”表头给出列位置。
        if len(header_candidates) == 1:
            current_column = header_candidates[0]
            current_x = cells[current_column].get("x")
        product = match_product(" ".join(texts), account, config)
        if not product:
            continue
        evidence = "\t".join(texts)
        if row.get("confidence", 1) < 0.92:
            review.append({"product": product[0], "reason": "OCR置信度不足", "evidence": evidence})
            continue
        numbers = [(i, price_number(text)) for i, text in enumerate(texts) if price_number(text) is not None]
        selected = None
        if current_column is not None:
            if kind == "ocr" and current_x is not None:
                candidates = [(abs(cells[i]["x"] - current_x), value, i) for i, value in numbers]
                candidates.sort()
                # 数字须落在已识别今日表头正下方，不用文字顺序猜列。
                if candidates:
                    nearest = candidates[0]
                    widths = [max(p[0] for p in cells[nearest[2]]["box"]) - min(p[0] for p in cells[nearest[2]]["box"])]
                    if nearest[0] <= max(25, widths[0] * 0.8) and (len(candidates) == 1 or candidates[1][0] > nearest[0] * 2 + 10):
                        selected = nearest[1]
            elif current_column < len(texts):
                selected = price_number(texts[current_column])
        elif kind == "table" and len(numbers) == 1:
            selected = numbers[0][1]
        if selected is None:
            review.append({"product": product[0], "reason": "无法确认今日价格列", "evidence": evidence})
            continue
        quotes.append({"product": product[0], "alias": product[1], "price": selected,
                       "date": day.isoformat(), "account": account, "kind": kind, "evidence": evidence})
    return quotes, review


def valid_url(url):
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "mp.weixin.qq.com" or parsed.username or parsed.password or parsed.port not in {None, 80, 443} or not (parsed.path == "/s" or parsed.path.startswith("/s/")):
        raise ValueError("只接受微信原文链接")
    public_query = [(key, value) for key, value in parse_qsl(parsed.query) if key in {"__biz", "mid", "idx", "sn", "chksm"}]
    return urlunsplit(("https", parsed.netloc, parsed.path, urlencode(public_query), ""))


def export_records(records, start, end, run_dir, *, download_images=True):
    run_dir = Path(run_dir).resolve()
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    input_dir = run_dir / "price_inputs" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid4().hex)
    input_dir.mkdir(parents=True, exist_ok=True)
    methods = sorted({r.get("verification", {}).get("method", "wechat_desktop_rpa") for r in records})
    report = {"start": start.isoformat(), "end": end.isoformat(), "articles": [], "review": [], "inputs": str(input_dir), "collection_methods": methods,
              "channel": methods[0] if len(methods) == 1 else "mixed"}
    seen = set()
    for index, record in enumerate(records):
        wait_if_paused()
        progress(70 + 18 * index / max(len(records), 1),
                 f"正在识别文章表格与图片 {index + 1}/{len(records)}：{record.get('account_name', '')}")
        item = {"title": record.get("title", ""), "account": record.get("account_name", ""), "url": ""}
        try:
            url = valid_url(record["url"])
            item["url"] = url
            account = item["account"]
            if account not in config["accounts"]:
                raise ValueError("公众号不在六个目标账号中")
            verified = record.get("verification", {})
            item["collection_method"] = verified.get("method", "wechat_desktop_rpa")
            if not all(verified.get(key) is True for key in ("url_stable", "title_matched", "account_matched")):
                raise ValueError("缺少原项目身份校验证据")
            day = parse_publish_time(str(record["publish_time"])).date()
            item["date"] = day.isoformat()
            if not start <= day <= end:
                raise ValueError("原始发布时间不在所选范围")
            # 行情标题出现具体月日时必须与发布日一致，保留已有日期冲突保护。
            dates = re.findall(r"(?:(20\d{2})[年./-])?(\d{1,2})[月./-](\d{1,2})(?:日)?", item["title"])
            if dates and any(date(int(y or day.year), int(m), int(d)) != day for y, m, d in dates):
                raise ValueError("标题行情日期与原文发布时间冲突")
            if not is_price_title(item["title"]):
                raise ValueError("非行情文章")
            identity = hashlib.sha256(url.encode()).hexdigest()[:16]
            if (account, identity) in seen:
                continue
            seen.add((account, identity))
            directory = run_dir / "assets" / identity
            directory.mkdir(parents=True, exist_ok=True)
            quotes, review = [], []
            for table in record.get("tables", []):
                grid = table_grid(table["html"]) if table.get("html") else table["rows"]
                rows = [{"cells": [{"text": text} for text in row]} for row in grid]
                found, gaps = extract_rows(rows, account, day, config, "table")
                quotes.extend(found)
                review.extend(gaps)
            pictures = capture_images(record.get("images", []), directory) if download_images else []
            if len(record.get("images", [])) > 30:
                review.append({"reason": "正文图片超过单篇30张处理上限，余下图片未识别", "remaining": len(record["images"]) - 30})
            for picture in pictures:
                if picture["status"] != "saved":
                    review.append({"reason": "图片下载或OCR失败", "image": picture["url"]})
                    continue
                rows, retries = refine_product_rows(picture, account, config)
                picture["ocr_retry"] = retries
                found, gaps = extract_rows(rows, account, day, config, "ocr")
                for quote in found:
                    quote["image_path"] = picture["local_path"]
                quotes.extend(found)
                review.extend(gaps)
            by_product = {}
            for quote in quotes:
                by_product.setdefault(quote["product"], []).append(quote)
            accepted = []
            for product, options in by_product.items():
                if len({q["price"] for q in options}) == 1:
                    accepted.append(options[0])
                else:
                    review.append({"product": product, "reason": "表格/图片价格冲突，未写入"})
            # 结构化报价转成既有更新器的输入，仅写一列已确认今日价格。
            content = "\n".join(f"{q['alias']}\t{q['price']}" for q in accepted)
            if not record.get("tables") and not record.get("images"):
                content = record.get("content", "")
            destination = input_dir / account / f"{day}_{identity}.txt"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if content.strip():
                destination.write_text(f"{account}\n{item['title']}\n{day}\n{content}", encoding="utf-8")
            evidence = {**record, "downloaded_images": pictures, "quotes": accepted, "review": review}
            (directory / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            if record.get("content_html"):
                (directory / "body.html").write_text(record["content_html"], encoding="utf-8")
            item.update(status="processed", structured_quotes=len(accepted), images=len(pictures), evidence=str(directory / "evidence.json"))
            report["review"].extend({**gap, "account": account, "date": str(day), "url": url} for gap in review)
        except (KeyError, ValueError, TypeError) as error:
            item.update(status="rejected", reason=str(error))
            if str(error) != "非行情文章":
                report["review"].append({"account":item["account"], "title":item["title"],
                                        "date":item.get("date", ""), "url":item["url"],
                                        "reason":str(error), "status":"rejected"})
        report["articles"].append(item)
    path = run_dir / "bridge_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--jsonl", help="原采集器导出的 articles.jsonl")
    parser.add_argument("--url", action="append", default=[], help="补录已知原文链接")
    parser.add_argument("--live", action="store_true", help="复用原采集器操作微信桌面")
    parser.add_argument("--account", action="append", default=[])
    parser.add_argument("--workbook")
    parser.add_argument("--cumulative", action="store_true", help="定时任务接续上次生成的数据库副本")
    parser.add_argument("--output-dir")
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--no-images", action="store_true")
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if not 0 <= (end - start).days <= 31:
        parser.error("选择不超过32天的有效日期范围")
    run_dir = Path(args.output_dir) if args.output_dir else BASE / "output" / ("baijiu-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ["RPA_SAVE_STRUCTURED"] = "1"
    records = []
    if args.live:
        from rpa_control_panel import collect_preflight
        preflight = collect_preflight()
        (run_dir / "preflight.json").write_text(json.dumps(preflight, ensure_ascii=False), encoding="utf-8")
        if not preflight["ready"]:
            raise RuntimeError(preflight["search"]["message"])
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        accounts = args.account or config["accounts"]
        if any(a not in config["accounts"] for a in accounts):
            raise ValueError("公众号不在目标账号中")
        jsonl = run_dir / "articles.jsonl"
        command = [sys.executable, str(BASE / "wechat_visual_rpa.py"), "--run-search-accounts", "--live", "--local-only", "--metrics", "none", "--price-only", "--start-date", str(start), "--end-date", str(end), "--max-pages", "48", "--max-articles", "40", "--task-timeout-minutes", "6", "--output-dir", str(run_dir / "desktop"), "--export-jsonl", str(jsonl), "--export-csv", str(run_dir / "articles.csv")]
        for account in accounts:
            command.extend(["--account-name", account])
        collection = subprocess.run(command, cwd=BASE, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                    timeout=None if os.environ.get("RPA_PAUSE_FILE") else 2400)
        if jsonl.exists():
            records.extend(json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip())
        collection_errors = []
        summaries_path = run_dir / "desktop/batch-summary.json"
        if summaries_path.exists():
            summaries = json.loads(summaries_path.read_text(encoding="utf-8"))
            collection_errors = [{"account": s.get("account", ""), "reason": s["fatal_error"]}
                                 for s in summaries if s.get("fatal_error")]
        if collection.returncode and not records:
            reason = collection_errors[-1]["reason"] if collection_errors else "微信桌面采集未完成"
            raise RuntimeError(reason + "；未取得可用文章，未更新 Excel")
    if args.jsonl:
        records.extend(json.loads(line) for line in Path(args.jsonl).read_text(encoding="utf-8").splitlines() if line.strip())
    for url in args.url:
        wait_if_paused()
        progress(65, "正在读取补录的微信原文链接")
        url = valid_url(url)
        page = parse_page(url)
        record = ingest(url, {}, "", "", "", dry_run=True, page=page)
        record["verification"] = {"url_stable": True, "title_matched": True, "account_matched": True, "method": "direct_original_page"}
        records.append(record)
    if not records:
        raise RuntimeError("没有取得文章，未更新Excel")
    report = export_records(records, start, end, run_dir, download_images=not args.no_images)
    if args.live:
        report["collection_errors"] = collection_errors
        report["collection_incomplete"] = bool(collection.returncode or collection_errors)
        (run_dir / "bridge_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    progress(88, "文章识别完成，准备更新 Excel 数据库")
    if not args.collect_only:
        if not args.workbook:
            raise ValueError("更新Excel须指定 --workbook")
        # 复用现有数据库更新器及模板映射，不另写一套 Excel 实现。
        runtime = Path(os.getenv("RPA_EXCEL_PYTHON") or str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"))
        if not runtime.is_file():
            raise RuntimeError("请用 RPA_EXCEL_PYTHON 指定已安装现有 Excel 更新器依赖的 Python")
        output = PRICE_APP / "outputs" / f"白酒价格数据库_RPA_{start:%Y%m%d}_{end:%Y%m%d}_{datetime.now():%Y%m%d_%H%M%S_%f}.xlsx"
        workbook = Path(args.workbook).resolve()
        if not workbook.is_relative_to(WORKSPACE) or workbook.suffix.lower() != ".xlsx" or not workbook.is_file():
            raise ValueError("模板须为工作区内现有xlsx文件")
        state_path = BASE / "config/baijiu_state.json"
        if args.cumulative:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                previous = Path(state["excel"]).resolve()
                if previous.parent == (PRICE_APP / "outputs").resolve() and not previous.is_file():
                    archived = PRICE_APP / "outputs" / "archive" / "databases" / previous.name
                    if archived.is_file():
                        previous = archived.resolve()
                if state["base_workbook"] == str(workbook) and previous.is_relative_to(PRICE_APP / "outputs") and previous.is_file():
                    selected_workbook = previous
                else:
                    selected_workbook = workbook
            except (OSError, ValueError, KeyError):
                selected_workbook = workbook
        else:
            selected_workbook = workbook
        command = [str(runtime), str(PRICE_APP / "update_baijiu_prices.py"), "--workbook", str(selected_workbook), "--articles-dir", report["inputs"], "--start", str(start), "--end", str(end), "--output", str(output)]
        completed = subprocess.run(command, cwd=PRICE_APP, env=dict(os.environ, PYTHONIOENCODING="utf-8"), capture_output=True, text=True, encoding="utf-8")
        print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        completed.check_returncode()
        report["excel"] = str(output)
        for prefix, key in [("匹配日志: ", "matches"), ("缺失复核: ", "missing")]:
            report[key] = next(line[len(prefix):] for line in completed.stdout.splitlines() if line.startswith(prefix))
        if args.cumulative:
            state_path.write_text(json.dumps({"base_workbook":str(workbook),"excel":str(output)},ensure_ascii=False),encoding="utf-8")
        (run_dir / "bridge_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(run_dir / "bridge_report.json"), "articles": len(report["articles"]), "review": len(report["review"]), "excel": report.get("excel")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
