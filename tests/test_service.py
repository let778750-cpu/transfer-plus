import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from rmb.config import ConfigStore, atomic_json, config_fingerprint, sha256_file
from rmb.service import JobService, create_app
from rmb.winjob import ProcessTree

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = JobService(Path(self.tmp.name))
        self.client = create_app(self.service, "test-token").test_client()
        self.headers = {"X-RMB-Token": "test-token"}
    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()
    def test_auth_and_origin(self):
        self.assertEqual(self.client.get('/health').status_code, 401)
        self.assertEqual(self.client.get('/health', headers={**self.headers,'Origin':'https://example.org'}).status_code, 403)
        self.assertEqual(self.client.get('/health',headers=self.headers).status_code, 200)
    def test_encryption_and_settings(self):
        result = self.service.config.update({'apiKey':'test-secret-not-real','model':'test-model'})
        self.assertEqual(self.service.config.secret(),'test-secret-not-real')
        self.assertNotIn('test-secret-not-real', self.service.config.path.read_text())
        self.assertNotIn(b'test-secret-not-real',self.service.config.secret_path.read_bytes())
        self.assertNotIn('apiKey', result)
        old=config_fingerprint(result)
        result=self.service.config.update({'glossary':'agent = 智能体'})
        self.assertNotEqual(config_fingerprint(result),old)
    def test_invalid_endpoint(self):
        for url in ['http://example.org','https://secret@example.org','https://example.org/?key=secret']:
            with self.assertRaises(ValueError):self.service.config.update({'baseUrl':url})
    def test_invalid_job_and_pdf(self):
        self.assertEqual(self.client.get('/api/tasks/xyz',headers=self.headers).status_code,400)
        p=Path(self.tmp.name)/'bad.pdf';p.write_text('not a PDF')
        with self.assertRaises(ValueError): self.service.submit(p)
    def test_source_copy_and_cancel_queued(self):
        p=Path(self.tmp.name)/'test.pdf';p.write_bytes(b'%PDF-1.7\n%%EOF')
        self.service.config.update({'apiKey':'test-only','paddleApiKey':'test-ocr-only'})
        self.service.pool.submit=lambda *a:None
        task=self.service.submit(p)
        self.assertEqual(task['sourceHash'],sha256_file(p))
        self.assertEqual(self.service.cancel(task['taskId'])['status'],'cancelled')
    def test_worker_launch_failure_does_not_hang(self):
        p=Path(self.tmp.name)/'test.pdf';p.write_bytes(b'%PDF-1.7\n%%EOF')
        self.service.config.update({'apiKey':'test-only','paddleApiKey':'test-ocr-only'})
        self.service.pool.submit=lambda *a:None
        task=self.service.submit(p)
        with patch('rmb.service.subprocess.Popen',side_effect=OSError('test failure')):
            self.service.run(task['taskId'])
        result=self.service.read_job(task['taskId'])
        self.assertEqual(result['status'],'failed')
        self.assertFalse(result['active'])
    def archived_fixture(self):
        source=Path(self.tmp.name)/'original.pdf';source.write_bytes(b'%PDF-1.7\noriginal fixture\n%%EOF')
        self.service.config.update({'apiKey':'test-only','paddleApiKey':'test-ocr-only'})
        self.service.pool.submit=lambda *a:None
        task=self.service.submit(source,source_key='1:SOURCE01')
        folder=self.service.data/'jobs'/task['taskId']
        output=folder/'result.pdf';output.write_bytes(b'%PDF-1.7\ntranslation fixture\n%%EOF')
        report={'status':'completed','issues':[],'outputHash':sha256_file(output),
                'sourceHash':task['sourceHash'],'configFingerprint':task['configFingerprint']}
        atomic_json(folder/'report.json',report)
        cache=self.service.data/'cache'/task['cacheKey'];cache.mkdir()
        (cache/'result.pdf').write_bytes(output.read_bytes());atomic_json(cache/'report.json',report)
        task.update(status='completed',active=False);self.service.save_job(task)
        return source,task,cache
    def test_archive_survives_restart_rename_and_absent_credential(self):
        source,first,cache=self.archived_fixture()
        renamed=source.with_name('renamed-paper.pdf');renamed.write_bytes(source.read_bytes())
        self.service.config.secret_path.unlink()
        self.service.shutdown();self.service=JobService(Path(self.tmp.name))
        with patch.object(self.service.pool,'submit',side_effect=AssertionError('must not call a worker')):
            task=self.service.submit(renamed,source_key='1:NEWKEY02')
        self.assertTrue(task['cached']);self.assertEqual(task['cacheKey'],first['cacheKey'])
        self.assertNotEqual(task['taskId'],first['taskId'])
        self.assertEqual(task['sourceKey'],'1:NEWKEY02')
        self.assertEqual((self.service.data/'jobs'/task['taskId']/'result.pdf').read_bytes(),(cache/'result.pdf').read_bytes())

    @unittest.skip('0.3 改善流（audit/修订链）语义已被 0.4 一键语义取代：引擎级缓存驱动重跑；遗留代码保留至独立仓库抽取后移除')
    def test_improvement_has_separate_revision_and_does_not_overwrite_old_archive(self):
        source,first,cache=self.archived_fixture();original=(cache/'result.pdf').read_bytes()
        task=self.service.submit(source,improve=True,base_output_hash=sha256_file(cache/'result.pdf'))
        self.assertEqual(task['improveBase'],cache.name);self.assertNotEqual(task['cacheKey'],cache.name)
        self.assertEqual((cache/'result.pdf').read_bytes(),original)
        self.assertEqual(self.service.cancel(task['taskId'])['status'],'cancelled')

    @unittest.skip('0.3 改善流（audit/修订链）语义已被 0.4 一键语义取代：引擎级缓存驱动重跑；遗留代码保留至独立仓库抽取后移除')
    def test_completed_improvement_reopens_offline_and_partial_pages_do_not_match_full(self):
        source,first,cache=self.archived_fixture()
        report=json.loads((cache/'report.json').read_text());report.update(schema=2,coverage={'total':20,'missing':0})
        atomic_json(cache/'report.json',report);self.service.config.secret_path.unlink()
        task=self.service.submit(source,improve=True,base_output_hash=report['outputHash'])
        self.assertTrue(task['cached'])
        report['preserved']=[{'page':2,'reason':'not_selected'}];atomic_json(cache/'report.json',report)
        with self.assertRaises(ValueError):self.service.submit(source,improve=True,base_output_hash=report['outputHash'])
    def test_modified_content_config_and_corrupt_archive_are_not_reused(self):
        source,task,cache=self.archived_fixture();self.service.config.secret_path.unlink()
        source.write_bytes(source.read_bytes()+b'changed')
        with self.assertRaises(ValueError):self.service.submit(source)
        source.write_bytes(source.read_bytes()[:-7])
        self.service.config.update({'model':'new-model'})
        with self.assertRaises(ValueError):self.service.submit(source)
        self.service.config.update({'model':'glm-5.3-flash'})
        (cache/'result.pdf').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):self.service.submit(source)

    def real_archive(self):
        import hashlib
        import pymupdf as fitz
        from rmb.cloud_pipeline import run_pipeline
        source=Path(self.tmp.name)/'real.pdf'
        with fitz.open() as doc:
            doc.new_page().insert_textbox(fitz.Rect(40,40,500,180),'The proposed scheduling method selects observation windows according to resource constraints and task priorities.',fontsize=11)
            doc.save(source)
        fingerprint=config_fingerprint(self.service.config.read());source_hash=sha256_file(source)
        key=hashlib.sha256((source_hash+fingerprint+json.dumps(None)).encode()).hexdigest()
        folder=self.service.data/'cache'/key;folder.mkdir(parents=True)
        (folder/'result.pdf').write_bytes(source.read_bytes())
        atomic_json(folder/'report.json',{'sourceHash':source_hash,'outputHash':source_hash,'configFingerprint':fingerprint,'pages':1,'issues':[],'preserved':[]})
        class Translator:
            def translate_checked(self,*args,**kwargs):return '提出的调度方法根据资源约束和任务优先级选择合适的观测时间窗口，以便完成所有需要执行的任务。'
        class Cloud:
            def parse(self,path,model,count,options):
                with fitz.open(path) as d:
                    return [{'page_index':i,'width':p.rect.width,'height':p.rect.height,'parsing_res_list':[
                        {'block_label':'text','block_bbox':b['bbox'],'block_content':'text'} for b in p.get_text('dict')['blocks'] if 'lines' in b]} for i,p in enumerate(d)]
        report=run_pipeline(source,folder,self.service.data,{'sourceHash':source_hash,'configFingerprint':fingerprint},
            self.service.config.read(),Cloud(),Translator(),lambda *a:None,lambda:None)
        return source,folder,report

    @unittest.skip('0.3 改善流（audit/修订链）语义已被 0.4 一键语义取代：引擎级缓存驱动重跑；遗留代码保留至独立仓库抽取后移除')
    def test_automatic_click_audits_complete_archive_without_credentials_or_worker(self):
        source,folder,report=self.real_archive()
        with patch.object(self.service.pool,'submit',side_effect=AssertionError('offline result must not request a model')):
            result=self.service.submit(source,automatic=True)
        self.assertTrue(result['cached']);self.assertFalse(result['archiveAudit']['needsWork'])
        self.assertEqual(result['cacheKey'],folder.name)

    @unittest.skip('0.3 改善流（audit/修订链）语义已被 0.4 一键语义取代：引擎级缓存驱动重跑；遗留代码保留至独立仓库抽取后移除')
    def test_unchanged_layout_limit_does_not_launch_identical_revision(self):
        source,folder,report=self.real_archive()
        for u in report['reading']['units']:u.update(status='missing',reason='layout_pending')
        report['coverage']['missing']=report['coverage']['total'];report['coverage']['pdf']=0
        report['status']='completed_with_warnings';atomic_json(folder/'report.json',report)
        with patch.object(self.service.pool,'submit',side_effect=AssertionError('identical layout must not restart')):
            result=self.service.submit(source,automatic=True)
        self.assertTrue(result['cached']);self.assertEqual(result['cacheKey'],folder.name)
        self.assertIn('未完成',result['message']);self.assertGreater(result['archiveAudit']['unfinished'],0)

    @unittest.skip('0.3 改善流（audit/修订链）语义已被 0.4 一键语义取代：引擎级缓存驱动重跑；遗留代码保留至独立仓库抽取后移除')
    def test_automatic_audit_detects_missing_unit_even_when_summary_claims_complete(self):
        source,folder,report=self.real_archive();report['reading']['units']=[];atomic_json(folder/'report.json',report)
        self.service.config.update({'apiKey':'test-only','paddleApiKey':'test-ocr-only'});self.service.pool.submit=lambda *a:None
        result=self.service.submit(source,automatic=True)
        self.assertEqual(result['improveBase'],folder.name);self.assertTrue(result['archiveAudit']['needsWork'])
        self.assertNotEqual(result['cacheKey'],folder.name)
        again=self.service.submit(source,automatic=True);self.assertEqual(again['taskId'],result['taskId'])
        self.service.cancel(result['taskId'])

    @unittest.skip('0.3 改善流（audit/修订链）语义已被 0.4 一键语义取代：引擎级缓存驱动重跑；遗留代码保留至独立仓库抽取后移除')
    def test_automatic_click_upgrades_old_rules_even_if_old_summary_has_no_missing_words(self):
        import hashlib
        from rmb.config import DEFAULTS
        source,folder,report=self.real_archive()
        public={k:self.service.config.read()[k] for k in DEFAULTS if k not in ('ocrProvider','ocrModel')};public['pipeline']='rmb-0.2.0-babeldoc-0.7.1-reading1'
        report['configFingerprint']=hashlib.sha256(json.dumps(public,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        report['reading']['schema']=1;atomic_json(folder/'report.json',report)
        self.service.config.update({'apiKey':'test-only','paddleApiKey':'test-ocr-only'});self.service.pool.submit=lambda *a:None
        result=self.service.submit(source,automatic=True,base_output_hash='stale-client-hint')
        self.assertEqual(result['improveBase'],folder.name)
        self.assertEqual(result['archiveAudit']['reason'],'recognition_upgrade')
        self.service.cancel(result['taskId'])

    @unittest.skip('0.3 改善流（audit/修订链）语义已被 0.4 一键语义取代：引擎级缓存驱动重跑；遗留代码保留至独立仓库抽取后移除')
    def test_automatic_audits_existing_derived_result_and_never_overwrites_it(self):
        import hashlib
        source,folder,report=self.real_archive()
        report['reading']['units']=[];atomic_json(folder/'report.json',report)
        canonical=hashlib.sha256((report['sourceHash']+report['configFingerprint']+json.dumps(None)).encode()).hexdigest()
        derived_key=hashlib.sha256((canonical+folder.name+sha256_file(folder/'report.json')).encode()).hexdigest()
        derived=folder.parent/derived_key;derived.mkdir()
        (derived/'result.pdf').write_bytes((folder/'result.pdf').read_bytes())
        report['coverage']['missing']=report['coverage']['total'];report['coverage']['pdf']=0
        atomic_json(derived/'report.json',report);before=(derived/'report.json').read_bytes()
        self.service.config.update({'apiKey':'test-only','paddleApiKey':'test-ocr-only'});self.service.pool.submit=lambda *a:None
        result=self.service.submit(source,automatic=True)
        self.assertFalse(result.get('cached',False));self.assertEqual(result['improveBase'],derived_key)
        self.assertNotEqual(result['cacheKey'],derived_key);self.assertEqual((derived/'report.json').read_bytes(),before)
        self.assertEqual(self.service.submit(source,automatic=True)['taskId'],result['taskId'])
        self.service.cancel(result['taskId'])
    def test_import_cleans_temporary_files_but_keeps_permanent_archive(self):
        source,task,cache=self.archived_fixture()
        folder=self.service.data/'jobs'/task['taskId'];work=folder/'engine-work';work.mkdir()
        (work/'private-ir.json').write_text('temporary');(folder/'prompt-cache.json').write_text('temporary')
        response=self.client.post('/api/tasks/'+task['taskId']+'/imported',headers=self.headers,
                                  json={'attachmentKey':'TEST0001','libraryID':1})
        self.assertEqual(response.status_code,200)
        self.assertTrue(source.exists());self.assertTrue((cache/'result.pdf').exists())
        self.assertFalse(work.exists());self.assertFalse((folder/'result.pdf').exists())
        report=json.loads((self.service.data/'reports'/(task['cacheKey']+'.json')).read_text(encoding='utf-8'))
        self.assertEqual(report['attachment']['key'],'TEST0001')
        # A second consumer of the same task still receives its complete PDF.
        response=self.client.get('/api/tasks/'+task['taskId']+'/result',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(Path(response.json['path']).read_bytes(),(cache/'result.pdf').read_bytes())
        downloaded=self.client.get('/translatedFile/'+task['taskId'],headers=self.headers)
        self.assertEqual(downloaded.data,(cache/'result.pdf').read_bytes());downloaded.close()
    @unittest.skipUnless(os.name=='nt','Windows only')
    def test_process_tree_kill(self):
        p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW)
        tree=ProcessTree(p);tree.close();p.wait(timeout=5)
        self.assertIsNotNone(p.returncode)

if __name__=='__main__':unittest.main()
