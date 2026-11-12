import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
import pymupdf as fitz
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.config import atomic_json,sha256_file,translation_fingerprint,DEFAULTS
from rmb.reading import make_input,good_translation,enhance_reading,tokens_for_line,safe_typeset,pdf_body_present,complex_math,segments_from_translation,audit_archive,is_literal,literal_text
from rmb.structure import reference_regions
from rmb.checkpoint import DurableCheckpoint

class ReadingTests(unittest.TestCase):
    def test_drop_cap_is_rejoined_as_prose(self):
        with fitz.open() as doc:
            p=doc.new_page();p.insert_text((40,70),'A',fontsize=28,fontname='Times-Roman')
            p.insert_text((61,55),'S AN important resource for satellite scheduling.',fontsize=10,fontname='Times-Roman')
            from rmb.direct_pdf import native_paragraphs
            lines=[l for b in p.get_text('rawdict')['blocks'] for l in b.get('lines',[])]
            value,objects=make_input({'lines':lines})
            self.assertIn('AS AN important',value);self.assertEqual(objects,[])

    def test_acronyms_retained_in_chinese_do_not_force_retranslation(self):
        self.assertTrue(pdf_body_present('Index Terms Adaptive large neighborhood search ALNS agile Earth observation satellite AEOS',
            'Index Terms 自适应大邻域搜索 ALNS 敏捷地球观测卫星 AEOS'))

    def test_complex_formula_keeps_prose_bands_in_the_inventory(self):
        from rmb.reading import reading_blocks
        with fitz.open() as doc:
            p=doc.new_page();p.insert_textbox(fitz.Rect(40,40,350,160),'The scheduling method uses a normalized value.\nx y\nThe final result describes the satellite schedule.',fontsize=10)
            with patch('rmb.reading.complex_math',return_value=True):blocks=reading_blocks(p)
            texts=[make_input(b)[0] for b in blocks]
            self.assertTrue(any('scheduling method' in t for t in texts));self.assertTrue(any('final result' in t for t in texts))
            self.assertTrue(any('{v0}' in t for t in texts))

    def line(self,words,font='Times-Italic'):
        return {'spans':[{'font':font,'size':10,'chars':[{'c':c,'bbox':[10+i*5,10,15+i*5,20],'origin':[10+i*5,18]} for i,c in enumerate(words)]}]}

    def test_italic_prose_is_text_but_variable_and_superscript_are_protected(self):
        value,objects=make_input({'lines':[self.line('effective heuristic x x2 bestSolution')]})
        self.assertIn('effective heuristic',value);self.assertNotIn('bestSolution',value)
        self.assertEqual(len(objects),1) # adjacent math tokens form one untouched crop

    def test_font_fragmented_word_and_line_break_are_reconstructed(self):
        first=self.line('An LL','Times-Roman');first['spans']+=self.line('M-based heuris-','Times-Bold')['spans']
        value,_=make_input({'lines':[first,self.line('tic is effective.','Times-Roman')]})
        self.assertIn('LLM-based heuristic',value)

    def test_math_tokens_are_required_once_and_unknown_tokens_rejected(self):
        self.assertTrue(good_translation('value {v0}','中文数值 {v0}',[fitz.Rect(0,0,10,10)]))
        self.assertFalse(good_translation('value {v0}','中文数值',[]))
        self.assertFalse(good_translation('value {v0}','中文数值 {v0} {v0}',[1]))
        self.assertFalse(good_translation('value','中文数值 {v9}',[]))
        self.assertFalse(good_translation('important '*50,'中文',[]))

    def test_dates_and_project_ids_are_exact_text_not_formula_images(self):
        line=self.line('Received 6 July 2026 under project 72201722.','Times-Roman')
        text,objects=make_input({'lines':[line]})
        self.assertTrue(all(is_literal(o) for o in objects));self.assertEqual(len(objects),3)
        with fitz.open() as doc:
            p=doc.new_page();segments=segments_from_translation('日期 {v0} 年份 {v1} 项目 {v2}',objects,p,True)
            self.assertFalse(any(s.get('image') or s.get('reference') for s in segments))
            self.assertIn('72201722.', ''.join(s.get('text','') for s in segments))

    def test_old_successful_reading_text_is_reused_and_reaudited_without_model(self):
        class Initial:
            def translate_checked(self,text,validator,retry=False):return '本文完整介绍调度方法及其实际用途，解释该方法如何支持任务规划、观测活动安排及实际调度执行过程。'+''.join(re.findall(r'\{v\d+\}',text))
        class NoModel:
            def translate_checked(self,*args,**kwargs):raise AssertionError('successful translation must be reused')
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.fixture(root)
            first=enhance_reading(root/'source.pdf',root/'result.pdf',root/'report.json',Initial())
            second=enhance_reading(root/'source.pdf',root/'result.pdf',root/'report.json',NoModel())
            self.assertEqual(first['coverage'],second['coverage'])
            self.assertFalse(audit_archive(root/'source.pdf',root/'result.pdf',second)['needsWork'])
            second['reading']['units'].pop()
            self.assertTrue(audit_archive(root/'source.pdf',root/'result.pdf',second)['needsWork'])

    def test_literal_only_paragraph_can_be_typeset_with_numbers_unchanged(self):
        class Translator:
            def translate_checked(self,text,validator,retry=False):return '该项目提出了详细的调度方法，并使用固定数量的卫星资源来验证其性能，研究于 {v0} 年开展，项目编号为 {v1}'
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            with fitz.open() as doc:
                p=doc.new_page(width=420,height=600)
                p.insert_textbox(fitz.Rect(40,40,380,140),'This project describes the scheduling method with detailed satellite resource constraints and validation results in 2026 under project 72201722.',fontsize=10)
                doc.save(root/'source.pdf');doc.save(root/'result.pdf')
            atomic_json(root/'report.json',{'sourceHash':sha256_file(root/'source.pdf'),'outputHash':sha256_file(root/'result.pdf'),'pages':1,'issues':[],'preserved':[]})
            result=enhance_reading(root/'source.pdf',root/'result.pdf',root/'report.json',Translator())
            self.assertGreater(result['coverage']['pdf'],0);self.assertEqual(result['coverage']['supplement'],0)
            with fitz.open(root/'result.pdf') as doc:
                self.assertIn('2026',doc[0].get_text());self.assertIn('72201722',doc[0].get_text())

    def test_reflow_rejects_neighbour_translation_inside_original_target_box(self):
        with fitz.open() as source,fitz.open() as target:
            a=source.new_page(width=420,height=400);a.insert_text((40,50),'Original text in target box')
            a.insert_text((40,140),'Neighbour original text')
            b=target.new_page(width=420,height=400);b.insert_text((40,50),'Original text in target box')
            b.insert_text((40,90),'Neighbour translation has moved here')
            before=b.get_text()
            result=safe_typeset(target,0,fitz.Rect(35,30,385,105),'本段中文',10,[],[fitz.Rect(35,120,385,150)],source[0])
            self.assertIsNone(result);self.assertEqual(target[0].get_text(),before)

    def test_mixed_page_cannot_count_untranslated_english_as_complete(self):
        source='The satellite scheduler chooses observation windows according to resource constraints. '*5
        self.assertFalse(pdf_body_present(source,source+'少量中文不能证明整个段落已经完成翻译。'*3))
        self.assertTrue(pdf_body_present(source,'调度器根据资源约束选择卫星观测窗口。'*6))

    def test_fraction_with_separate_denominator_uses_original_page_reference(self):
        with fitz.open() as doc:
            p=doc.new_page(width=420,height=400)
            p.insert_text((40,50),'The normalized value is represented by x')
            # Place numerator/denominator in separate text blocks and keep vector bar.
            p.insert_text((300,90),'x');p.insert_text((300,110),'y');p.draw_line((295,96),(310,96))
            box=fitz.Rect(300,78,307,93)
            self.assertTrue(complex_math([box],p))
            segments=segments_from_translation('归一化值 {v0}',[box],p,True,box)
            self.assertTrue(any(s.get('reference') for s in segments));self.assertFalse(any('image' in s for s in segments))

    def test_reference_state_ends_at_appendix_and_body_after_it(self):
        with fitz.open() as doc:
            p=doc.new_page();p.insert_text((40,40),'REFERENCES',fontname='hebo');p.insert_text((40,70),'[1] A. Smith. Citation title.')
            p=doc.new_page();p.insert_text((40,40),'Appendix A. Extra experiments',fontname='hebo');p.insert_text((40,70),'This is body text that must be translated.')
            regions=reference_regions(doc)
            self.assertTrue(regions[0]);self.assertEqual(regions[1],[])
        with fitz.open() as doc:
            p=doc.new_page();p.insert_text((40,40),'References');p.insert_text((40,70),'[1] Citation text.')
            p.insert_text((40,110),'APPENDIX');p.insert_text((40,140),'Body after references on the same page.')
            regions=reference_regions(doc)[0]
            self.assertTrue(all(r.y1<110 for r in regions))

    def test_completed_translation_memory_survives_render_version_and_job_cleanup(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);scope={'sourceHash':'a','translationFingerprint':translation_fingerprint(DEFAULTS)}
            first=DurableCheckpoint(root/'one.json',{'configFingerprint':'old'},root/'memory.json',scope)
            first.save({'paragraph':'中文内容'});(root/'one.json').unlink()
            next=DurableCheckpoint(root/'two.json',{'configFingerprint':'new'},root/'memory.json',scope)
            self.assertEqual(next.get('paragraph'),'中文内容')
            changed=DurableCheckpoint(root/'three.json',{},root/'memory.json',{**scope,'translationFingerprint':'different'})
            self.assertIsNone(changed.get('paragraph'))

    def fixture(self,root):
        with fitz.open() as doc:
            p=doc.new_page(width=420,height=600)
            p.insert_textbox(fitz.Rect(40,40,380,135),'This paragraph explains the proposed scheduling method and its practical advantages for mission planning. '*3,fontsize=10)
            p.insert_textbox(fitz.Rect(40,170,380,260),'The decision variable x2 represents the scheduled observation and preserves the original mathematical meaning.',fontsize=10)
            doc.save(root/'source.pdf');doc.save(root/'result.pdf')
        report={'sourceHash':sha256_file(root/'source.pdf'),'outputHash':sha256_file(root/'result.pdf'),'pages':1,
                'issues':[{'page':1,'region':None,'types':['restoration_clips_text_glyph'],'fallback':'page'}],
                'preserved':[],'configFingerprint':'test'}
        atomic_json(root/'report.json',report)
        return report

    def test_plain_and_inline_math_are_integrated_into_pdf_without_supplement(self):
        class Translator:
            def translate_checked(self,text,validator,retry=False):return '这是完整的中文说明，介绍了调度方法及其实际用途，并解释方法如何支持任务规划与观测活动安排。'+''.join(re.findall(r'\{v\d+\}',text))
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);before=self.fixture(root)
            report=enhance_reading(root/'source.pdf',root/'result.pdf',root/'report.json',Translator())
            self.assertEqual(sha256_file(root/'source.pdf'),before['sourceHash'])
            self.assertTrue(report['reading']['repairs']);self.assertGreater(report['coverage']['pdf'],0)
            self.assertEqual(report['coverage']['supplement'],0);self.assertEqual(report['coverage']['missing'],0)
            self.assertTrue(all(u['status']=='pdf' for u in report['reading']['units']))
            self.assertFalse(any('segments' in u for u in report['reading']['units']))
            with fitz.open(root/'result.pdf') as doc:self.assertIn('x2',doc[0].get_text())

    def test_failed_translation_remains_missing_and_cannot_count_as_chinese(self):
        class Bad:
            def translate_checked(self,text,validator,retry=False):return text
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);before=self.fixture(root)
            report=enhance_reading(root/'source.pdf',root/'result.pdf',root/'report.json',Bad())
            self.assertEqual(report['status'],'failed');self.assertGreater(report['coverage']['missing'],0)
            self.assertEqual(sha256_file(root/'result.pdf'),before['outputHash'])

    def test_pending_layout_is_reused_but_never_counted_as_translated_pdf(self):
        class Initial:
            def translate_checked(self,text,validator,retry=False):return '本文详细解释调度方法的实际用途，分析任务安排和观测计划之间的关系，保留所有变量与数学含义。'+''.join(re.findall(r'\{v\d+\}',text))
        class NoModel:
            def translate_checked(self,*a,**kw):raise AssertionError('must reuse saved translation')
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);self.fixture(root)
            with patch('rmb.reading.safe_typeset',return_value=None):
                first=enhance_reading(root/'source.pdf',root/'result.pdf',root/'report.json',Initial())
                second=enhance_reading(root/'source.pdf',root/'result.pdf',root/'report.json',NoModel())
            self.assertEqual(first['coverage'],second['coverage'])
            self.assertEqual(second['coverage']['pdf'],0);self.assertEqual(second['coverage']['supplement'],0)
            self.assertEqual(second['coverage']['missing'],second['coverage']['total'])
            self.assertTrue(audit_archive(root/'source.pdf',root/'result.pdf',second)['needsWork'])

    def test_inline_original_vectors_keep_subscript_geometry(self):
        from rmb.direct_pdf import compose_inline
        with fitz.open() as doc:
            p=doc.new_page(width=420,height=600)
            p.insert_text((40,60),'x',fontname='tiit',fontsize=12)
            p.insert_text((46,63),'2',fontname='tiro',fontsize=7)
            boxes=[c['bbox'] for b in p.get_text('rawdict')['blocks'] for l in b.get('lines',[]) for s in l['spans'] for c in s['chars']]
            box=fitz.Rect(boxes[0])
            for b in boxes[1:]:box|=fitz.Rect(b)
            snippet=compose_inline(p,fitz.Rect(30,30,390,200),'决策变量 {v0} 表示已安排的观测任务。',[{'box':list(box),'kind':'math','text':'x2'}],12,1)
            self.assertIsNotNone(snippet)
            raw=[c for b in snippet[0].get_text('rawdict')['blocks'] for l in b.get('lines',[]) for s in l['spans'] for c in s['chars']]
            x=next(c for c in raw if c['c']=='x');two=next(c for c in raw if c['c']=='2')
            self.assertAlmostEqual(two['origin'][0]-x['origin'][0],6,places=2)
            self.assertAlmostEqual(two['origin'][1]-x['origin'][1],3,places=2)
            import unicodedata
            self.assertIn('决策变量',unicodedata.normalize('NFC',snippet[0].get_text()));snippet.close()

    def test_indented_paragraphs_split_without_losing_source_glyphs(self):
        from rmb.direct_pdf import native_paragraphs
        with fitz.open() as doc:
            p=doc.new_page(width=420,height=600)
            for i in range(10):
                x=50 if i in (0,5) else 40
                text='First sentence ends.' if i==4 else 'Another line of ordinary body text.'
                p.insert_text((x,50+i*12),text,fontsize=10)
            original=[b for b in p.get_text('rawdict')['blocks'] if b.get('lines')]
            # Some producers merge the two visible paragraphs into a single PDF block.
            merged={**original[0],'bbox':tuple(fitz.Rect(40,35,300,180)),
                    'lines':[line for block in original for line in block['lines']]}
            from types import SimpleNamespace
            blocks=native_paragraphs(SimpleNamespace(rect=p.rect,get_text=lambda *a:{'blocks':[merged]}))
            self.assertEqual(len(blocks),2)
            text=lambda bs:''.join(c['c'] for b in bs for l in b['lines'] for s in l['spans'] for c in s['chars'])
            self.assertEqual(text(blocks),text(original))

    def test_chinese_font_aliases_are_equivalent_but_math_scripts_are_not(self):
        from rmb.direct_pdf import prose_counter
        self.assertEqual(prose_counter('策略进行了'),prose_counter('策略进行了'))
        self.assertEqual(prose_counter('IV-D'),prose_counter('IV‑D'))
        self.assertNotEqual(prose_counter('x²'),prose_counter('x2'))
        self.assertNotEqual(prose_counter('ℝ'),prose_counter('R'))

if __name__=='__main__':unittest.main()
