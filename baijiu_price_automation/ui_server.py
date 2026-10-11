import argparse
import base64
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from database_versions import DatabaseVersions
from workbook_imports import DatabaseImports

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
OUTPUTS = BASE / "outputs"
STATE = {"running": False, "message": "等待操作", "result": None, "log": "", "progress": 0, "job_id": None}
LOCK = threading.RLock()
CONTROL_ENV = {}

def request_pause(job_id):
    with LOCK:
        if not STATE["running"] or job_id != STATE.get("job_id"):
            raise ValueError("任务已结束或已变更，请刷新页面")
        if not STATE.get("pausable"):
            raise ValueError("当前阶段正在写入数据库，请等待完成")
        Path(CONTROL_ENV["RPA_PAUSE_FILE"]).touch()
        STATE["pause_requested"] = True

def request_resume(job_id):
    with LOCK:
        if not STATE["running"] or job_id != STATE.get("job_id"):
            raise ValueError("任务已结束或已变更，请刷新页面")
        Path(CONTROL_ENV["RPA_PAUSE_FILE"]).unlink(missing_ok=True)
        STATE.update(pause_requested=False, paused=False)

def job_boundary():
    with LOCK:
        paused = STATE.get("pause_requested", False)
        if paused:
            STATE["paused"] = True
    while STATE.get("pause_requested", False):
        time.sleep(0.1)
    STATE["paused"] = False


def begin_write_phase():
    # Serialize the last pause request with the transition to the Excel writer.
    while True:
        job_boundary()
        with LOCK:
            if not STATE.get("pause_requested"):
                STATE["pausable"] = False
                return
PORT = 8765

def database_config():
    view = DatabaseVersions(OUTPUTS).view()
    options = []
    seen = set()
    for slot in view["slots"]:
        if not slot["available"] or slot["name"] in seen:
            continue
        seen.add(slot["name"])
        path = Path(slot["path"])
        options.append({"value": str(path.relative_to(ROOT)).replace("\\", "/"),
                        "label": f"{slot['label']} · {slot['created_at'][:10]} {slot['created_at'][11:16]}"})
    imports, active = DatabaseImports(BASE).options()
    options.extend(imports)
    if not options:
        template = ROOT / "名酒酒价各平台数据库20260516.xlsx"
        if template.is_file():
            options.append({"value": template.name, "label": "原始数据库（首次更新）"})
    return {"workbooks": [item["value"] for item in options], "workbook_options": options,
            "default_workbook": active if active in [item["value"] for item in options] else options[0]["value"] if options else "",
            "database_versions": view["slots"]}

