"""0.4 pipeline: BabelDOC local engine (ONNX layout) + guarded GLM transport.

Replaces the cloud-recognition + hand-rolled composition path. The engine owns
layout analysis, formula/vector protection and typesetting; this module owns
credentials, pacing, validation, cancellation, verification and the report
contract the plugin consumes. One run yields one complete PDF or one explicit
error — there is no partially-synced intermediate state.
"""
from __future__ import annotations
from collections import Counter
import re
import shutil
import threading
import time
from pathlib import Path

from .config import atomic_json, config_fingerprint, sha256_file

# BabelDOC renders formulas and protected spans as {{N}} placeholders in the
# text handed to the translator; the multiset must survive translation.
PLACEHOLDER = re.compile(r"\{\{\s*\d+\s*\}\}")

PIPELINE = "babeldoc-engine-v1"


class Cancelled(RuntimeError):
    user_message = "已取消，进度已保留；再次点击翻译即可继续"
    resumable = True


class ScannedDocument(RuntimeError):
    user_message = "检测为扫描件（无文字层），本版本暂不支持；已停止，未调用翻译接口"
    resumable = False


def validate_output(source, output):
    if not isinstance(output, str) or not output.strip():
        return "empty_translation"
    if Counter(PLACEHOLDER.findall(source)) != Counter(PLACEHOLDER.findall(output)):
        return "placeholder_changed"
    if len(output) > max(200, len(source) * 6):
        return "unexpected_expansion"
    if not any("一" <= ch <= "鿿" for ch in output) and any(
        "a" <= ch.lower() <= "z" for ch in source
    ):
        return "not_translated"
    return None


def make_translator(config, secret, check=lambda: None):
    """Build a BabelDOC BaseTranslator bound to the guarded GLM transport.

    Pacing, quota latch (code 1308) and placeholder validation reuse the
    discipline proven in 0.3. A paragraph that still fails validation after a
    corrective retry stays in its original language instead of poisoning
    formulas, and is counted in the report.
    """
    from babeldoc.translator.translator import BaseTranslator

    class GlmTranslator(BaseTranslator):
        name = "rmb-glm"

        def __init__(self):
            super().__init__("English", "Simplified Chinese", ignore_cache=False)
            self.add_cache_impact_parameters("model", config["model"])
            self.add_cache_impact_parameters("engine", PIPELINE)
            self.model = config["model"]
            self.client = self._client()
            self.qps = float(config.get("qps", 4))
            self.glossary = str(config.get("glossary") or "")
            self.lock = threading.Lock()
            self.next_call = 0.0
            self.quota_error = None
            self.check = check
            self.kept_original = []
            self.metrics = {"requests": 0, "requestSeconds": 0.0}

        def _client(self):
            from openai import OpenAI

            return OpenAI(base_url=config["baseUrl"], api_key=secret).with_options(
                timeout=45, max_retries=1
            )

        def do_llm_translate(self, text, rate_limit_params=None):
            # Satisfies the 0.7.1 ABC surface; the guarded path is do_translate.
            return self.do_translate(text, rate_limit_params)

        def do_translate(self, text, rate_limit_params=None):
            if text is None:
                return None  # engine capability probe contract (probe_il_translator)
            # Rich-text capability probe from the rust core poses as an
            # instruction-like paragraph; translate best-effort, never record it.
            probe = isinstance(text, str) and text.startswith("You are a professional")
            if self.quota_error:
                raise self.quota_error
            self.check()
            with self.lock:
                delay = max(0.0, self.next_call - time.monotonic())
                self.next_call = max(time.monotonic(), self.next_call) + 1.0 / self.qps
            end = time.monotonic() + delay
            while time.monotonic() < end:
                if self.quota_error:
                    raise self.quota_error
                self.check()
                time.sleep(min(0.1, max(0.0, end - time.monotonic())))
            self.check()
            raw, problem = self._request(text, corrective=False)
            if problem and not probe:
                raw2, problem2 = self._request(text, corrective=True)
                if not problem2:
                    return raw2
                self.kept_original.append({"reason": problem2, "excerpt": text[:80]})
                return text  # keep original paragraph; layout stays intact
            return raw if not problem else (raw or text)

        def _request(self, text, corrective):
            from .translator import ModelQuotaExceeded

            system = (
                "专业学术翻译引擎。将提供的英文文本完整翻译为简体中文，只输出译文本身。"
                "保留全部 {{N}} 占位符、数字、标识符与代码记号，不增不删不解释。"
                + (f"术语对照：{self.glossary}" if self.glossary else "")
                + ("上次输出未通过校验；请完整翻译并逐字保留 {{N}} 占位符。" if corrective else "")
            )
            started = time.monotonic()
            raw, err = "", None
            with self.lock:
                self.metrics["requests"] += 1
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0,
                    max_tokens=8192,
                    extra_body={"thinking": {"type": "disabled"}},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": text},
                    ],
                )
                raw = (response.choices[0].message.content or "").strip()
                if response.choices[0].finish_reason != "stop":
                    raise ValueError("incomplete_model_response")
            except Exception as exc:
                body = getattr(exc, "body", {}) or {}
                detail = body.get("error", body) if isinstance(body, dict) else {}
                if str(detail.get("code")) == "1308":
                    match = re.search(
                        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(detail.get("message", ""))
                    )
                    self.quota_error = ModelQuotaExceeded(match.group() if match else "")
                    raise self.quota_error from None
                err = "model_request_failed:" + type(exc).__name__
            finally:
                with self.lock:
                    self.metrics["requestSeconds"] += time.monotonic() - started
            return raw, err or validate_output(text, raw)

    return GlmTranslator()


