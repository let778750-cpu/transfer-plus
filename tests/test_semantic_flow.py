import json,sys,tempfile,unittest
from pathlib import Path
import pymupdf as fitz
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.native_flow import semantic_native
from rmb.cloud_layout import inventory_native,raster_units
from rmb.cloud_pipeline import assemble,recognize_crops,deduplicate_raster
from rmb.cloud_pipeline import typeset_raster,translate_units
from rmb.reading import good_translation,MARKER
from rmb.config import DEFAULTS

class SemanticFlowTests(unittest.TestCase):
    def test_echoed_chart_token_remains_unfinished_without_endless_retry(self):
        units=[{'id':str(i),'source':source,'kind':kind,'objects':[],'status':'missing'}
               for i,(source,kind) in enumerate([('Wq','raster'),('Priority','raster'),
                   ('Value','raster'),('Wk','native'),('Satellite scheduling','raster')])]
        class Translator:
            def __init__(self):self.records=[]
            def translate_batch_checked(self,rows,retry=False):
                result={}
                for r in rows:
                    src=r['input'];out='值' if src=='Value' else src
                    issue='model_request_failed:Timeout' if src=='Priority' else 'reading_content_incomplete'
                    self.records.append({'source':src,'translation':'' if src=='Priority' else out,'issue':issue})
                    result[r['id']]=out
                return result
        translate_units(units,Translator(),[],lambda *a:None,lambda:None)
        self.assertEqual(units[0]['status'],'missing')
        self.assertEqual(units[0]['reason'],'unresolved_ocr_label')
        self.assertNotIn('translation',units[0])
        self.assertEqual(units[1]['reason'],'translation_incomplete')
        self.assertEqual(units[2]['translation'],'值')
        self.assertEqual(units[3]['reason'],'translation_incomplete')
        self.assertEqual(units[4]['reason'],'translation_incomplete')

    def test_tight_algorithm_rows_translate_without_moving_math(self):
        with tempfile.TemporaryDirectory() as t,fitz.open() as d:
            p=d.new_page(width=350,height=200)
            p.insert_text((40,60),'Initialize all network parameters',fontsize=10)
            p.insert_text((40,72),'Set learning rate',fontsize=10)
            p.insert_text((150,72),'x',fontname='tiit',fontsize=10)
            regions=[{'id':0,'label':'algorithm','region':[35,45,290,82]}]
            units=semantic_native(p,regions,inventory_native(p,regions))
            for u in units:
                if u['status']=='missing':u['translation']='初始化所有网络参数' if 'Initialize' in u['source'] else '设置学习率'
            box=fitz.Rect(149,61,157,76);before=p.get_pixmap(clip=box,matrix=fitz.Matrix(3,3)).samples
            source=Path(t)/'a.pdf';target=Path(t)/'b.pdf';d.save(source)
            assemble(source,target,{'units':units,'layouts':[]},DEFAULTS,lambda*a:None,lambda:None)
            self.assertTrue(all(u['status']!='missing' for u in units))
            with fitz.open(target) as after:
                self.assertIn('初始化',after[0].get_text())
                self.assertEqual(before,after[0].get_pixmap(clip=box,matrix=fitz.Matrix(3,3)).samples)

    def test_formula_cover_keeps_background_image_and_unrelated_rule(self):
        from PIL import Image
        import io
        for background in ('image','rule'):
            with tempfile.TemporaryDirectory() as t,fitz.open() as d:
                p=d.new_page(width=400,height=240)
                if background=='image':
                    stream=io.BytesIO();Image.new('RGB',(400,240),(216,229,255)).save(stream,format='PNG')
                    p.insert_image(p.rect,stream=stream.getvalue())
                else:p.draw_line((20,43.9),(350,43.9),color=(1,0,0),width=.3)
                p.insert_text((40,60),'x represents the observed satellite resource availability.',fontsize=10)
                for a,b in [((36,53),(39,58)),((39,58),(42,45)),((42,45),(48,45))]:p.draw_line(a,b)
                rs=[{'id':0,'label':'text','region':[30,40,360,75]}]
                u=semantic_native(p,rs,inventory_native(p,rs))[0]
                u['translation']='所观测的卫星资源可用性由这个变量表示：{v0}。'
                source=Path(t)/'source.pdf';target=Path(t)/'out.pdf';d.save(source)
                before=p.get_pixmap(matrix=fitz.Matrix(3,3)).samples
                assemble(source,target,{'units':[u],'layouts':[]},DEFAULTS,lambda*a:None,lambda:None)
                self.assertEqual(u['status'],'missing')
                with fitz.open(target) as after:self.assertEqual(before,after[0].get_pixmap(matrix=fitz.Matrix(3,3)).samples)

    def test_root_path_outside_text_bbox_is_bound_or_rejected(self):
        with tempfile.TemporaryDirectory() as t,fitz.open() as d:
            p=d.new_page(width=400,height=240)
            p.insert_text((40,60),'x represents the observed satellite resource availability.',fontsize=10)
            for a,b in [((36,53),(39,58)),((39,58),(42,45)),((42,45),(48,45))]:p.draw_line(a,b)
            rs=[{'id':0,'label':'text','region':[30,40,360,75]}]
            u=semantic_native(p,rs,inventory_native(p,rs))[0]
            if u['completeMath']:
                box=fitz.Rect(u['objects'][0]['box'])
                self.assertTrue(box.contains(fitz.Rect(36,45,48,58)))
                self.assertTrue(u['removeFormulaPaths'])
                u['translation']='所观测的卫星资源可用性由这个变量表示：{v0}。'
                source=Path(t)/'root.pdf';target=Path(t)/'out.pdf';d.save(source)
                assemble(source,target,{'units':[u],'layouts':[]},DEFAULTS,lambda *a:None,lambda:None)
                self.assertEqual(u['status'],'pdf')
                with fitz.open(target) as after:
                    self.assertEqual(set(after[0].get_pixmap(clip=fitz.Rect(35,44,49,59)).samples),{255})
                    self.assertGreaterEqual(len(after[0].get_drawings()),3)

    def test_raster_label_keeps_internal_legend_symbol(self):
        for raster,color in ((False,(1,0,0)),(True,(1,0,0)),(True,(0,0,0))):
            with fitz.open() as d:
                p=d.new_page(width=250,height=200);p.insert_text((40,80),'Satellite',fontsize=10)
                p.draw_circle((90,74),3,color=color,fill=color)
                if raster:
                    pix=p.get_pixmap();d.delete_page(0);p=d.new_page(width=250,height=200);p.insert_image(p.rect,stream=pix.tobytes('png'))
                before=p.get_pixmap().samples
                u={'region':[36,62,110,88],'fontSize':10,'translation':'卫星','objects':[]}
                self.assertIsNone(typeset_raster(d,0,u,[]));self.assertEqual(before,d[0].get_pixmap().samples)

    def test_citation_at_sentence_end_does_not_exclude_prose(self):
        with fitz.open() as d:
            p=d.new_page(width=600,height=400)
            p.insert_text((40,200),'This method improves satellite scheduling using the framework proposed by Smith et al.',fontsize=10)
            rs=[{'id':0,'label':'text','region':[30,180,550,220]}]
            u=semantic_native(p,rs,inventory_native(p,rs))[0]
            self.assertEqual(u['status'],'missing');self.assertGreater(u['words'],10)

    def test_detached_subscript_and_path_operator_belong_to_one_paragraph(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);source=root/'source.pdf'
            with fitz.open() as d:
                p=d.new_page(width=350,height=250)
                p.insert_text((35,60),'The value',fontsize=10)
                p.insert_text((117,60),'controls the scheduling process.',fontsize=10)
                # Emit math after the prose, as publisher PDFs commonly do.
                p.insert_text((80,60),'x',fontname='tiit',fontsize=10)
                p.insert_text((85,63),'i',fontname='tiit',fontsize=6)
                p.draw_line((92,55),(100,55),width=.6);p.draw_line((92,58),(100,58),width=.6)
                p.insert_text((103,60),'1',fontsize=10)
                regions=[{'id':0,'label':'text','region':[30,45,300,70]}]
                units=semantic_native(p,regions,inventory_native(p,regions))
                self.assertEqual(len(units),1);u=units[0]
                self.assertTrue(u['completeMath']);self.assertTrue(u['removeFormulaPaths'])
                self.assertEqual(u['source'].count('scheduling'),1)
                self.assertNotIn('xi',MARKER.sub('',u['source']))
                u['translation']='数值 '+ ' '.join('{v'+n+'}' for n in MARKER.findall(u['source']))+' 控制调度过程。'
                d.save(source)
            assemble(source,root/'result.pdf',{'units':units,'layouts':[{'page':1,'regions':regions}]},DEFAULTS,lambda *a:None,lambda:None)
            self.assertEqual(u['status'],'pdf')
            with fitz.open(root/'result.pdf') as d:
                self.assertIn('调度',d[0].get_text());self.assertIn('i',d[0].get_text())
                # The original position of the path-painted '=' must be blank.
                self.assertEqual(set(d[0].get_pixmap(clip=fitz.Rect(92,54,100,59),alpha=False).samples),{255})

    def test_short_chinese_is_valid_but_echoed_english_is_not(self):
        self.assertTrue(good_translation('Value','值',[]))
        source='The scheduling method controls every satellite in this experiment.'
        self.assertFalse(good_translation(source,source+'该方法控制本实验中的每颗卫星。',[]))

    def test_scientific_ticks_and_panel_ids_do_not_block_labels(self):
        with fitz.open() as d:
            p=d.new_page()
            raw={'width':100,'height':30,'rec_texts':['(b) Priority Reward'],
                'rec_polys':[[[1,1],[99,1],[99,29],[1,29]]],'rec_scores':[.99]}
            u=raster_units(p,[0,0,100,30],raw,'image')[0]
            self.assertNotIn('reason',u);self.assertIn('Priority Reward',u['source'])
            raw['rec_texts']=['1e6'];u=raster_units(p,[0,0,100,30],raw,'image')[0]
            self.assertEqual(u['status'],'preserved')

    def test_overlapping_subfigure_ocr_has_one_owner(self):
        base={'kind':'raster','page':1,'source':'Priority','objects':[],'region':[1,1,30,10]}
        result=deduplicate_raster([base,{**base,'region':[1.1,1.1,30.1,10.1]}])
        self.assertEqual(len(result),1);self.assertEqual(result[0]['ocrObservations'],2)

    def test_crop_cache_survives_batch_reordering_without_network(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);crops=[]
            for i in range(2):
                with fitz.open() as d:
                    p=d.new_page(width=50,height=30);p.insert_text((5,15),str(i),fontsize=10)
                    file=root/f'crop{i}.png';p.get_pixmap().save(file)
                crops.append({'path':file,'page':0,'region':[0,0,50,30]})
            class OCR:
                calls=0
                def parse(self,file,model,count,options):
                    self.calls+=1
                    return [{'width':50,'height':30,'rec_texts':['Label'+str(n)]} for n in range(count)]
            client=OCR();first=list(recognize_crops(crops,client,root,lambda *a:None,lambda:None))
            second=list(recognize_crops(list(reversed(crops)),client,root,lambda *a:None,lambda:None))
            self.assertEqual(client.calls,1);self.assertEqual(second[0][1],first[1][1])

if __name__=='__main__':unittest.main()
