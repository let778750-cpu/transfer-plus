import json
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
import pymupdf as fitz
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.paddle_cloud import PaddleCloud,CloudError,Cancelled,parse_jsonl,response_data
from rmb.cloud_layout import cloud_regions,inventory_native,ocr_lines,unit,raster_units
from rmb.cloud_pipeline import run_pipeline,audit_archive,normalize,chunks,split_complex_units,assemble,refine_table_units
from rmb.config import DEFAULTS,sha256_file,config_fingerprint,atomic_json,ConfigStore

class Response:
    def __init__(self,value,status=200):self.value=value;self.status_code=status;self.text=json.dumps(value)
    def json(self):return self.value
    def raise_for_status(self):
        if self.status_code>=400:raise AssertionError('unexpected HTTP error')

class CloudTests(unittest.TestCase):
    def test_complete_pages_and_order_required(self):
        def row(n):return json.dumps({'result':{'layoutParsingResults':[{'prunedResult':{'page_index':n}}]}})
        self.assertEqual([p['page_index'] for p in parse_jsonl(row(1)+'\n'+row(0),2)],[0,1])
        for data,count in ((row(0),2),(row(0)+'\n'+row(0),2),(row(3),1)):
            with self.assertRaises(CloudError):parse_jsonl(data,count)
    def test_quota_and_credential_errors_are_sanitized(self):
        for code,status in [(12001,403),(0,401),(12002,429)]:
            with self.assertRaises(CloudError) as e:response_data(Response({'code':code,'msg':'PRIVATE TOKEN AND BODY'},status))
            self.assertNotIn('PRIVATE',str(e.exception))
    def test_remote_id_survives_cancel_and_result_cache_never_posts(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);source=root/'test.pdf';source.write_bytes(b'%PDF-test');calls=[];cancel=[False]
            class HTTP:
                def post(self,url,**kwargs):
                    calls.append('post');cancel[0]=True
                    return Response({'code':0,'data':{'jobId':'cloud-one'}})
                def get(self,url,**kwargs):
                    calls.append('get')
                    if url.endswith('cloud-one'):
                        return Response({'code':0,'data':{'state':'done','resultUrl':{'jsonUrl':'https://storage.example/result'}}})
                    self_outer.assertNotIn('Authorization',kwargs.get('headers',{}))
                    r=Response({});r.text=json.dumps({'result':{'layoutParsingResults':[{'prunedResult':{'page_index':0,'width':600,'height':800,'parsing_res_list':[]}}]}});return r
            self_outer=self
            client=PaddleCloud('test-token',root/'cache',session=HTTP(),cancelled=lambda:cancel[0])
            with self.assertRaises(Cancelled):client.parse(source,'PaddleOCR-VL-1.6',1)
            self.assertEqual(len(list((root/'cache').glob('*/remote.json'))),1)
            cancel[0]=False;client.parse(source,'PaddleOCR-VL-1.6',1);client.parse(source,'PaddleOCR-VL-1.6',1)
            self.assertEqual(calls,['post','get','get'])
    def test_ambiguous_upload_recovers_batch_before_post(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);file=root/'paper.pdf';file.write_bytes(b'%PDF-test')
            import requests
            class HTTP:
                def post(self,*a,**kw):raise requests.Timeout()
                def get(self,*a,**kw):return Response({'code':0,'data':{'extractResult':[]}})
            client=PaddleCloud('test',root/'cache',session=HTTP())
            with self.assertRaisesRegex(CloudError,'上传响应'):client.parse(file,'test',1)
            with patch.object(client.http,'post',side_effect=AssertionError('duplicate upload')):
                with self.assertRaisesRegex(CloudError,'未重复上传'):client.parse(file,'test',1)
    def test_coordinate_dimensions_and_cell_labels(self):
        with fitz.open() as d:
            p=d.new_page(width=300,height=400);p.insert_text((30,50),'Normal italic prose',fontname='Times-Italic')
            r=cloud_regions({'width':600,'height':800,'parsing_res_list':[{'block_bbox':[40,50,550,120],'block_label':'table','block_content':'text'}]},p)
            units=inventory_native(p,r);self.assertTrue(units);self.assertTrue(all(u['status']=='missing' for u in units))
            self.assertIn('Normal italic prose',units[0]['source'])
            with self.assertRaises(ValueError):cloud_regions({'parsing_res_list':[{'block_bbox':[0,0,10,10]}]},p)
    def test_connect_timeout_and_expired_job_can_resume(self):
        import requests
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);file=root/'source.pdf';file.write_bytes(b'%PDF-test')
            class HTTP:
                calls=0
                def post(self,*a,**kw):
                    self.calls+=1
                    if self.calls==1:raise requests.ConnectTimeout()
                    return Response({'code':0,'data':{'jobId':'expired'}})
                def get(self,*a,**kw):return Response({'code':11002},404)
            http=HTTP();client=PaddleCloud('fake',root/'cache',session=http)
            with self.assertRaisesRegex(CloudError,'尚未建立'):client.parse(file,'test',1)
            with self.assertRaisesRegex(CloudError,'已失效'):client.parse(file,'test',1)
            self.assertEqual(http.calls,2)
            state=json.loads(next((root/'cache').glob('*/remote.json')).read_text())
            self.assertFalse(state['submitting']);self.assertIsNone(state['jobId'])
    def test_ocr_crop_coordinates(self):
        result=ocr_lines({'width':200,'height':100,'rec_texts':['Caption'],'rec_polys':[[[10,10],[190,10],[190,30],[10,30]]],'rec_scores':[.99]},[100,200,200,250])
        self.assertEqual(result[0]['region'],[105,205,195,215]);self.assertFalse(result[0]['rotated'])
    def test_two_keys_are_separate_and_not_returned(self):
        with tempfile.TemporaryDirectory() as t:
            store=ConfigStore(Path(t));v=store.update({'apiKey':'translation-fixture','paddleApiKey':'ocr-fixture'})
            self.assertTrue(v['hasPaddleApiKey']);self.assertEqual(store.ocr_secret(),'ocr-fixture')
            store.update({'paddleApiKey':''});self.assertEqual(store.secret(),'translation-fixture')
            for p in (store.path,store.secret_path,store.ocr_secret_path):
                self.assertNotIn(b'ocr-fixture',p.read_bytes());self.assertNotIn(b'translation-fixture',p.read_bytes())