def _has_text_layer(path: Path) -> bool:
    import pymupdf

    with pymupdf.open(path) as doc:
        sample = "".join(doc[n].get_text() for n in range(min(6, len(doc))))
    return len(sample.split()) >= 40


def run_pipeline(source, job, data_dir, state, config, store, progress, check):
    """Execute one document end to end; write report.json + result.pdf."""
    import pymupdf

    with pymupdf.open(source) as doc:
        n_pages = doc.page_count
        page_sizes = [(round(p.rect.width, 1), round(p.rect.height, 1)) for p in doc]
        source_words = len(" ".join(p.get_text() for p in doc).split())
    if not _has_text_layer(source):
        raise ScannedDocument()

    from babeldoc.docvision.base_doclayout import DocLayoutModel
    from babeldoc.format.pdf.high_level import translate as babeldoc_translate
    from babeldoc.format.pdf.translation_config import TranslationConfig

    translator = make_translator(config, store.secret(), check=check)
    stop = threading.Event()
    progress("正在加载本地版面模型", 8, "parsing")
    layout_model = DocLayoutModel.load_available()

    def watch():
        while not stop.is_set():
            done = translator.metrics["requests"]
            hits = translator.translate_cache_call_count
            progress(f"翻译进行中 · 新翻译 {done} 段 / 缓存复用 {hits} 段", 55, "translating")
            stop.wait(2.0)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    t0 = time.monotonic()
    try:
        out_dir = job / "engine-out"
        out_dir.mkdir(exist_ok=True)
        engine_config = TranslationConfig(
            translator=translator,
            input_file=str(source),
            lang_in="English",
            lang_out="Simplified Chinese",
            doc_layout_model=layout_model,
            output_dir=str(out_dir),
            qps=int(config.get("qps", 4)),
            pool_max_workers=4,
            term_pool_max_workers=2,
            auto_extract_glossary=False,
        )
        result = babeldoc_translate(engine_config)
        mono = Path(result.mono_pdf_path) if result.mono_pdf_path else None
        if mono is None or not mono.exists():
            raise RuntimeError("engine_no_output")
    finally:
        stop.set()

    progress("正在核验产物", 92, "verifying")
    with pymupdf.open(mono) as out_doc:
        if out_doc.page_count != n_pages:
            raise RuntimeError(f"page_count_mismatch:{out_doc.page_count}!={n_pages}")
        for i, page in enumerate(out_doc.pages()):
            if (round(page.rect.width, 1), round(page.rect.height, 1)) != page_sizes[i]:
                raise RuntimeError(f"page_size_changed:{i + 1}")
        cjk_pages = sum(
            1 for p in out_doc.pages() if any("一" <= ch <= "鿿" for ch in p.get_text())
        )
        if cjk_pages < max(1, int(n_pages * 0.6)):
            raise RuntimeError(f"translation_missing:{cjk_pages}/{n_pages}")

    shutil.copy2(mono, job / "result.pdf")
    output_hash = sha256_file(job / "result.pdf")
    shutil.rmtree(out_dir, ignore_errors=True)

    seconds = round(time.monotonic() - t0, 1)
    kept = len(translator.kept_original)
    issues = []
    if kept:
        issues.append({
            "page": 1,
            "types": ["paragraph_kept_original"],
            "type": "paragraph_kept_original",
            "reason": translator.kept_original[0]["reason"],
            "count": kept,
            "excerpt": translator.kept_original[0]["excerpt"],
        })
    report = {
        "schema": 2,
        "pipeline": PIPELINE,
        "sourceHash": state["sourceHash"],
        "configFingerprint": config_fingerprint(config),
        "outputHash": output_hash,
        "status": "completed_with_warnings" if kept else "completed",
        "pages": n_pages,
        "issues": issues,
        "completionRevision": 0,
        "message": ("翻译完成" + (f"，{kept} 个段落保留原文待核对" if kept else "")),
        "coverage": {
            "total": max(1, source_words),
            "pdf": max(1, source_words),
            "missing": 0,
            "keptOriginalParagraphs": kept,
        },
        "timings": {
            "seconds": {"engine": seconds},
            "translation": {
                "requests": translator.metrics["requests"],
                "requestSeconds": round(translator.metrics["requestSeconds"], 1),
                "cacheHits": translator.translate_cache_call_count,
            },
        },
    }
    atomic_json(job / "report.json", report)
    return report