def latest_overview():
    """Expose only public business fields from a receipt for the actual latest file."""
    config = json.loads((BASE / "config/baijiu_price_config.json").read_text(encoding="utf-8"))
    view = DatabaseVersions(OUTPUTS).view()
    latest = next((s for s in view["slots"] if s["role"] == "latest" and s["available"]), None)
    response = {"accounts": config["accounts"], "products": len(config["products"]), "latest_run": None}
    if not latest:
        return response
    receipt_path = OUTPUTS / "brand_repair_verification.json"
    if not receipt_path.is_file() or Path(json.loads(receipt_path.read_text(encoding="utf-8")).get("output", "")).name != latest["name"]:
        receipt_path = OUTPUTS / "manual_update_verification.json"
    if not receipt_path.is_file() or Path(json.loads(receipt_path.read_text(encoding="utf-8")).get("output", "")).name != latest["name"]:
        receipt_path = OUTPUTS / "year_correction_verification.json"
    rows, gaps, reviews, articles, verified, files = [], [], [], None, False, []
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if Path(receipt.get("output", "")).name == latest["name"]:
            rows = receipt.get("matches", [])
            verified = bool(receipt.get("checks")) and all(v == "passed" for v in receipt["checks"].values())
            missing_path = Path(receipt.get("missing", "")).resolve()
            if missing_path.parent == OUTPUTS.resolve() and missing_path.is_file():
                gaps = csv_rows(missing_path)
                files.append(missing_path.name)
            files.append(receipt_path.name)
            bridge = Path(receipt.get("bridge_report", "")).resolve()
            if bridge.is_relative_to(ROOT) and bridge.is_file():
                report = json.loads(bridge.read_text(encoding="utf-8"))
                reviews = report.get("review", [])
                articles = sum(i.get("status") == "processed" for i in report.get("articles", []))
                match_path = Path(report.get("matches", "")).resolve()
                if match_path.parent == OUTPUTS.resolve() and match_path.is_file():
                    files.insert(0, match_path.name)
    if not rows:
        for path in sorted(OUTPUTS.glob("run_summary_*.json"), key=lambda p:p.stat().st_mtime, reverse=True):
            summary = json.loads(path.read_text(encoding="utf-8"))
            if summary.get("excel") != latest["name"] or summary.get("sample") or summary.get("exit_code") != 0:
                continue
            files = [name for name in summary.get("files", []) if Path(name).name == name]
            match = next((OUTPUTS / name for name in files if name.startswith("matches_") and name.endswith(".csv")), None)
            missing = next((OUTPUTS / name for name in files if name.startswith("missing_review_") and name.endswith(".csv")), None)
            rows, gaps = csv_rows(match), csv_rows(missing)
            reviews = summary.get("rpa_review", [])
            articles = sum(i.get("status") == "processed" for i in summary.get("rpa_articles", [])) or None
            break
    if not rows:
        return response
    unique = {}
    for row in rows:
        key = (row["date"], row["account"], row["product"])
        unique.setdefault(key, row)
    public_rows = [{k:row.get(k, "") for k in ("date", "account", "product", "price", "sheet", "cell")} for row in unique.values()]
    public_rows.sort(key=lambda r:(r["date"], r["account"], r["product"]), reverse=True)
    response["latest_run"] = {"excel": latest["name"], "created_at": latest["created_at"], "verified": verified,
        "start": min(r["date"] for r in rows), "end": max(r["date"] for r in rows), "prices": len(unique),
        "cells": len(rows), "missing": len(gaps), "articles": articles, "review_count": len(reviews),
        "files": files, "preview": public_rows, "brand_quotes": [{"brand":brand, "quotes":sum(config['products'][r['product']]['sheet'] == brand for r in unique.values())} for brand in ('茅台','五粮液','泸州老窖','习酒')], "gaps": [{k:r.get(k, "") for k in ("date", "account", "product", "reason")} for r in gaps[:80]],
        "rpa_review": [{k:r.get(k, "") for k in ("date", "account", "product", "reason")} for r in reviews[:80]],
        "sample": False, "account_quotes": [{"account":a, "quotes":sum(r["account"] == a for r in unique.values())} for a in config["accounts"]]}
    return response

def today():
    return datetime.now(timezone(timedelta(hours=8))).date()

def csv_rows(path):
    if not path:
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def resolve_workbook(name):
    path = (ROOT / name).resolve()
    if path.parent == OUTPUTS.resolve() and not path.is_file() and path.suffix.lower() == ".xlsx":
        archived = (OUTPUTS / "archive" / "databases" / path.name).resolve()
        if archived.parent == (OUTPUTS / "archive" / "databases").resolve() and archived.is_file():
            path = archived
    if not path.is_relative_to(ROOT) or path.suffix.lower() != ".xlsx" or not path.is_file():
        raise ValueError("数据库文件必须是工作区内现有的 xlsx 文件")
    if "样例" in path.name:
        raise ValueError("正式更新不能使用样例数据库")
    return path

def set_progress(percent, message):
    STATE.update(progress=max(STATE.get("progress", 0), min(100, round(percent))), message=message)


