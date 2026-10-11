"""Cooperative pause at safe boundaries; resume keeps the same process and cursor."""
import json
import os
import time
from pathlib import Path


def wait_if_paused():
    name = os.environ.get("RPA_PAUSE_FILE")
    if not name:
        return 0.0
    path = Path(name)
    if not path.exists():
        return 0.0
    started = time.monotonic()
    print('RPA_CONTROL ' + json.dumps({"event": "paused"}), flush=True)
    while path.exists():
        time.sleep(0.1)
    elapsed = time.monotonic() - started
    print('RPA_CONTROL ' + json.dumps({"event": "resumed", "seconds": elapsed}), flush=True)
    return elapsed
