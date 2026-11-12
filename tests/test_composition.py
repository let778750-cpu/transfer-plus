"""Synthetic PDFs exercise final fallback and restoration, without private papers."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import pymupdf as fitz
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.config import sha256_file
from rmb.quality import restoration_group, verify_and_compose, disjoint_regions, nomenclature_regions, reference_text

class CompositionTests(unittest.TestCase):
    def test_tiny_punctuation_overlap_joins_restoration_with_its_margin(self):
        def para(x,y,x2,y2):return {'box':dict(x=x,y=y,x2=x2,y2=y2)}
        before={'author':para(10,50,50,60),'comma':para(50.9,54,53,56),'abstract':para(10,10,90,30)}
        area,members=restoration_group('author',before,before,100,fitz.Rect(0,0,100,100))
        self.assertEqual(members,{'author','comma'})
        self.assertGreaterEqual(area.x1,54)
        self.assertFalse(area.intersects(fitz.Rect(10,70,90,90)))

    def test_nomenclature_preserves_content_without_masking_the_column_gutter(self):
        blocks=[(340,100,440,112,'NOMENCLATURE'),(322,120,560,300,'x: Symbol definitions'),
                (48,110,309,290,'Left-column translated body'),(322,315,420,330,'I. INTRODUCTION')]
        region=nomenclature_regions(blocks,612,792)[0]
        self.assertEqual(tuple(region),(321,99,612,313))
        self.assertFalse(region.intersects(fitz.Rect(298,150,309,165)))
        self.assertTrue(region.contains(fitz.Rect(322,120,566,300)))

    def test_reference_text_hides_internal_tokens_and_orders_readable_paragraphs(self):
        text=reference_text([{'render_order':2,'unicode':'正文 {v3}<style id="1">保留</style>'},
                             {'render_order':1,'unicode':'中文标题'},
                             {'render_order':0,'unicode':"Yi Di{v1}<style id='2'>2</style>"}])
        self.assertTrue(text.startswith('中文标题\n正文'))
        self.assertNotIn('<style',text);self.assertNotIn('{v',text);self.assertNotIn('Yi Di',text)
        self.assertIn('公式或标记见原文',text)

    def test_overlapping_restoration_clips_do_not_repaint_glyphs(self):
        regions=disjoint_regions([fitz.Rect(0,0,10,10),fitz.Rect(5,5,15,15),fitz.Rect(0,0,10,10)])
        self.assertAlmostEqual(sum(r.get_area() for r in regions),175)
        for i,a in enumerate(regions):
            for b in regions[i+1:]:self.assertEqual((a&b).get_area(),0)
    def test_nested_fragments_are_restored_together(self):
        def para(x,y,w,h):return {'box':dict(x=x,y=y,x2=x+w,y2=y+h)}
        before={'body':para(10,10,90,40),'doi-fragment':para(40,15,30,10),'neighbor':para(10,100,90,40)}
        after={**before,'body':para(10,10,90,50)}
        area,members=restoration_group('body',before,after,300,fitz.Rect(0,0,300,300))
        self.assertEqual(members,{'body','doi-fragment'});self.assertIsNotNone(area)

    def fixture(self,root,second_ir=True):
        src=fitz.open();dst=fitz.open()
        for _ in range(2):
            src.new_page(width=300,height=300).insert_text((30,40),'Original body with enough text for layout inspection.')
            dst.new_page(width=300,height=300).insert_text((30,40),'这是用于合成验收的中文段落。',fontname='china-s')
        src.save(root/'source.pdf');dst.save(root/'candidate.pdf');src.close();dst.close()
        pages=[{'page_number':i,'pdf_paragraph':[]} for i in range(2 if second_ir else 1)]
        (root/'styles_and_formulas.json').write_text(json.dumps({'page':pages}),encoding='utf-8')
        (root/'final.json').write_text(json.dumps({'page':pages}),encoding='utf-8')
    def compose(self,root):
        verify_and_compose(root/'source.pdf',root/'candidate.pdf',root,root/'result.pdf',root/'report.json',
                           {'minFontScale':.85},[],{'sourceHash':sha256_file(root/'source.pdf'),'configFingerprint':'test'},
                           final_ir_path=root/'final.json')
        return json.loads((root/'report.json').read_text(encoding='utf-8'))
    def test_missing_page_audit_preserves_original_and_reports_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.fixture(root,False);report=self.compose(root)
            with fitz.open(root/'result.pdf') as result,fitz.open(root/'source.pdf') as source:
                self.assertEqual(result[1].get_text(),source[1].get_text())
                self.assertEqual(len(result),2)
            self.assertEqual(report['status'],'completed_with_warnings')
            self.assertIn('audit_page_missing',report['issues'][0]['types'])
    def test_new_render_failure_is_visible_and_restores_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.fixture(root)
            with patch('rmb.quality.render_errors',side_effect=[{'invalid icc'},set(),set(),set()]):
                report=self.compose(root)
            with fitz.open(root/'result.pdf') as result,fitz.open(root/'source.pdf') as source:
                self.assertEqual(result[0].get_text(),source[0].get_text())
            self.assertEqual(report['issues'][0]['types'],['pdf_render_failed'])
            self.assertEqual(report['pagesWithChinese'],1)
    def test_tiny_mask_touching_one_glyph_cannot_silently_drop_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.fixture(root)
            with fitz.open(root/'candidate.pdf') as candidate:
                box=candidate[0].get_text('rawdict')['blocks'][0]['lines'][0]['spans'][0]['chars'][0]['bbox']
            x0,y0,x1,y1=box
            before=json.loads((root/'styles_and_formulas.json').read_text())
            before['page'][0]['page_layout']=[{'class_name':'abandon','box':dict(x=x0+4,y=300-y1,x2=x0+5,y2=300-y0)}]
            (root/'styles_and_formulas.json').write_text(json.dumps(before))
            report=self.compose(root)
            self.assertTrue(any('restoration_clips_text_glyph' in i['types'] for i in report['issues']))
            with fitz.open(root/'result.pdf') as result,fitz.open(root/'source.pdf') as source:
                self.assertEqual(result[0].get_text(),source[0].get_text())
    def test_actual_glyph_overflow_is_detected_even_with_a_small_layout_box(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.fixture(root)
            before={'page':[{'page_number':0,'pdf_paragraph':[{'debug_id':'p','render_order':1,'unicode':'Original body',
                'box':dict(x=30,y=245,x2=230,y2=270),'pdf_paragraph_composition':[]}]}]}
            after={'page':[{'page_number':0,'pdf_paragraph':[{'debug_id':'q','render_order':1,'unicode':'中文',
                'box':dict(x=30,y=245,x2=230,y2=270),'pdf_paragraph_composition':[
                 {'pdf_character':{'char_unicode':c,'box':dict(x=310+i*10,y=245,x2=320+i*10,y2=260)}} for i,c in enumerate('中文')]}]}]}
            (root/'styles_and_formulas.json').write_text(json.dumps(before),encoding='utf-8')
            (root/'final.json').write_text(json.dumps(after),encoding='utf-8')
            report=self.compose(root)
            self.assertTrue(any('text_outside_page' in i['types'] for i in report['issues']))

if __name__=='__main__':unittest.main()
