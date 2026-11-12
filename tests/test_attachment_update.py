import unittest,tempfile,json,sys
from pathlib import Path
from types import SimpleNamespace
import pymupdf as fitz
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.attachment_update import prepare_update
from rmb.config import atomic_json,sha256_file

class AttachmentUpdateTests(unittest.TestCase):
    def test_backup_is_private_and_annotation_page_is_restored_locally(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);job=root/'jobs'/'job';job.mkdir(parents=True)
            old=root/'old.pdf';new=job/'result.pdf'
            with fitz.open() as d:
                for text in ['old page','annotated page']:
                    p=d.new_page();p.insert_text((40,50),text)
                d.save(old)
            with fitz.open(old) as d:
                d[0].insert_text((40,90),'new translation');d.save(new)
            report={'sourceHash':'source','outputHash':sha256_file(new)};atomic_json(job/'report.json',report)
            service=SimpleNamespace(data=root,read_job=lambda j:{'sourceHash':'source'},result_path=lambda j:new)
            data={'path':str(old),'outputHash':sha256_file(old),'sourceHash':'source','attachmentKey':'TRAN0001','libraryID':1,'annotations':[{'key':'ANNO0001','position':{'pageIndex':1}}],'report':{}}
            result=prepare_update(service,'job',data)
            self.assertEqual(sha256_file(Path(result['backupPath'])),sha256_file(old))
            self.assertEqual(list(root.glob('*.pdf')),[old])
            data['annotations'][0]['position']['pageIndex']=0
            restored=prepare_update(service,'job',data)
            with fitz.open(restored['resultPath']) as d:
                self.assertNotIn('new translation',d[0].get_text())
            self.assertEqual(restored['report']['issues'][0]['type'],'annotation_anchor')
            data['outputHash']='0'*64
            with self.assertRaisesRegex(ValueError,'外部改变'):prepare_update(service,'job',data)
