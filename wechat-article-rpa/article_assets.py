"""正文结构保留和本地图片 OCR；不执行网页脚本。"""
from __future__ import annotations
import hashlib
import html
import io
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import requests
from bs4 import BeautifulSoup
from PIL import Image


def image_url(value: str) -> str:
    value = html.unescape(value or "").replace(r"\x26", "&").replace(r"\/", "/")
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlsplit(value)
    # 图片请求仅发往正文使用的微信 CDN，不携带账号 Cookie。
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"mmbiz.qpic.cn", "mmbiz.qlogo.cn"} or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        return ""
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def extract_assets(document: str) -> dict:
    soup = BeautifulSoup(document, "html.parser")
    body = soup.select_one("#js_content")
    tables, images, json_data = [], [], []
    if body:
        for table in body.find_all("table"):
            rows = [[cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"], recursive=False)]
                    for row in table.find_all("tr")]
            tables.append({"rows": rows, "html": str(table)})
        for node in body.find_all("img"):
            url = image_url(node.get("data-src") or node.get("src") or "")
            if url:
                images.append({"url": url, "alt": node.get("alt", ""), "kind": "body"})
        for tag in body.select("script,style,iframe,object,embed,video,audio"):
            tag.decompose()
        for node in body.find_all(True):
            for key in list(node.attrs):
                if key.lower().startswith("on"):
                    del node.attrs[key]
    # 图片消息的图片数组位于 cgiDataNew，限定为该消息脚本中的 CDN 地址。
    if soup.select_one("#js_article.share_content_page"):
        for script in soup.find_all("script"):
            value = script.get_text()
            if "window.cgiDataNew" in value and re.search(r"item_show_type\s*:\s*['\"]8['\"]", value):
                for raw in re.findall(r"['\"](?:cdn_url|image_url|url)['\"]\s*:\s*['\"]([^'\"]+)['\"]|\b(?:cdn_url|image_url)\s*:\s*['\"]([^'\"]+)['\"]", value):
                    url = image_url(raw[0] or raw[1])
                    if url:
                        images.append({"url": url, "alt": "", "kind": "picture_message"})
    for script in soup.select('script[type="application/json"],script[type="application/ld+json"]'):
        try:
            json_data.append({"id": script.get("id", ""), "data": json.loads(script.get_text())})
        except (ValueError, TypeError):
            pass
    images = list({item["url"]: item for item in images}.values())
    return {"content_html": str(body) if body else "", "tables": tables, "images": images,
            "embedded_json": json_data, "structured_version": 1}


def table_grid(document: str) -> list[list[str]]:
    """展开合并单元格，保留每个数字对应的列位置。"""
    soup = BeautifulSoup(document, "html.parser")
    grid = []
    occupied = {}
    for row_index, row in enumerate(soup.find_all("tr")):
        column = 0
        for cell in row.find_all(["td", "th"], recursive=False):
            while (row_index, column) in occupied:
                column += 1
            colspan = min(50, max(1, int(cell.get("colspan", 1))))
            rowspan = min(100, max(1, int(cell.get("rowspan", 1))))
            text = cell.get_text(" ", strip=True)
            for r in range(row_index, row_index + rowspan):
                for c in range(column, column + colspan):
                    occupied[r, c] = text
            column += colspan
        width = max((c for r, c in occupied if r == row_index), default=-1) + 1
        grid.append([occupied.get((row_index, c), "") for c in range(width)])
    return grid


def capture_images(images: list[dict], directory: Path, *, ocr=True) -> list[dict]:
    directory.mkdir(parents=True, exist_ok=True)
    cache_path = directory / "image_cache.json"
    try:
        cached = {item["url"]: item for item in json.loads(cache_path.read_text(encoding="utf-8"))}
    except (OSError, ValueError, KeyError):
        cached = {}
    client = requests.Session()
    client.trust_env = False
    engine = None
    results = []
    for item in images[:30]:
        result = dict(item)
        url = image_url(item.get("url", ""))
        if not url:
            continue
        old = cached.get(url)
        if old and old.get("status") == "saved" and Path(old.get("local_path", "")).is_file() and (not ocr or "ocr_blocks" in old):
            if hashlib.sha256(Path(old["local_path"]).read_bytes()).hexdigest() == old.get("sha256"):
                results.append(old)
                continue
        try:
            with client.get(url, headers={"Referer": "https://mp.weixin.qq.com/", "User-Agent": "Mozilla/5.0"}, timeout=(10, 25), stream=True, allow_redirects=False) as response:
                response.raise_for_status()
                if response.is_redirect or not response.headers.get("Content-Type", "").startswith("image/"):
                    raise ValueError("图片响应无效")
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > 12 * 1024 * 1024:
                        raise ValueError("图片超过12MB")
            with Image.open(io.BytesIO(data)) as picture:
                if picture.width * picture.height > 30_000_000:
                    raise ValueError("图片像素超过上限")
                picture.load()
                extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}.get(picture.format, ".img")
                result["width"], result["height"] = picture.size
            path = directory / (hashlib.sha256(data).hexdigest() + extension)
            path.write_bytes(data)
            result.update(local_path=str(path.resolve()), sha256=hashlib.sha256(data).hexdigest(), status="saved")
            if ocr:
                if engine is None:
                    from rapidocr_onnxruntime import RapidOCR
                    engine = RapidOCR()
                blocks, _ = engine(str(path))
                result["ocr_blocks"] = [{"box": block[0], "text": block[1], "confidence": float(block[2])} for block in (blocks or [])]
        except Exception as error:
            result.update(status="failed", error=type(error).__name__ + ": " + str(error)[:160])
        results.append(result)
        cache_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


def ocr_rows(blocks: list[dict], *, threshold=0.92) -> list[dict]:
    """按图中行位置保留单元格，不将不同产品的数字连接到一起。"""
    rows = []
    for block in sorted(blocks, key=lambda b: sum(p[1] for p in b["box"]) / 4):
        box = block["box"]
        y = sum(p[1] for p in box) / 4
        height = max(p[1] for p in box) - min(p[1] for p in box)
        target = next((r for r in rows if abs(r["y"] - y) <= min(height, r["height"]) * 0.45), None)
        if target is None:
            target = {"y": y, "height": height, "cells": []}
            rows.append(target)
        target["cells"].append({**block, "x": sum(p[0] for p in box) / 4})
    for row in rows:
        row["cells"].sort(key=lambda c: c["x"])
        row["text"] = "\t".join(c["text"] for c in row["cells"])
        row["confidence"] = min(c["confidence"] for c in row["cells"])
        row["accepted_confidence"] = row["confidence"] >= threshold
    return rows
