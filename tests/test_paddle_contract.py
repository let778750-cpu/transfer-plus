"""Offline fixtures from the documented async/serving response contracts.

No credentials, real documents, OCR uploads or translation calls.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from rmb.paddle_cloud import PaddleCloud, CloudError, RESULT_SCHEMA, parse_jsonl
from rmb.cloud_layout import ocr_lines, cloud_regions
import pymupdf as fitz


def line(pages, info=None, kind='layoutParsingResults'):
    result = {kind: [{'prunedResult': page} for page in pages]}
    if info is not None:
        result['dataInfo'] = info
    return json.dumps({'errorCode': 0, 'result': result})


class Response:
    status_code = 200
    headers = {}
    def __init__(self, value):
        self.value = value
        self.text = json.dumps(value)
    def json(self): return self.value
    def raise_for_status(self): pass


class ContractTests(unittest.TestCase):
    def test_pruned_pdf_pages_use_document_order_and_per_page_dimensions(self):
        payload = line([
            {'parsing_res_list': [{'block_bbox': [20,40,580,100], 'block_content': 'First'}]},
            {'parsing_res_list': [{'block_bbox': [20,40,780,100], 'block_content': 'Second'}]},
        ], {'type':'pdf', 'numPages':2, 'pages':[{'width':600,'height':800}, {'width':800,'height':600}]})
        pages = parse_jsonl(payload, 2)
        self.assertEqual([p['page_index'] for p in pages], [0,1])
        self.assertEqual([p['width'] for p in pages], [600,800])
        with fitz.open() as doc:
            page = doc.new_page(width=400, height=300)
            self.assertEqual(cloud_regions(pages[1], page)[0]['region'], [10,20,390,50])

    def test_ocr_image_metadata_reaches_crop_coordinate_mapping(self):
        page = parse_jsonl(line([{
            'rec_texts':['Caption'], 'rec_scores':[.99],
            'rec_polys':[[[10,10],[190,10],[190,30],[10,30]]],
        }], {'type':'image','width':200,'height':100}, 'ocrResults'), 1)[0]
        self.assertEqual(ocr_lines(page, [100,200,200,250])[0]['region'], [105,205,195,215])

    def test_jsonl_parts_support_local_or_full_metadata_without_restarting_page_numbers(self):
        dimensions = [{'width':600,'height':800}, {'width':800,'height':600}]
        for full_metadata in (False, True):
            payload = '\n'.join(line([{'parsing_res_list':[]}], {
                'type':'pdf', 'numPages':2 if full_metadata else 1,
                'pages':dimensions if full_metadata else [dimension],
            }) for dimension in dimensions)
            pages = parse_jsonl(payload,2)
            self.assertEqual([p['page_index'] for p in pages], [0,1])
            self.assertEqual([p['width'] for p in pages], [600,800])

    def test_explicit_indices_still_sorted_and_checked(self):
        self.assertEqual([p['name'] for p in parse_jsonl(line([
            {'page_index':1,'name':'second'}, {'page_index':0,'name':'first'}]), 2)], ['first','second'])
        for pages in ([{'page_index':0}, {}], [{'page_index':0}, {'page_index':0}],
                      [{'page_index':False}], [{'page_index':-1}]):
            with self.assertRaises(CloudError): parse_jsonl(line(pages), len(pages))

    def test_incomplete_or_malformed_results_are_never_silently_accepted(self):
        for payload, count in (
            (line([{}]),2),
            (line([{}], {'type':'pdf','numPages':2,'pages':[{'width':100,'height':100}]}),1),
            (line([{}], {'type':'pdf','pages':[]}),1),
            (line([{}], {'width':float('nan'),'height':100}),1),
            (line([{}], {'width':0,'height':100}),1),
            (line([{}], {'width':True,'height':100}),1),
            (json.dumps({'errorCode':11003,'result':{'layoutParsingResults':[{'prunedResult':{}}]}}),1),
            ('PRIVATE SIGNED URL or TOKEN is not JSON',1),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(CloudError) as error: parse_jsonl(payload,count)
                self.assertNotIn('PRIVATE',str(error.exception))

    def test_raw_coordinate_dimensions_take_precedence(self):
        page = parse_jsonl(line([{'width':200,'height':100}], {'width':400,'height':200}),1)[0]
        self.assertEqual((page['width'],page['height']), (200,100))

    def test_old_parsed_cache_recovers_remote_result_without_new_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.pdf';source.write_bytes(b'%PDF-fixture')
            http=Mock()
            http.post.return_value=Response({'code':0,'data':{'jobId':'existing'}})
            status=Response({'code':0,'data':{'state':'done','resultUrl':{'jsonUrl':'https://storage.example/result'}}})
            result=Response({});result.text=line([{}], {'width':200,'height':100})
            http.get.side_effect=[status,result,status,result]
            client=PaddleCloud('FAKE-TOKEN',root/'cache',session=http)
            client.parse(source,'PP-OCRv6',1)
            saved=next((root/'cache').glob('*/parsed.json'))
            old=json.loads(saved.read_text(encoding='utf-8'))
            old.pop('resultSchema');old['pages'][0].pop('width');old['pages'][0].pop('height')
            saved.write_text(json.dumps(old),encoding='utf-8')
            pages=client.parse(source,'PP-OCRv6',1)
            self.assertEqual(pages[0]['width'],200)
            self.assertEqual(http.post.call_count,1)
            self.assertEqual(json.loads(saved.read_text(encoding='utf-8'))['resultSchema'], RESULT_SCHEMA)
            client.parse(source,'PP-OCRv6',1)
            self.assertEqual(http.get.call_count,4)
            # Storage requests never receive the AI Studio bearer credential.
            for call in (http.get.call_args_list[1], http.get.call_args_list[3]):
                self.assertNotIn('Authorization',call.kwargs.get('headers',{}))

    def test_polling_uses_official_bounded_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.pdf';source.write_bytes(b'%PDF-fixture')
            http=Mock();http.post.return_value=Response({'code':0,'data':{'jobId':'existing'}})
            result=Response({});result.text=line([{}])
            http.get.side_effect=[Response({'code':0,'data':{'state':'pending'}})]*7+[
                Response({'code':0,'data':{'state':'done','resultUrl':{'jsonUrl':'https://storage.example/result'}}}),result]
            client=PaddleCloud('FAKE-TOKEN',root/'cache',session=http);client.wait=Mock()
            client.parse(source,'PaddleOCR-VL-1.6',1)
            self.assertEqual([c.args[0] for c in client.wait.call_args_list], [3,4.5,6.75,10.125,15,15,15])


if __name__=='__main__': unittest.main()
