import tempfile,sys,unittest
from pathlib import Path
import pymupdf as fitz
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.cloud_pipeline import recognize_crops
from rmb.paddle_cloud import CloudError

class CropTests(unittest.TestCase):
    def test_crop_mapping_survives_batches_and_cached_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);items=[]
            for i in range(21):
                with fitz.open() as d:
                    p=d.new_page(width=120+i,height=80);p.insert_text((10,40),'Caption')
                    path=root/f'{i}.png';p.get_pixmap().save(path)
                items.append({'path':path,'page':i,'region':[i,0,120+i,80]})
            calls=[]
            class OCR:
                def parse(self,file,model,count,options):
                    calls.append(count)
                    with fitz.open(file) as d:
                        return [{'width':p.rect.width*2,'height':p.rect.height*2} for p in d]
            for _ in range(2):
                results=list(recognize_crops(items,OCR(),root,lambda *a:None,lambda:None))
                self.assertEqual([item['page'] for item,_ in results],list(range(21)))
                self.assertEqual([raw['width'] for _,raw in results],list(range(120,141)))
            self.assertEqual(calls,[20,1])
    def test_missing_crop_page_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'a.png'
            with fitz.open() as d:d.new_page(width=100,height=100).get_pixmap().save(path)
            class Bad:
                def parse(self,*a):return []
            with self.assertRaises(CloudError):list(recognize_crops([{'path':path}],Bad(),root,lambda *a:None,lambda:None))

if __name__=='__main__':unittest.main()