def consume_job_line(line):
    STATE["log"] = (STATE["log"] + line)[-80000:]
    try:
        if line.startswith("RPA_CONTROL "):
            item = json.loads(line.removeprefix("RPA_CONTROL "))
            with LOCK:
                STATE["paused"] = item["event"] == "paused" and STATE.get("pause_requested", False)
            return
        if line.startswith("RPA_PROGRESS "):
            item = json.loads(line.removeprefix("RPA_PROGRESS "))
            set_progress(item["percent"], item["message"])
            return
        begin = line.find('{"event":')
        if begin < 0:
            return
        event = json.loads(line[begin:])
        kind = event["event"]
        if kind == "accounts_loaded":
            STATE["accounts_total"] = event["count"]
            STATE["accounts_completed"] = 0
        elif kind == "account_collection_started":
            STATE["account"] = event["account"]
            set_progress(5 + 65 * STATE.get("accounts_completed", 0) / max(STATE.get("accounts_total", 6), 1),
                         f"正在采集公众号 {STATE.get('accounts_completed', 0) + 1}/{STATE.get('accounts_total', 6)}：{event['account']}")
        elif kind == "account_collection_finished":
            STATE["accounts_completed"] = event["completed"]
            set_progress(5 + 65 * event["completed"] / max(event["total"], 1),
                         f"已完成公众号 {event['completed']}/{event['total']}：{event['account']}")
        elif kind == "profile_page_analyzed":
            labels = "、".join(str(x) for x in event.get("time_labels", []) if x)
            STATE["message"] = f"正在翻阅公众号列表：{event['account']} · 第 {event['page']} 屏" + (f"（{labels}）" if labels else "")
        elif kind == "article_card_skipped_newer_title":
            STATE["message"] = f"已略过结束日期之后的文章：{event['account']} · {event['title']}"
        elif kind == "article_open_attempt":
            STATE["message"] = f"正在获取链接：{event['account']} · {event['title']}" + (f"（重试 {event['attempt'] - 1}）" if event['attempt'] > 1 else '')
        elif kind == "article_collect_succeeded":
            STATE["message"] = f"已取得文章：{event['account']} · {event['title']}"
    except (ValueError, KeyError, TypeError):
        pass


