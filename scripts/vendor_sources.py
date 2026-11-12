"""Fetch immutable upstream source archives; never copy credentials or user PDFs."""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile
import time
import concurrent.futures

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ("Liozhang/z-transplit", "v1.20.0", "490612cba42e39eb8167b595a19b22a0f16b4114", "plugin", ""),
    ("guaguastandup/zotero-pdf2zh", "v4.1.8", "5dd2eaf019f71266764c269bbd2104b559ae95cf", "server/upstream", "server/"),
    ("funstory-ai/BabelDOC", "v0.7.1", "d922e2b0c5a353bde8731921eb442c07ae75849d", "engine-patches/upstream", ""),
]

def wanted(repo, path):
    if repo.endswith("z-transplit"):
        return (path.startswith(("src/", "addon/", "typings/", "tests/node/")) or
                path in ("package.json", "package-lock.json", "tsconfig.json", "README.md", "LICENSE")) and not path.endswith((".jar", ".png", ".jpg", ".gif", ".webp"))
    if repo.endswith("zotero-pdf2zh"):
        return path in ("server/utils/task_manager.py", "LICENSE")
    return path.startswith(("babeldoc/", "rust/")) or path in ("LICENSE", "README.md", "pyproject.toml", "Cargo.lock", "Cargo.toml")

def main():
    records = []
    for repo, version, commit, destination, prefix in SOURCES:
        target = ROOT / destination
        url = f"https://api.github.com/repos/{repo}/git/trees/{commit}?recursive=1"
        req = urllib.request.Request(url, headers={"User-Agent": "ResearchMaxBilingual-build/0.1"})
        tree = json.load(urllib.request.urlopen(req, timeout=45))
        files = [x for x in tree["tree"] if x["type"] == "blob" and wanted(repo, x["path"])]
        def fetch(member):
            relative = member["path"]
            existing = target / relative[len(prefix):] if relative.startswith(prefix) else None
            if existing and existing.is_file():
                old = existing.read_bytes()
                if hashlib.sha1(b"blob " + str(len(old)).encode() + b"\0" + old).hexdigest() == member["sha"]:
                    return
                raise RuntimeError("Refusing to overwrite modified source: " + str(existing))
            raw = f"https://raw.githubusercontent.com/{repo}/{commit}/{relative}"
            for attempt in range(4):
                try:
                    contents = urllib.request.urlopen(raw, timeout=45).read()
                    break
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(1)
            # Git blob SHA verifies every raw response against the immutable tree.
            digest = hashlib.sha1(b"blob " + str(len(contents)).encode() + b"\0" + contents).hexdigest()
            if digest != member["sha"]:
                raise RuntimeError("Git blob mismatch: " + relative)
            if relative in ("LICENSE", "NOTICE.md"):
                lic = ROOT / "licenses" / (repo.split("/")[-1] + "-" + relative)
                lic.parent.mkdir(parents=True, exist_ok=True)
                lic.write_bytes(contents)
            if not relative.startswith(prefix):
                return
            relative = relative[len(prefix):]
            if not relative or relative.startswith(".github/"):
                return
            output = (target / relative).resolve()
            if not output.is_relative_to(target.resolve()):
                raise RuntimeError("Unsafe archive path")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(contents)
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(fetch, files))
        count = len(files)
        records.append({"repository": repo, "version": version, "commit": commit,
                        "tree_sha": tree["sha"], "blob_verification": "git-sha1",
                        "destination": destination, "files": count})
        print(json.dumps(records[-1]), flush=True)
    (ROOT / "upstream-lock.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__":
    main()
