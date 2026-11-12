import json
from pathlib import Path
import sys
import threading
import unittest
from types import SimpleNamespace as NS
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.translator import GuardedTranslator, validate_translation
from rmb.worker import predominantly_chinese
from rmb.quality import recognition_issues, selected_pages

class ProtocolTests(unittest.TestCase):
    def test_model_removes_or_duplicates_math(self):
        for text in ['中文','中文 {v1} {v1}','中文 { v1 }']:
            self.assertEqual(validate_translation('Use {v1}',text),'math_placeholder_changed')
        self.assertIsNone(validate_translation('Use {v1} and <style id="1">x</style>', '使用 {v1} 和 <style id="1">x</style>'))
    def test_malformed_response_keeps_all_paragraphs(self):
        translator=object.__new__(GuardedTranslator)
        translator.memo={};translator.records=[];translator.lock=threading.RLock();translator.next_call=0;translator.failures=0
        translator.config={'qps':8,'glossary':''};translator.model='synthetic'
        translator.client=NS(chat=NS(completions=NS(create=lambda **kw: NS(choices=[NS(message=NS(content='[{"id":0,"output":"丢失第二段"}]'),finish_reason='stop')]))))
        translator.update_token_count=lambda response:None
        source=[{'id':0,'input':'First {v1}'},{'id':1,'input':'Second paragraph'}]
        result=json.loads(translator.do_llm_translate('## Here is the input:\n'+json.dumps(source)))
        self.assertEqual([r['output'] for r in result],[r['input'] for r in source])
        self.assertTrue(all(r['issue']=='paragraph_structure_changed' for r in translator.records))
    def test_api_failure_is_reported(self):
        translator=object.__new__(GuardedTranslator)
        translator.memo={};translator.records=[];translator.lock=threading.RLock();translator.next_call=0;translator.failures=0
        translator.config={'qps':8,'glossary':''};translator.model='synthetic'
        def fail(**kw):raise TimeoutError('not recorded: private text')
        translator.client=NS(chat=NS(completions=NS(create=fail)))
        self.assertEqual(translator.do_llm_translate('Now translate the following text:\n\nSource {v1}'),'Source {v1}')
        self.assertEqual(translator.records[0]['issue'],'model_request_failed:TimeoutError')
    def test_chinese_with_english_metadata(self):
        self.assertTrue(predominantly_chinese('中文论文标题与中文摘要说明'*15+' metadata '*700))
        self.assertFalse(predominantly_chinese('An English academic paper '*100+'中文'))
    def test_selected_pages_are_physical_indices(self):
        self.assertEqual(selected_pages('1,3-4',5),{0,2,3})
        for pages in ['0','5-3','1-6']:
            with self.assertRaises(ValueError):selected_pages(pages,5)
    def test_split_multiline_formula_is_rejected(self):
        para={'pdf_paragraph_composition':[{'pdf_formula':{'box':{'x':0,'y':0,'x2':100,'y2':25},'pdf_character':[{'pdf_style':{'font_size':10}}]}}]}
        self.assertEqual(recognition_issues(para,{}),['multiline_math_recognition_uncertain'])

if __name__=='__main__':unittest.main()
