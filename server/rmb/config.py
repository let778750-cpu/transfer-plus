from __future__ import annotations
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlparse

DEFAULTS = {
    "provider": "zhipu", "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
    "model": "glm-5.3-flash", "targetLanguage": "zh-CN",
    "qps": 4, "minFontScale": 0.85, "glossary": "",
    "pipeline": "babeldoc-engine-v1",
}

def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
        # Windows readers/virus scanners can briefly hold a destination without
        # FILE_SHARE_DELETE. Retry the atomic replace, never truncate in place.
        for attempt in range(40):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(.025)
    finally:
        if os.path.exists(name):
            os.unlink(name)

class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

def protect_secret(value: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Encrypted credentials require Windows DPAPI")
    buf = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    api = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not api(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(dest.pbData, dest.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dest.pbData)

class ConfigStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "config" / "settings.json"
        self.secret_path = self.data_dir / "config" / "credential.dpapi"
        self.ocr_secret_path = self.data_dir / "config" / "paddle-credential.dpapi"

    def read(self):
        result = dict(DEFAULTS)
        if self.path.exists():
            result.update(json.loads(self.path.read_text(encoding="utf-8")))
        result["hasApiKey"] = self.secret_path.exists()
        result["hasPaddleApiKey"] = self.ocr_secret_path.exists()
        return result

    def ocr_secret(self):
        if not self.ocr_secret_path.exists():
            raise ValueError("请在 Zotero 工具 → 中文对照设置中填写 AI Studio 访问令牌")
        return protect_secret(base64.b64decode(self.ocr_secret_path.read_bytes()), decrypt=True).decode("utf-8")

    def secret(self):
        if not self.secret_path.exists():
            raise ValueError("请先在中文对照设置中配置 API 密钥")
        return protect_secret(base64.b64decode(self.secret_path.read_bytes()), decrypt=True).decode("utf-8")

    def update(self, value):
        result = self.read()
        for key in DEFAULTS:
            if key in value:
                result[key] = value[key]
        result.pop("hasApiKey", None)
        result.pop("hasPaddleApiKey", None)
        if result.get("pipeline") != "babeldoc-engine-v1":
            result["pipeline"] = "babeldoc-engine-v1"
        url = urlparse(str(result["baseUrl"]))
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query:
            raise ValueError("模型接口必须是无凭据和查询参数的 HTTPS 地址")
        if not isinstance(result["model"], str) or not result["model"].strip():
            raise ValueError("模型名称不能为空")
        result["qps"] = int(result["qps"])
        result["minFontScale"] = float(result["minFontScale"])
        if not 1 <= result['qps'] <= 8 or not math.isfinite(result['minFontScale']) or not .85 <= result['minFontScale'] <= 1:
            raise ValueError('并发速率应为 1–8，最小字号比例应为 0.85–1')
        if not isinstance(result['glossary'],str) or len(result['glossary']) > 100000:
            raise ValueError('术语表应为不超过十万字符的文本')
        if result["targetLanguage"] != "zh-CN":
            raise ValueError("首版仅支持简体中文")
        result["baseUrl"] = result["baseUrl"].rstrip("/")
        if "apiKey" in value and value["apiKey"]:
            encrypted = base64.b64encode(protect_secret(str(value["apiKey"]).strip().encode()))
            self.secret_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.secret_path.with_suffix(".tmp")
            temporary.write_bytes(encrypted)
            os.replace(temporary, self.secret_path)
        if value.get('paddleApiKey'):
            encrypted = base64.b64encode(protect_secret(str(value['paddleApiKey']).strip().encode()))
            self.ocr_secret_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.ocr_secret_path.with_suffix('.tmp')
            temporary.write_bytes(encrypted)
            os.replace(temporary, self.ocr_secret_path)
        atomic_json(self.path, result)
        return self.read()

def config_fingerprint(config):
    public = {k: config[k] for k in DEFAULTS}
    public["pipeline"] = "rmb-0.4.0-babeldoc-engine-v1"
    return hashlib.sha256(json.dumps(public, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def translation_fingerprint(config):
    public={k:config[k] for k in ('provider','baseUrl','model','targetLanguage','glossary')}
    public['prompt']='rmb-academic-math-tokens-v1'
    return hashlib.sha256(json.dumps(public,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def sha256_file(path: Path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for part in iter(lambda: f.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()
