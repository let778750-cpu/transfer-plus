"""0.4 babeldoc-engine pipeline: validator, scanned fast-fail, report contract,
cache-hit submit path. The real engine is faked via sys.modules injection so
these tests stay offline and need no babeldoc installation."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from rmb.babeldoc_flow import validate_output, PLACEHOLDER  # noqa: E402


class ValidatorTests(unittest.TestCase):
    def test_placeholder_multiset_must_survive(self):
        src = "the {{1}} value of {{2}} equals {{1}}"
        self.assertIsNone(validate_output(src, "该 {{1}} 的值 {{2}} 等于 {{1}}"))
        self.assertEqual(validate_output(src, "该 {{1}} 的值 {{2}} 等于 {{3}}"), "placeholder_changed")
        self.assertEqual(validate_output(src, "该值的描述"), "placeholder_changed")

    def test_empty_and_expansion(self):
        self.assertEqual(validate_output("hello", "  "), "empty_translation")
        self.assertEqual(validate_output("hi there", "x" * 900), "unexpected_expansion")

    def test_english_source_requires_chinese_output(self):
        self.assertEqual(validate_output("scheduling window", "scheduling window"), "not_translated")
        self.assertIsNone(validate_output("scheduling window", "调度窗口"))

    def test_no_english_letters_skip_language_check(self):
        self.assertIsNone(validate_output("1234 5678", "1234 5678"))


def make_sample_pdf(path: Path, pages=1, chinese=False):
    import pymupdf

    english = (
        "The scheduling window selection method accounts for resource constraints "
        "and task priorities across the observation horizon. "
    ) * 4
    chinese_text = "调度窗口选择方法在观测范围内综合考虑资源约束与任务优先级，并给出可执行的调度序列。" * 4
    with pymupdf.open() as doc:
        for _ in range(pages):
            page = doc.new_page()
            if chinese:
                page.insert_textbox(
                    pymupdf.Rect(40, 40, 540, 400), chinese_text,
                    fontname="china-s", fontsize=11,
                )
            else:
                page.insert_textbox(pymupdf.Rect(40, 40, 540, 400), english, fontsize=11)
        doc.save(path)


class FakeEngine:
    """Patches the babeldoc imports inside run_pipeline."""

    def __init__(self, output_pdf: Path):
        self.output_pdf = output_pdf

    def install(self):
        result = types.SimpleNamespace(mono_pdf_path=str(self.output_pdf))

        layout_mod = types.ModuleType("babeldoc.docvision.base_doclayout")
        layout_mod.DocLayoutModel = types.SimpleNamespace(load_available=lambda: object())
        high = types.ModuleType("babeldoc.format.pdf.high_level")
        high.translate = lambda config: result
        cfg = types.ModuleType("babeldoc.format.pdf.translation_config")
        cfg.TranslationConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
        translator_mod = types.ModuleType("babeldoc.translator.translator")

        class BaseTranslator:  # minimal stand-in for the real ABC
            def __init__(self, lang_in, lang_out, ignore_cache):
                self.translate_cache_call_count = 0
                self.name = "fake"

            def add_cache_impact_parameters(self, k, v):
                pass

        translator_mod.BaseTranslator = BaseTranslator
        for name, module in [
            ("babeldoc", types.ModuleType("babeldoc")),
            ("babeldoc.docvision", types.ModuleType("babeldoc.docvision")),
            ("babeldoc.docvision.base_doclayout", layout_mod),
            ("babeldoc.format", types.ModuleType("babeldoc.format")),
            ("babeldoc.format.pdf", types.ModuleType("babeldoc.format.pdf")),
            ("babeldoc.format.pdf.high_level", high),
            ("babeldoc.format.pdf.translation_config", cfg),
            ("babeldoc.translator", types.ModuleType("babeldoc.translator")),
            ("babeldoc.translator.translator", translator_mod),
        ]:
            sys.modules.setdefault(name, module)
        self._saved = None

    def __enter__(self):
        self._saved = {}
        for name in list(sys.modules):
            if name.startswith("babeldoc"):
                self._saved[name] = sys.modules.pop(name)
        self.install()
        return self

    def __exit__(self, *exc):
        for name in [n for n in list(sys.modules) if n.startswith("babeldoc")]:
            del sys.modules[name]
        sys.modules.update(self._saved or {})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.job = Path(self.tmp.name) / "job"
        self.job.mkdir()
        self.source = Path(self.tmp.name) / "sample.pdf"
        make_sample_pdf(self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        from rmb import babeldoc_flow
        from rmb.config import ConfigStore

        store = ConfigStore(Path(self.tmp.name))
        store.update({"apiKey": "test-only"})
        config = store.read()
        state = {"sourceHash": "0" * 64}
        return babeldoc_flow.run_pipeline(
            self.source, self.job, Path(self.tmp.name), state, config, store,
            progress=lambda *a: None, check=lambda: None,
        )

    def test_scanned_document_fails_fast_without_engine(self):
        import pymupdf

        with pymupdf.open() as doc:
            doc.new_page()
            doc.save(self.source)
        with self.assertRaises(babeldoc_scanned()):
            self._run()

    def test_report_contract_and_verification(self):
        translated = Path(self.tmp.name) / "translated.pdf"
        make_sample_pdf(translated, chinese=True)
        with FakeEngine(translated):
            report = self._run()
        self.assertEqual(report["pipeline"], "babeldoc-engine-v1")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["coverage"]["missing"], 0)
        self.assertEqual(report["issues"], [])
        self.assertTrue((self.job / "result.pdf").exists())
        self.assertTrue((self.job / "report.json").exists())
        self.assertFalse((self.job / "engine-out").exists())

    def test_page_count_mismatch_is_rejected(self):
        bad = Path(self.tmp.name) / "bad.pdf"
        make_sample_pdf(bad, pages=2, chinese=True)
        with FakeEngine(bad):
            with self.assertRaises(RuntimeError):
                self._run()


def babeldoc_scanned():
    from rmb.babeldoc_flow import ScannedDocument

    return ScannedDocument


class SubmitPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
        from rmb.service import JobService

        self.service = JobService(Path(self.tmp.name))

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def test_babeldoc_submit_queues_without_paddle_credential(self):
        import hashlib

        from rmb.config import config_fingerprint, sha256_file

        self.service.config.update({"apiKey": "test-only"})
        source = Path(self.tmp.name) / "s.pdf"
        make_sample_pdf(source)
        # Queue only; never start the real worker in tests.
        self.service.pool.submit = lambda *a: None
        state = self.service.submit(str(source), automatic=True)
        self.assertEqual(state["status"], "queued")
        fingerprint = config_fingerprint(self.service.config.read())
        expected = hashlib.sha256(
            (sha256_file(source) + fingerprint + json.dumps(None)).encode()
        ).hexdigest()
        self.assertEqual(state["cacheKey"], expected)
        self.assertNotIn("improveBase", state)

    def test_completed_archive_is_served_offline(self):
        import hashlib

        from rmb.config import atomic_json, config_fingerprint, sha256_file

        self.service.config.update({"apiKey": "test-only"})
        source = Path(self.tmp.name) / "s.pdf"
        make_sample_pdf(source)
        fingerprint = config_fingerprint(self.service.config.read())
        key = hashlib.sha256(
            (sha256_file(source) + fingerprint + json.dumps(None)).encode()
        ).hexdigest()
        cache = self.service.data / "cache" / key
        cache.mkdir(parents=True)
        translated = cache / "result.pdf"
        make_sample_pdf(translated, chinese=True)
        atomic_json(cache / "report.json", {
            "sourceHash": sha256_file(source), "configFingerprint": fingerprint,
            "outputHash": sha256_file(translated), "status": "completed",
            "pipeline": "babeldoc-engine-v1", "issues": [], "message": "翻译完成",
            "coverage": {"total": 10, "pdf": 10, "missing": 0}, "pages": 1,
        })
        self.service.config.secret_path.unlink()
        state = self.service.submit(str(source), automatic=True)
        self.assertTrue(state["cached"])
        self.assertEqual(state["status"], "completed")


if __name__ == "__main__":
    unittest.main()
