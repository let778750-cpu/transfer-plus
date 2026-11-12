"""Copy maintained service code into an independent installed runtime."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from rmb.config import atomic_json
from rmb import VERSION

p = argparse.ArgumentParser()
p.add_argument("--runtime", type=Path, required=True)
p.add_argument("--data", type=Path)
p.add_argument("--register", action="store_true")
args = p.parse_args()
version = args.runtime / "versions" / VERSION
if not (version / "python" / "python.exe").exists():
    raise SystemExit("Prepare the standalone Python runtime first")
shutil.copytree(ROOT / "server", version / "server", dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
shutil.copytree(ROOT / "licenses", version / "licenses", dirs_exist_ok=True)
data = args.data or args.runtime / "data"
for folder in ("config", "cache", "jobs", "reports", "logs", "glossary"):
    (data / folder).mkdir(parents=True, exist_ok=True)
active_path = args.runtime / "active.json"
previous = json.loads(active_path.read_text(encoding="utf-8")).get("version") if active_path.exists() else None
active = {"version": VERSION}
if previous and previous != active["version"]: active["previousVersion"] = previous
atomic_json(active_path, active)
if args.register:
    atomic_json(Path(os.environ["LOCALAPPDATA"]) / "ResearchMaxBilingual" / "install.json",
                {"root": str(args.runtime), "data": str(data)})
print(json.dumps({"runtime": str(version), "registered": args.register}))
