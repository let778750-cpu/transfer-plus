"""Archive migration uses synthetic PDFs and never calls a model."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import pymupdf as fitz
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import repair_first_page as migration


class FirstPageRepairTests(unittest.TestCase):
    def fixture(self, root):
        job=root/'job';base=root/'base';job.mkdir();base.mkdir()
        with fitz.open() as doc:
            doc.new_page().insert_text((40,40),'Original first page')
            doc.new_page().insert_text((40,40),'Original second page')
            doc.save(job/'source.pdf')
        with fitz.open() as doc:
            doc.new_page().insert_text((40,40),'Original first page')
            doc.new_page().insert_text((40,40),'保留第二页已完成的中文译文',fontname='china-s')
            doc.save(base/'result.pdf')
        with fitz.open() as doc:
            doc.new_page().insert_text((40,40),'这是修复后的第一页中文译文',fontname='china-s')
            doc.new_page().insert_text((40,40),'Must not replace retained Chinese')
            doc.save(job/'patch.pdf')
        cfg=dict(migration.DEFAULTS)
        legacy={**cfg,'pipeline':'rmb-0.1.2-babeldoc-0.7.1-guard9'}
        old={'sourceHash':migration.sha256_file(job/'source.pdf'),'outputHash':migration.sha256_file(base/'result.pdf'),
             'status':'completed_with_warnings','configFingerprint':hashlib.sha256(json.dumps(legacy,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
             'issues':[{'page':1,'fallback':'page'},{'page':2,'fallback':'paragraph','types':['protected']}],
             'preserved':[{'page':2,'type':'table'}]}
        for file,value in [(job/'config.json',cfg),(job/'job.json',{'sourceHash':old['sourceHash']}),
                           (job/'translation-records.json',[]),(base/'report.json',old)]:
            file.write_text(json.dumps(value),encoding='utf-8')
        def compose(source,candidate,ir,target,report,config,records,state,**kwargs):
            shutil.copy2(job/'patch.pdf',target)
            migration.atomic_json(report,{'sourceHash':state['sourceHash'],'configFingerprint':state['configFingerprint'],
                                         'pagesWithChinese':1,'issues':[],'preserved':[]})
        return job,base,compose,old

    def test_only_first_page_changes_and_publishing_keeps_old_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);job,base,compose,old=self.fixture(root)
            with patch.object(migration,'verify_and_compose',side_effect=compose):
                result=migration.repair(job,base,root/'out',root/'cache')
            report=json.loads((Path(result['archive'])/'report.json').read_text(encoding='utf-8'))
            self.assertNotEqual(report['configFingerprint'],migration.config_fingerprint(migration.DEFAULTS))
            expected={**migration.DEFAULTS,'pipeline':'rmb-0.1.6-babeldoc-0.7.1-guard10'}
            self.assertEqual(report['configFingerprint'],hashlib.sha256(json.dumps(expected,sort_keys=True,ensure_ascii=False).encode()).hexdigest())
            self.assertEqual(migration.sha256_file(base/'result.pdf'),old['outputHash'])
            with fitz.open(Path(result['archive'])/'result.pdf') as actual,fitz.open(base/'result.pdf') as previous:
                self.assertIn('修复',actual[0].get_text())
                self.assertEqual(actual[1].get_pixmap().samples,previous[1].get_pixmap().samples)
            self.assertEqual(report['issues'],old['issues'][1:]);self.assertEqual(report['preserved'],old['preserved'])
            self.assertEqual(report['checksScope']['recheckedPages'],[1]);self.assertEqual(report['pagesWithChinese'],2)
            with patch.object(migration,'verify_and_compose',side_effect=compose):
                with self.assertRaisesRegex(ValueError,'overwrite'):migration.repair(job,base,root/'again',root/'cache')

    def test_mismatched_source_or_translation_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);job,base,compose,old=self.fixture(root)
            (job/'job.json').write_text(json.dumps({'sourceHash':'wrong'}))
            with self.assertRaisesRegex(ValueError,'Source identity'):migration.repair(job,base,root/'out')
            (job/'job.json').write_text(json.dumps({'sourceHash':old['sourceHash']}))
            cfg=dict(migration.DEFAULTS,glossary='different');(job/'config.json').write_text(json.dumps(cfg))
            with self.assertRaisesRegex(ValueError,'settings differ'):migration.repair(job,base,root/'out')

    def test_failed_first_page_cannot_publish_a_complete_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);job,base,compose,old=self.fixture(root)
            def failed(*args,**kwargs):
                compose(*args,**kwargs)
                path=args[4];report=json.loads(path.read_text());report['issues']=[{'page':1,'fallback':'page'}]
                migration.atomic_json(path,report)
            with patch.object(migration,'verify_and_compose',side_effect=failed):
                with self.assertRaisesRegex(ValueError,'did not pass'):migration.repair(job,base,root/'out',root/'cache')
            self.assertFalse((root/'cache').exists())

    def test_partial_base_cannot_be_promoted_to_full_document_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);job,base,compose,old=self.fixture(root)
            old['preserved'].append({'page':2,'reason':'not_selected'})
            migration.atomic_json(base/'report.json',old)
            with self.assertRaisesRegex(ValueError,'omits pages'):migration.repair(job,base,root/'out',root/'cache')
            self.assertFalse((root/'cache').exists())


if __name__=='__main__':unittest.main()
