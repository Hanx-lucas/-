"""Portable UI entry point; the installer creates a shared environment beside both apps."""
from pathlib import Path
import os,subprocess,sys

base=Path(__file__).resolve().parent
candidates=[base.parent/'.venv/Scripts/python.exe',base/'.venv/Scripts/python.exe',Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe']
python=next((p for p in candidates if p.is_file()),Path(sys.executable))
result=subprocess.run([str(python),str(base/'ui_server.py'),*sys.argv[1:]],cwd=base,env=dict(os.environ,PYTHONIOENCODING='utf-8'))
raise SystemExit(result.returncode)
