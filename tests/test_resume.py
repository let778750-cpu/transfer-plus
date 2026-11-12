"""Offline interruption/restart tests; no private papers or model calls."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace as NS
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.checkpoint import TranslationCheckpoint, scope_of, save_stage, stage_ready, STAGE_FILES
from rmb.translator import GuardedTranslator
from rmb.service import JobService, create_app

def translator(checkpoint, responder):
    obj=object.__new__(GuardedTranslator)
    obj.memo={};obj.records=[];obj.lock=threading.RLock();obj.next_call=0;obj.failures=0
    obj.config={'qps':8,'glossary':''};obj.model='synthetic';obj.checkpoint=checkpoint
    obj.client=NS(chat=NS(completions=NS(create=responder)))
    obj.update_token_count=lambda response:None
    return obj

class ResumeTests(unittest.TestCase):
    def test_completed_rows_survive_new_process_and_changed_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'checkpoint.json';scope={'sourceHash':'source','configFingerprint':'config'}
            calls=[]
            def respond(**kw):
                rows=json.loads(kw['messages'][1]['content'].split('## Here is the input:')[1]);calls.append(rows)
                return NS(choices=[NS(message=NS(content=json.dumps([{'id':r['id'],'output':'翻译 '+r['input']} for r in rows])),finish_reason='stop')])
            first=translator(TranslationCheckpoint(path,scope),respond)
            prompt=lambda rows:'Instructions\n## Here is the input:\n'+json.dumps(rows)
            first.do_llm_translate(prompt([{'id':'old','input':'First {v1}'}]))
            second=translator(TranslationCheckpoint(path,scope),respond)
            result=json.loads(second.do_llm_translate(prompt([{'id':19,'input':'First {v1}'},{'id':20,'input':'Second'}])))
            self.assertEqual([r['id'] for r in calls[1]],[20])
            self.assertEqual(result,[{'id':19,'output':'翻译 First {v1}'},{'id':20,'output':'翻译 Second'}])
            third=translator(TranslationCheckpoint(path,scope),lambda **kw:self.fail('no new API call expected'))
            third.do_llm_translate(prompt([{'id':'new','input':'First {v1}'}]))
            self.assertEqual(TranslationCheckpoint(path,{'sourceHash':'source','configFingerprint':'different'}).entries,{})

    def test_failures_are_not_persisted_as_successful_translations(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint=TranslationCheckpoint(Path(folder)/'checkpoint.json',{})
            bad=translator(checkpoint,lambda **kw:NS(choices=[NS(message=NS(content='错误，遗漏数学对象'),finish_reason='stop')]))
            self.assertEqual(bad.do_llm_translate('Now translate the following text:\nUse {v1}'),'Use {v1}')
            self.assertEqual(checkpoint.entries,{})

    def test_cancel_resume_keeps_work_but_never_delivers_partial_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);service=JobService(root);service.pool.submit=lambda *args:None
            service.config.update({'apiKey':'synthetic-not-real','paddleApiKey':'synthetic-not-real'})
            source=root/'paper.pdf';source.write_bytes(b'%PDF-1.7\n%%EOF')
            first=service.submit(source);job=root/'jobs'/first['taskId']
            TranslationCheckpoint(job/'translation-checkpoint.json',scope_of(first)).save({'known':'已完成段落'})
            for name in STAGE_FILES['inspection']:
                (job/name).parent.mkdir(parents=True,exist_ok=True);(job/name).write_text('{}')
            save_stage(job,first,'inspection')
            (job/'result.pdf').write_bytes(b'%PDF-partial')
            service.cancel(first['taskId'])
            self.assertTrue((job/'result.pdf').exists())
            with self.assertRaises(ValueError): service.result_path(first['taskId'])
            app=create_app(service,'test-token');client=app.test_client()
            response=client.post(f"/api/tasks/{first['taskId']}/imported",json={'attachmentKey':'TEST0001','libraryID':1},headers={'X-RMB-Token':'test-token'})
            self.assertEqual(response.status_code,400)
            service.shutdown();service=JobService(root);service.pool.submit=lambda *args:None
            second=service.submit(source)
            self.assertEqual(second['resumedFrom'],first['taskId'])
            self.assertEqual(second['resume']['savedTranslations'],1)
            resumed=root/'jobs'/second['taskId'];self.assertTrue(stage_ready(resumed,second,'inspection'))
            self.assertFalse((resumed/'result.pdf').exists())
            service.cancel(second['taskId']);service.config.update({'model':'different'})
            third=service.submit(source);self.assertNotIn('resumedFrom',third)
            service.shutdown()

    def test_corrupt_stage_is_not_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            job=Path(folder);state={'sourceHash':'s','configFingerprint':'c'}
            name=STAGE_FILES['inspection'][0];(job/name).parent.mkdir(parents=True);(job/name).write_text('{}')
            save_stage(job,state,'inspection');(job/name).write_text('changed')
            self.assertFalse(stage_ready(job,state,'inspection'))
