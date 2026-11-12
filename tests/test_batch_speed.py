import json,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace as N
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.translator import GuardedTranslator,ModelQuotaExceeded
from rmb.checkpoint import TranslationCheckpoint
from rmb.config import DEFAULTS
from rmb.cloud_pipeline import translate_units
from rmb.paddle_cloud import Cancelled

class BatchTests(unittest.TestCase):
    def translator(self,cp,call):
        t=GuardedTranslator.__new__(GuardedTranslator)
        t.model='test';t.memo={};t.records=[];t.lock=threading.RLock();t.next_call=0.;t.failures=0
        t.checkpoint=cp;t.config={**DEFAULTS,'qps':10000};t.quota_error=None;t.check=lambda:None
        t.client=N(chat=N(completions=N(create=call)));t.update_token_count=lambda r:None
        return t
    def response(self,rows):return N(choices=[N(message=N(content=json.dumps(rows)),finish_reason='stop')])
    def test_legacy_cache_reused_and_only_valid_ids_persist(self):
        with tempfile.TemporaryDirectory() as temp:
            cp=TranslationCheckpoint(Path(temp)/'cp.json',{})
            text='The method uses satellite resources.'
            prompt='Reading layer: translate the complete academic paragraph, not a summary. \nNow translate the following text:\n\n'+text
            cp.save({cp.key('single',{'input':prompt}):'该方法使用卫星资源。'})
            sent=[]
            def call(**kw):
                payload=json.loads(kw['messages'][1]['content'].split('## Here is the input:')[1]);sent.extend(payload)
                return self.response([{'id':'b','output':'该方法使用卫星资源 {v0}。'},{'id':'c','output':'Untranslated English'}])
            t=self.translator(cp,call)
            rows=[{'id':'a','input':text,'validator':lambda x:'卫星' in x},
                  {'id':'b','input':text+' {v0}','validator':lambda x:'卫星' in x},
                  {'id':'c','input':'Another paragraph.','validator':lambda x:'中文' in x}]
            out=t.translate_batch_checked(rows)
            self.assertEqual([r['id'] for r in sent],['b','c']);self.assertEqual(out['a'],'该方法使用卫星资源。')
            self.assertEqual(len(cp.entries),2)
            self.assertEqual(out['c'],'Another paragraph.')
            t.client.chat.completions.create=lambda **kw:self.fail('valid cache must avoid network')
            self.assertEqual(t.translate_batch_checked(rows[:2])['b'],out['b'])
    def test_missing_duplicate_or_damaged_placeholder_ids_never_enter_cache(self):
        for outputs in ([{'id':'a','output':'中文内容。'},{'id':'a','output':'中文内容。'}],
                        [{'id':'b','output':'中文内容。'}], [{'id':'a','output':'中文内容。'}]):
            with tempfile.TemporaryDirectory() as temp:
                cp=TranslationCheckpoint(Path(temp)/'cp.json',{})
                t=self.translator(cp,lambda **kw:self.response(outputs))
                t.translate_batch_checked([{'id':'a','input':'The value {v0}.','validator':lambda x:True}])
                self.assertFalse(cp.entries)
    def test_pool_batches_concurrently_and_second_pass_uses_checkpoint(self):
        active=0;peak=0;calls=0;guard=threading.Lock()
        def call(**kw):
            nonlocal active,peak,calls
            with guard:active+=1;peak=max(active,peak);calls+=1
            time.sleep(.03)
            payload=json.loads(kw['messages'][1]['content'].split('## Here is the input:')[1])
            with guard:active-=1
            return self.response([{'id':r['id'],'output':'该方法使用可用的卫星资源。'} for r in payload])
        with tempfile.TemporaryDirectory() as temp:
            cp=TranslationCheckpoint(Path(temp)/'cp.json',{})
            units=[{'id':str(i),'source':'The method uses available satellite resources. '+chr(65+i),
                    'objects':[],'status':'missing'} for i in range(24)]
            translate_units(units,self.translator(cp,call),[],lambda *a:None,lambda:None)
            self.assertEqual(calls,3);self.assertGreaterEqual(peak,2);self.assertLessEqual(peak,4)
            self.assertTrue(all('translation' in u for u in units))
            translate_units(units,self.translator(cp,call),[],lambda *a:None,lambda:None)
            self.assertEqual(calls,3)
    def test_cancel_before_submission_does_not_send_batches(self):
        t=self.translator(None,lambda **kw:self.fail('cancelled'))
        def stop():raise Cancelled()
        with self.assertRaises(Cancelled):translate_units([{'id':'a','source':'Test text','objects':[],'status':'missing'}],t,[],lambda *a:None,stop)

    def test_model_can_translate_input_field_in_place_with_all_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            cp=TranslationCheckpoint(Path(temp)/'cp.json',{})
            t=self.translator(cp,lambda **kw:self.response([{'id':'a','input':'该方法使用卫星资源。'}]))
            rows=[{'id':'a','input':'The method uses satellite resources.','validator':lambda x:'卫星' in x}]
            self.assertEqual(t.translate_batch_checked(rows)['a'],'该方法使用卫星资源。')
            self.assertEqual(len(cp.entries),1)

    def test_extra_duplicate_id_with_valid_text_rejects_entire_response(self):
        with tempfile.TemporaryDirectory() as temp:
            cp=TranslationCheckpoint(Path(temp)/'cp.json',{})
            outputs=[{'id':key,'output':'该方法使用卫星资源。'} for key in ['a','b','a']]
            t=self.translator(cp,lambda **kw:self.response(outputs))
            rows=[{'id':key,'input':'The method uses satellite resources.',
                   'validator':lambda x:'卫星' in x} for key in ['a','b']]
            t.translate_batch_checked(rows)
            self.assertFalse(cp.entries)

    def test_workers_waiting_for_rate_limit_stop_after_quota_error(self):
        from concurrent.futures import ThreadPoolExecutor
        barrier=threading.Barrier(4);calls=[]
        class Exhausted(Exception):body={'error':{'code':'1308'}}
        def call(**kw):
            calls.append(time.monotonic());time.sleep(.03);raise Exhausted()
        t=self.translator(None,call);t.config['qps']=5
        def run(i):
            barrier.wait()
            with self.assertRaises(ModelQuotaExceeded):
                t.translate_batch_checked([{'id':str(i),'input':'Satellite planning.', 'validator':lambda x:True}])
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(run,range(4)))
        self.assertEqual(len(calls),1)

if __name__=='__main__':unittest.main()
