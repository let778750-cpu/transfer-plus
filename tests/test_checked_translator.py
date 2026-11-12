from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.translator import GuardedTranslator,ModelQuotaExceeded
from rmb.config import DEFAULTS
from rmb.checkpoint import TranslationCheckpoint,SharedMemory
from rmb.reading import good_translation

class CheckedTranslatorTests(unittest.TestCase):
    def translator(self,checkpoint,call):
        with patch('rmb.translator.OpenAITranslator.__init__',return_value=None):
            # Avoid constructing a real transport or upstream cache in tests.
            translator=GuardedTranslator.__new__(GuardedTranslator)
        import threading
        translator.model='test';translator.memo={};translator.records=[];translator.lock=threading.RLock()
        translator.next_call=0.;translator.failures=0;translator.checkpoint=checkpoint
        translator.config={**DEFAULTS,'qps':10000};translator.quota_error=None
        translator.client=N(chat=N(completions=N(create=call)));translator.update_token_count=lambda r:None
        return translator

    def response(self,text):return N(choices=[N(message=N(content=text),finish_reason='stop')])

    def test_rejected_result_is_not_persisted_and_next_attempt_calls_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'memory.json';cp=TranslationCheckpoint(path,{'source':'same'})
            source='The scheduling method uses available satellite resources.'
            validator=lambda text:good_translation(source,text,[])
            calls=[]
            bad=self.translator(cp,lambda **kw:(calls.append('bad') or self.response(source)))
            bad.translate_checked(source,validator)
            self.assertEqual(cp.entries,{})
            good=self.translator(TranslationCheckpoint(path,{'source':'same'}),lambda **kw:(calls.append('good') or self.response('调度方法使用可用的卫星资源。')))
            translated=good.translate_checked(source,validator);self.assertTrue(validator(translated));self.assertEqual(calls,['bad','good'])
            again=self.translator(TranslationCheckpoint(path,{'source':'same'}),lambda **kw:self.fail('cache should be used'))
            self.assertEqual(again.translate_checked(source,validator),translated)

    def test_quota_exhaustion_stops_further_requests_without_caching_failure(self):
        class Quota(Exception):body={'error':{'code':'1308','message':'限额将在 2026-11-10 18:53:15 重置'}}
        count=[]
        def call(**kwargs):count.append(1);raise Quota()
        t=self.translator(None,call)
        for text in ('First academic paragraph.','Second academic paragraph.'):
            with self.assertRaises(ModelQuotaExceeded):t.translate_checked(text,lambda text:True)
        self.assertEqual(len(count),1);self.assertEqual(t.memo,{})

    def test_shared_memory_merges_writers_loaded_before_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'memory.json';first=SharedMemory(path,{});second=SharedMemory(path,{})
            first.save({'a':'甲'});second.save({'b':'乙'})
            self.assertEqual(TranslationCheckpoint(path,{}).entries,{'a':'甲','b':'乙'})

if __name__=='__main__':unittest.main()
