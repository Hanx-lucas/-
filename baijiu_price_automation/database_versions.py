"""Four visible database checkpoints; older workbooks remain recoverable in archive."""
from __future__ import annotations

import calendar
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ZONE = timezone(timedelta(hours=8))


def month_before(day):
    year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def formal_name(name):
    return name.endswith(".xlsx") and name.startswith(("白酒价格数据库_", "名酒酒价各平台数据库"))


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 20
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("数据库版本目录正在使用，请稍后重试")
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class DatabaseVersions:
    def __init__(self, directory, now=None):
        self.directory = Path(directory).resolve()
        self.archive = self.directory / "archive" / "databases"
        self.manifest = self.directory / "database_versions.json"
        self.now = now

    def _now(self):
        return self.now or datetime.now(ZONE)

    def _path(self, name):
        if Path(name).name != name or any(c in name for c in '/\\:'):
            raise ValueError("数据库文件名无效")
        for directory in (self.directory, self.archive):
            path = (directory / name).resolve()
            if path.parent == directory.resolve() and path.is_file():
                return path
        return None

    def _read(self):
        bootstrap = False
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"version": 1, "records": []}
            bootstrap = True
        known = {r["name"]: r for r in data["records"] if self._path(r["name"])}
        # Bootstrap the existing successful outputs. Samples and temporary files are excluded.
        for directory in ((self.directory, self.archive) if bootstrap else ()):
            for path in directory.glob("*.xlsx"):
                if formal_name(path.name) and path.name not in known:
                    match = re.search(r"_(20\d{6})_(\d{6})(?:_\d{6})?\.xlsx$", path.name)
                    created = (datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=ZONE)
                               if match else datetime.fromtimestamp(path.stat().st_mtime, ZONE))
                    known[path.name] = {"name": path.name, "created_at": created.isoformat(), "source": "", "start": "", "end": ""}
        return sorted(known.values(), key=lambda r: (r["created_at"], r["name"]), reverse=True)

    def _slots(self, records):
        day = self._now().astimezone(ZONE).date()
        slots = []
        for role, label, target in [("latest", "最新数据库", None), ("previous", "上次数据库", None),
                                     ("day15", "15天前快照", day - timedelta(days=15)),
                                     ("month1", "1个月前快照", month_before(day))]:
            if target is None:
                index = 0 if role == "latest" else 1
                record = records[index] if len(records) > index else None
            else:
                record = next((r for r in records if datetime.fromisoformat(r["created_at"]).astimezone(ZONE).date() <= target), None)
            path = self._path(record["name"]) if record else None
            slots.append({"role": role, "label": label, "target_date": str(target) if target else None,
                          "available": bool(path), "name": record["name"] if record else None,
                          "created_at": record["created_at"] if record else None,
                          "path": str(path) if path else None})
        return slots

    def view(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with file_lock(self.directory / ".database_versions.lock"):
            records = self._read()
            return {"slots": self._slots(records), "archive": str(self.archive), "total_versions": len(records)}

    def publish(self, path, *, source=None, start="", end=""):
        path = Path(path).resolve()
        if path.parent != self.directory or not path.is_file() or not formal_name(path.name):
            raise ValueError("只能登记正式输出目录中的数据库副本")
        with file_lock(self.directory / ".database_versions.lock"):
            records = self._read()
            item = next((r for r in records if r["name"] == path.name), None)
            if item is None:
                item = {"name": path.name, "created_at": self._now().isoformat()}
                records.append(item)
            item.update(source=str(Path(source).resolve()) if source else "", start=start, end=end)
            records.sort(key=lambda r: (r["created_at"], r["name"]), reverse=True)
            return self._organize(records)

    def organize(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with file_lock(self.directory / ".database_versions.lock"):
            return self._organize(self._read())

    def _organize(self, records):
        slots = self._slots(records)
        keep = {s["name"] for s in slots if s["available"]}
        deferred = []
        self.archive.mkdir(parents=True, exist_ok=True)
        # Validate each resolved source and destination before reversible, individual moves.
        for name in keep:
            source = self._path(name)
            destination = (self.directory / name).resolve()
            if source.parent == self.archive.resolve() and destination.parent == self.directory:
                if destination.exists():
                    raise RuntimeError("版本目录存在同名文件，已停止整理")
                try:
                    source.rename(destination)
                except PermissionError:
                    deferred.append(source.name)
        for path in list(self.directory.glob("*.xlsx")):
            source = path.resolve()
            if source.name.startswith("~$"):
                continue  # Excel's live lock file is not a database version.
            if source.name in keep or (formal_name(source.name) and source.name not in {r["name"] for r in records}):
                # An unregistered formal file may still be written by a running job.
                continue
            destination = (self.archive / source.name).resolve()
            if source.parent != self.directory or destination.parent != self.archive.resolve():
                raise ValueError("归档路径超出数据库目录")
            if destination.exists():
                if formal_name(source.name):
                    raise RuntimeError("归档存在同名数据库，已停止整理")
                destination = (self.archive / (source.stem + "__" + datetime.now(ZONE).strftime("%Y%m%d_%H%M%S_%f") + source.suffix)).resolve()
                if destination.parent != self.archive.resolve() or destination.exists():
                    raise RuntimeError("无法为临时文件创建独立归档名称")
            try:
                source.rename(destination)
            except PermissionError:
                deferred.append(source.name)
        data = {"version": 1, "updated_at": self._now().isoformat(), "records": records, "deferred_archive": deferred}
        temporary = self.manifest.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.manifest)
        return {"slots": self._slots(records), "archive": str(self.archive), "total_versions": len(records), "deferred_archive": deferred}
