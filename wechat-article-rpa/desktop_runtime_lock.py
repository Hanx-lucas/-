"""Windows 进程锁：界面、命令行和定时任务只能有一个桌面采集器。"""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import msvcrt
import sys


@contextmanager
def desktop_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as error:
        handle.close()
        raise RuntimeError("已有微信桌面采集正在运行，请等待该任务结束") from error
    try:
        yield
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def single_desktop_collector(function):
    @wraps(function)
    def run(*args, **kwargs):
        if "--live" not in sys.argv:
            return function(*args, **kwargs)
        with desktop_lock(Path(__file__).resolve().parent / "output/desktop-collector.lock"):
            return function(*args, **kwargs)
    return run