def stream_child(command, cwd, timeout):
    # 实时读取子进程日志，不能等采集退出后才把状态交给网页。
    output = []
    proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            env=dict(os.environ, **CONTROL_ENV, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    def read():
        for line in proc.stdout:
            output.append(line)
            consume_job_line(line)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    active_seconds = 0.0
    previous = time.monotonic()
    try:
        while proc.poll() is None:
            time.sleep(0.1)
            now = time.monotonic()
            if not STATE.get("paused", False):
                active_seconds += now - previous
            previous = now
            if active_seconds >= timeout:
                raise subprocess.TimeoutExpired(command, timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.kill()
        proc.wait(timeout=10)
        raise RuntimeError("采集超时，已停止本次进程；请确认微信已登录且搜一搜保持打开后重试")
    finally:
        reader.join(timeout=5)
        proc.stdout.close()
    return subprocess.CompletedProcess(command, proc.returncode, ''.join(output), '')


def run_child(script, args, timeout=600):
    job_boundary()
    return stream_child([sys.executable, "-u", str(BASE/script), *args], BASE, timeout)

def run_job(payload):
    try:
        job_boundary()
        set_progress(2, "正在检查数据库和更新配置")
        mode = payload.get("mode", "local")
        start = datetime.fromisoformat(payload["start"]).date()
        end = datetime.fromisoformat(payload["end"]).date()
        if start > end or (end-start).days > 31:
            raise ValueError("请选择不超过 32 天的有效日期区间")
        workbook = resolve_workbook(payload["workbook"])
        if mode not in {"local", "auto", "dry", "sample", "history", "rpa", "rpa_links"}:
            raise ValueError("操作模式无效")
        if mode == "sample":
            start = end = datetime(2026, 6, 4).date()
        date_args = ["--start", start.isoformat(), "--end", end.isoformat()]
        STATE["log"] = ""
        fetch_report = None
        history_report = None
        rpa_report = None
        rpa_inputs = None
        before = set(OUTPUTS.glob("*.csv"))
        if mode == "auto":
            STATE["message"] = "正在获取六个公众号文章，验证公众号和日期"
            run_child("fetch_wechat_articles.py", date_args + ["--timeout", "5", "--sleep", "0.3", "--max-queries", "1"])
            new_fetch = sorted(set(OUTPUTS.glob("fetch_report_*.csv")) - before)
            fetch_report = str(new_fetch[-1]) if new_fetch else None
        if mode == "history":
            STATE["message"] = "正在采集最新文章、补抓合集历史并检查逐日缺失"
            urls = [x.strip() for x in str(payload.get("seed_urls", "")).splitlines() if x.strip()]
            if len(urls) > 20:
                raise ValueError("单次最多补抓20个微信原文/合集链接")
            for url in urls:
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"} or parsed.hostname != "mp.weixin.qq.com" or parsed.username or parsed.password or parsed.port not in {None,80,443}:
                    raise ValueError("只接受微信原文或公开合集链接")
            rss_root = ROOT / "we-mp-rss"
            if not (rss_root/"tools/capture_baijiu.py").is_file():
                raise ValueError("微信读书通道尚未部署；请先使用微信历史采集或原文链接解析，或单独部署 WeRSS 并授权")
            old_reports = set(OUTPUTS.glob("history_backfill_*.json"))
            command = [str(rss_root/".venv/Scripts/python.exe"), "-u", str(rss_root/"tools/capture_baijiu.py"),
                       *date_args, "--collect-only"]
            for url in urls:
                command += ["--seed-url", url]
            child = subprocess.run(command, cwd=rss_root, capture_output=True, text=True, encoding="utf-8",
                                   env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=900,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            STATE["log"] += child.stdout + child.stderr
            if child.returncode != 0:
                raise ValueError("采集流程未完成，见运行日志；已有数据库未覆盖")
            new_reports = sorted(set(OUTPUTS.glob("history_backfill_*.json"))-old_reports)
            if new_reports:
                history_report = new_reports[-1]
        if mode in {"rpa", "rpa_links"}:
            set_progress(5, "请确认微信已登录，采集时不要关闭搜一搜窗口")
            rpa_root = ROOT / "wechat-article-rpa"
            run_dir = OUTPUTS / ("rpa-run-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            urls = [x.strip() for x in str(payload.get("seed_urls", "")).splitlines() if x.strip()]
            if len(urls) > 20:
                raise ValueError("单次最多补录20个原文链接")
            if mode == "rpa_links" and not urls:
                raise ValueError("请填写需要解析的微信原文链接")
            rpa_python = rpa_root / ".venv/Scripts/python.exe"
            command = [str(rpa_python) if rpa_python.is_file() else sys.executable, str(rpa_root / "baijiu_bridge.py"),
                       *date_args, "--output-dir", str(run_dir), "--collect-only"]
            if mode == "rpa":
                command.append("--live")
            for url in urls:
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"} or parsed.hostname != "mp.weixin.qq.com" or parsed.username or parsed.password or parsed.port not in {None,80,443}:
                    raise ValueError("只接受微信原文链接")
                command.extend(["--url", url])
            job_boundary()
            child = stream_child(command, rpa_root, 3600)
            if child.returncode:
                reasons = re.findall(r"(?:RuntimeError|ValueError): ([^\n]+)", child.stdout)
                raise ValueError(reasons[-1] if reasons else "RPA采集/解析未完成，见日志；未更新Excel")
            rpa_report = run_dir / "bridge_report.json"
            data = json.loads(rpa_report.read_text(encoding="utf-8"))
            rpa_inputs = Path(data["inputs"])
            shared_report = OUTPUTS / (run_dir.name + "-report.json")
            shared_report.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            rpa_report = shared_report
        begin_write_phase()
        set_progress(90, "正在按模板更新 Excel 并生成复核记录")
        sample = mode == "sample"
        input_dir = rpa_inputs or BASE / ("examples" if sample else "article_inputs/WeRSS" if mode == "history" else "article_inputs")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = (f"样例验证_非真实行情_{stamp}.xlsx" if sample
                else f"白酒价格数据库_{start:%Y%m%d}_{end:%Y%m%d}_{stamp}.xlsx")
        output = OUTPUTS / name
        args = date_args + ["--workbook", str(workbook), "--articles-dir", str(input_dir), "--output", str(output)]
        if mode == "dry":
            args.append("--dry-run")
        before = set(OUTPUTS.glob("*.csv"))
        proc = run_child("update_baijiu_prices.py", args)
        set_progress(96, "正在核对数据库写入结果")
        new_csv = set(OUTPUTS.glob("*.csv")) - before
        matches = next((x for x in new_csv if x.name.startswith("matches_")), None)
        missing = next((x for x in new_csv if x.name.startswith("missing_review_")), None)
        rows, gaps = csv_rows(matches), csv_rows(missing)
        unique = {(r["date"], r["account"], r["product"]) for r in rows}
        expected = sum(len(v["aliases"]) for v in json.loads((BASE/"config/baijiu_price_config.json").read_text(encoding="utf-8"))["products"].values()) * ((end-start).days+1)
        result = {"mode": mode, "start": start.isoformat(), "end": end.isoformat(),
                  "prices": len(unique), "cells": len(rows), "missing": len(gaps), "expected": expected,
                  "excel": name if output.exists() and mode != "dry" else None,
                  "files": [x.name for x in [matches, missing, Path(fetch_report) if fetch_report else None, history_report, rpa_report] if x],
                  "preview": [next(r for r in rows if (r['date'],r['account'],r['product']) == key) for key in sorted(unique, reverse=True)], "gaps": gaps[:80],
                  "sample": sample, "exit_code": proc.returncode}
        config = json.loads((BASE/'config/baijiu_price_config.json').read_text(encoding='utf-8'))
        result['brand_quotes'] = [{'brand':brand, 'quotes':sum(config['products'][key[2]]['sheet'] == brand for key in unique)} for brand in ('茅台','五粮液','泸州老窖','习酒')]
        if history_report:
            history = json.loads(history_report.read_text(encoding="utf-8"))
            result["article_coverage"] = history["coverage"]
            result["history_sources"] = history["sources"]
            result["history_errors"] = len(history["errors"])
        if rpa_report:
            rpa = json.loads(rpa_report.read_text(encoding="utf-8"))
            result["rpa_articles"] = rpa["articles"]
            result["rpa_review"] = rpa["review"]
            result["collection_incomplete"] = rpa.get("collection_incomplete", False)
            result["collection_errors"] = rpa.get("collection_errors", [])
        result["status"] = ("样例验证通过（模拟价格）" if sample and proc.returncode == 0
                            else "无可用价格，未生成 Excel" if not rows
                            else "试运行完成，未写 Excel" if mode == "dry"
                            else "已生成数据库，有未取得报价待复核" if gaps
                            else "更新完成")
        if proc.returncode not in {0, 2}:
            raise RuntimeError("数据库更新未完成，请查看日志")
        STATE["result"] = result
        if result["excel"] and not sample:
            DatabaseImports(BASE).clear_active()
        STATE["message"] = result["status"]
        if result.get("collection_incomplete") and result["excel"]:
            STATE["message"] = result["status"] = "部分更新完成；有公众号采集失败，请补录或重试"
        STATE["progress"] = 100
        (OUTPUTS/f"run_summary_{stamp}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),encoding="utf-8")
    except Exception as exc:
        STATE["message"] = f"运行失败：{exc}"
        STATE["log"] += f"\n{type(exc).__name__}: {exc}"
    finally:
        with LOCK:
            STATE.update(running=False, paused=False, pause_requested=False, pausable=False)
            if CONTROL_ENV.get("RPA_PAUSE_FILE"):
                Path(CONTROL_ENV["RPA_PAUSE_FILE"]).unlink(missing_ok=True)
            CONTROL_ENV.clear()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def reply(self, data, code=200, kind="application/json; charset=utf-8", filename=None):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            from urllib.parse import quote
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/":
            self.reply((BASE/"ui.html").read_bytes(),kind="text/html; charset=utf-8")
        elif route in {"/ui.css", "/ui.js"}:
            kind = "text/css; charset=utf-8" if route.endswith(".css") else "text/javascript; charset=utf-8"
            self.reply((BASE / route[1:]).read_bytes(), kind=kind)
        elif route == "/api/status":
            self.reply(STATE)
        elif route == "/api/overview":
            self.reply(latest_overview())
        elif route == "/api/config":
            self.reply({**database_config(),
                        "start": (today()-timedelta(days=6)).isoformat(), "end": today().isoformat(),
                        "inputs": len([x for x in (BASE/"article_inputs").rglob("*") if x.suffix.lower() in {".txt",".html",".htm",".csv"}]),
                        "accounts": json.loads((BASE/"config/baijiu_price_config.json").read_text(encoding="utf-8"))["accounts"]})
        elif route.startswith("/download/"):
            name = unquote(route.removeprefix("/download/"))
            path = (OUTPUTS/name).resolve()
            if path.parent == OUTPUTS.resolve() and not path.is_file() and path.suffix == ".xlsx":
                archived = (OUTPUTS / "archive" / "databases" / name).resolve()
                if archived.parent == (OUTPUTS / "archive" / "databases").resolve():
                    path = archived
            if path.parent not in {OUTPUTS.resolve(), (OUTPUTS / "archive" / "databases").resolve()} or not path.is_file() or path.suffix not in {".xlsx",".csv",".json"}:
                self.reply({"error":"文件不存在"},404)
            else:
                kind = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if path.suffix == ".xlsx" else "application/octet-stream"
                self.reply(path.read_bytes(),kind=kind,filename=path.name)
        else:
            self.reply({"error":"未找到"},404)
    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}:
            self.reply({"error":"请通过本机界面提交操作"},403)
            return
        try:
            length = int(self.headers.get("Content-Length",0))
            limit = 70_000_000 if self.path == "/api/database" else 20_000_000
            if length > limit:
                raise ValueError("数据库须不超过 50MB" if self.path == "/api/database" else "单次上传不能超过 20MB")
            payload = json.loads(self.rfile.read(length))
            if self.path == "/api/run":
                with LOCK:
                    if STATE["running"]:
                        self.reply({"error":"已有任务正在运行"},409)
                        return
                    job_id = uuid.uuid4().hex
                    CONTROL_ENV["RPA_PAUSE_FILE"] = str(OUTPUTS / ("pause-" + job_id))
                    STATE.update(running=True,message="正在启动",result=None,log="",progress=0,
                                 paused=False, pause_requested=False, pausable=True,
                                 job_id=job_id, started_at=datetime.now().isoformat(),
                                 mode=payload.get("mode", "local"), accounts_total=0, accounts_completed=0)
                    threading.Thread(target=run_job,args=(payload,),daemon=True).start()
                self.reply({"started":True})
            elif self.path == "/api/pause":
                request_pause(payload.get("job_id"))
                self.reply({"pause_requested": True})
            elif self.path == "/api/resume":
                request_resume(payload.get("job_id"))
                self.reply({"resumed": True})
            elif self.path == "/api/database":
                with LOCK:
                    if STATE["running"]:
                        self.reply({"error":"任务运行中，请在完成后导入数据库"},409)
                        return
                    config = json.loads((BASE/"config/baijiu_price_config.json").read_text(encoding="utf-8"))
                    self.reply(DatabaseImports(BASE).import_file(payload, config))
            elif self.path == "/api/upload":
                names = []
                for item in payload.get("files",[]):
                    name = Path(item["name"]).name
                    if any(ch in name for ch in '<>:"/\\|?*') or Path(name).suffix.lower() not in {".txt",".html",".htm",".csv"}:
                        raise ValueError("只支持带日期和公众号名称的 TXT/HTML/CSV 文件")
                    path = BASE/"article_inputs"/name
                    if path.exists():
                        raise ValueError(f"文件已存在：{name}。请在 article_inputs 文件夹中核对后替换。")
                    path.write_bytes(base64.b64decode(item["data"],validate=True))
                    names.append(name)
                self.reply({"saved":names})
            else:
                self.reply({"error":"未找到"},404)
        except Exception as exc:
            self.reply({"error":str(exc)},400)

def main():
    global PORT
    parser = argparse.ArgumentParser(description="白酒数据库本机可视化工具")
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--no-browser",action="store_true")
    parser.add_argument("--cli",choices=["local","auto","dry","sample","history","rpa","rpa_links"])
    parser.add_argument("--workbook",default="名酒酒价各平台数据库20260516.xlsx")
    parser.add_argument("--start",default=(today()-timedelta(days=6)).isoformat())
    parser.add_argument("--end",default=today().isoformat())
    args=parser.parse_args()
    OUTPUTS.mkdir(exist_ok=True)
    if not args.cli:
        DatabaseVersions(OUTPUTS).organize()
    if args.cli:
        STATE["running"]=True
        run_job(vars(args)|{"mode":args.cli})
        print(STATE["log"])
        print(STATE["message"])
        if STATE["result"]:
            print(json.dumps(STATE["result"],ensure_ascii=False,indent=2))
        return 0 if STATE["result"] and STATE["result"]["prices"] else 2
    PORT=args.port
    url=f"http://127.0.0.1:{PORT}"
    try:
        server=ThreadingHTTPServer(("127.0.0.1",PORT),Handler)
    except OSError:
        print(f"端口 {PORT} 已占用。如果工具已启动，请打开 {url}。否则用 --port 8766。")
        return 1
    if not args.no_browser:
        webbrowser.open(url)
    print(f"本机界面：{url}，关闭本窗口即可停止。",flush=True)
    server.serve_forever()
    return 0

if __name__ == "__main__":
    sys.exit(main())