class PipelineTests(unittest.TestCase):
    def test_partial_or_clipping_cells_never_remove_native_lines(self):
        with fitz.open() as d:
            p=d.new_page(width=300,height=300)
            p.insert_text((40,50),'First native row',fontsize=10)
            p.insert_text((40,65),'Second native row',fontsize=10)
            units=inventory_native(p,[{'id':0,'label':'table','region':[30,30,270,150]}])
            for bottom in (55,58):
                result=refine_table_units(p,units,[[30,30,270,bottom]])
                self.assertIn('Second native row',' '.join(u['source'] for u in result))
                self.assertEqual(sum(u['words'] for u in result),sum(u['words'] for u in units))

    def test_complex_fraction_survives_prose_expansion(self):
        from rmb.reading import union,complex_math
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);source=root/'source.pdf'
            with fitz.open() as d:
                p=d.new_page(width=300,height=300)
                p.insert_text((40,50),'The complex formula below is important',fontsize=10)
                p.insert_text((80,67),'x',fontname='tiit',fontsize=10)
                p.insert_text((80,82),'y',fontname='tiit',fontsize=10)
                p.draw_line((77,70),(89,70))
                p.insert_text((40,104),'Further explanation is shown here',fontsize=10)
                lines=[l for b in p.get_text('rawdict')['blocks'] for l in b.get('lines',[])]
                u=unit(p,{'lines':lines,'bbox':list(union([l['bbox'] for l in lines]))},'text')
                self.assertTrue(complex_math(u['objects'],p))
                units=split_complex_units(p,[u]);self.assertTrue(any(v.get('immutable') for v in units))
                next(v for v in units if v['status']=='missing')['translation']='下面展示的复杂公式对于卫星任务的调度与资源配置非常重要。'
                d.save(source)
            assemble(source,root/'result.pdf',{'units':units,'layouts':[]},DEFAULTS,lambda *a:None,lambda:None)
            with fitz.open(source) as a,fitz.open(root/'result.pdf') as b:
                self.assertIn('x',b[0].get_text());self.assertIn('y',b[0].get_text())
                box=fitz.Rect(75,55,95,88)
                self.assertEqual(a[0].get_pixmap(clip=box).samples,b[0].get_pixmap(clip=box).samples)

    def test_own_simple_inline_formula_does_not_block_paragraph(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);source=root/'source.pdf'
            with fitz.open() as d:
                p=d.new_page(width=500,height=300);p.insert_text((40,60),'The value x controls the orbital scheduling process.',fontsize=11)
                units=inventory_native(p,[]);u=units[0];u['translation']='数值 {v0} 控制轨道调度过程。'
                regions=[{'label':'inline_formula','region':u['objects'][0]['box']}]
                d.save(source)
            assemble(source,root/'result.pdf',{'units':units,'layouts':[{'page':1,'regions':regions}]},DEFAULTS,lambda *a:None,lambda:None)
            self.assertEqual(u['status'],'pdf')

    def test_raster_formula_not_retyped(self):
        with fitz.open() as d:
            p=d.new_page();units=raster_units(p,[0,0,100,30],{'width':100,'height':30,
                'rec_texts':['The value x_i equals 5 and y_j grows.'],'rec_polys':[[[0,0],[100,0],[100,30],[0,30]]]},'text')
            self.assertEqual(units[0]['reason'],'ocr_math_alignment')
    def fixture(self,root):
        source=root/'source.pdf'
        with fitz.open() as d:
            p=d.new_page(width=500,height=400)
            p.insert_text((35,60),'Reliable satellite scheduling',fontsize=16)
            p.insert_textbox(fitz.Rect(35,100,460,180),'This method schedules observation tasks using available satellite resources. The constraints describe timing and resource allocation.',fontsize=12)
            p.insert_text((35,250),'Variables',fontsize=12,fontname='Times-Italic')
            d.save(source)
        return source
    @unittest.skip('0.3 改善流（audit/修订链）语义已被 0.4 一键语义取代：引擎级缓存驱动重跑；遗留代码保留至独立仓库抽取后移除')
    def test_full_pipeline_and_second_click_are_offline(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);job=root/'job';job.mkdir();data=root/'data';data.mkdir();source=self.fixture(root)
            class OCR:
                calls=0
                def parse(self,file,model,count,options):
                    self.calls+=1
                    with fitz.open(file) as doc:
                        return [{'page_index':i,'width':p.rect.width,'height':p.rect.height,'parsing_res_list':[
                            {'block_bbox':b['bbox'],'block_label':'text','block_content':' '.join(s['text'] for l in b['lines'] for s in l['spans'])}
                            for b in p.get_text('dict')['blocks'] if 'lines' in b]} for i,p in enumerate(doc)]
            class Translator:
                calls=0
                def translate_checked(self,text,validator,retry=False):
                    self.calls+=1
                    return {'Reliable satellite scheduling':'可靠的卫星调度方法','Variables':'变量说明'}.get(text,'该方法利用可用的卫星资源安排观测任务。约束条件描述了时序关系和资源分配方式。')
            state={'sourceHash':sha256_file(source),'configFingerprint':config_fingerprint(DEFAULTS)}
            ocr=OCR();translator=Translator()
            report=run_pipeline(source,job,data,state,DEFAULTS,ocr,translator,lambda *a:None,lambda:None)
            self.assertEqual(report['coverage']['missing'],0)
            self.assertFalse(audit_archive(source,job/'result.pdf',report)['needsWork'])
            second=root/'second';second.mkdir()
            run_pipeline(source,second,data,state,DEFAULTS,ocr,translator,lambda *a:None,lambda:None,[report])
            self.assertEqual(ocr.calls,1);self.assertEqual(translator.calls,3)
            self.assertEqual(sha256_file(source),state['sourceHash'])
    def test_rotation_and_batch_boundaries(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);source=root/'source.pdf';normalized=root/'normalized.pdf'
            with fitz.open() as d:
                for _ in range(21):p=d.new_page(width=300,height=400);p.insert_text((30,50),'Heading')
                d[0].set_rotation(90);d.save(source)
            normalize(source,normalized)
            with fitz.open(normalized) as d:
                self.assertEqual(d[0].rotation,0);self.assertEqual(d[0].rect,fitz.Rect(0,0,400,300))
                parts=list(chunks(d,list(range(21)),root/'chunks'));self.assertEqual([len(x[0]) for x in parts],[20,1])

    @unittest.skip('0.3 改善流（audit/修订链）语义已被 0.4 一键语义取代：引擎级缓存驱动重跑；遗留代码保留至独立仓库抽取后移除')
    def test_empty_scan_is_actually_recognized_on_next_click(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);data=root/'data';data.mkdir();source=root/'scan.pdf'
            with fitz.open() as d:
                p=d.new_page(width=300,height=150);p.insert_text((30,70),'Satellite scheduling',fontsize=16)
                pix=p.get_pixmap();d.delete_page(0);p=d.new_page(width=300,height=150)
                p.insert_image(p.rect,stream=pix.tobytes('png'));d.save(source)
            class OCR:
                calls=0
                def parse(self,file,model,count,options):
                    self.calls+=1
                    with fitz.open(file) as d:w,h=d[0].rect.width*2,d[0].rect.height*2
                    if model!='PP-OCRv6':return [{'width':w,'height':h,'parsing_res_list':[]}]
                    return [{'width':w,'height':h,'rec_texts':[] if self.calls<4 else ['Satellite scheduling'],
                        'rec_polys':[] if self.calls<4 else [[[58,105],[350,105],[350,145],[58,145]]],'rec_scores':[] if self.calls<4 else [.99]}]
            class Translator:
                def translate_checked(self,*a,**k):return '卫星任务调度'
            state={'sourceHash':sha256_file(source),'configFingerprint':config_fingerprint(DEFAULTS)}
            ocr=OCR();first=root/'first';first.mkdir();second=root/'second';second.mkdir()
            r=run_pipeline(source,first,data,state,DEFAULTS,ocr,Translator(),lambda*a:None,lambda:None)
            self.assertEqual(r['issues'][0]['type'],'recognition_empty')
            r=run_pipeline(source,second,data,state,DEFAULTS,ocr,Translator(),lambda*a:None,lambda:None,[r])
            self.assertGreater(ocr.calls,2)
            self.assertFalse(any(u.get('reason')=='recognition_empty' for u in r['reading']['units']))

if __name__=='__main__':unittest.main()
