"""Regression cases for the real submission-stage busy response."""
import json
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import requests

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.paddle_cloud import PaddleCloud,CloudError,Cancelled,response_data

class Response:
    def __init__(self,value,status=200,headers=None):
        self.value=value;self.status_code=status;self.headers=headers or {}
    def json(self):return self.value

class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.file=self.root/'source.pdf';self.file.write_bytes(b'%PDF-fixture')
        self.state={'batchId':'one-stable-batch'};self.state_file=self.root/'remote.json'
        self.http=Mock();self.progress=Mock()
        self.client=PaddleCloud('DO-NOT-LOG',self.root/'cache',session=self.http,progress=self.progress)
        self.client.wait=Mock()
    def submit(self):self.client.submit(self.file,'PaddleOCR-VL-1.6',{},self.state,self.state_file)
    def test_queue_then_throttle_then_success_same_batch_and_reopened_upload(self):
        responses=[Response({'code':'10010'},200),Response({'code':12002},429,{'Retry-After':'12'}),
                   Response({'code':0,'data':{'jobId':'one-job'}})]
        def post(*a,**kw):
            self.assertEqual(kw['data']['batchId'],'one-stable-batch')
            self.assertEqual(kw['files']['file'][1].read(),b'%PDF-fixture')
            return responses.pop(0)
        self.http.post.side_effect=post;self.submit()
        self.assertEqual(self.http.post.call_count,3);self.assertEqual(self.state['jobId'],'one-job')
        self.assertFalse(self.state['submitting']);self.assertNotIn('lastError',self.state)
        self.assertEqual([c.args[0] for c in self.client.wait.call_args_list],[5,12])
    def test_busy_budget_exhaustion_persists_reason_and_allows_resume(self):
        self.http.post.return_value=Response({'code':10010,'msg':'PRIVATE CONTENT'},200)
        with self.assertRaisesRegex(CloudError,'自动重试已暂停'):self.submit()
        self.assertEqual(self.http.post.call_count,5)
        saved=json.loads(self.state_file.read_text());self.assertFalse(saved['submitting'])
        self.assertEqual(saved['lastError']['code'],10010)
        self.assertNotIn('PRIVATE',self.state_file.read_text());self.assertNotIn('DO-NOT-LOG',self.state_file.read_text())
        self.http.post.return_value=Response({'code':0,'data':{'jobId':'resume'}})
        self.submit();self.assertEqual(self.state['jobId'],'resume')
    def test_cancel_during_backoff_stops_submission(self):
        self.http.post.return_value=Response({'code':10010})
        self.client.wait.side_effect=Cancelled()
        with self.assertRaises(Cancelled):self.submit()
        self.assertEqual(self.http.post.call_count,1);self.assertFalse(self.state['submitting'])
    def test_quota_and_bad_parameters_never_auto_retry(self):
        for code,status in ((12001,403),(10008,400),(401,401)):
            self.http.reset_mock();self.http.post.return_value=Response({'code':code},status)
            with self.assertRaises(CloudError) as c:self.submit()
            self.assertEqual(c.exception.code,code);self.assertEqual(c.exception.http_status,status)
            self.assertEqual(self.http.post.call_count,1);self.client.wait.assert_not_called()
    def test_unknown_429_does_not_claim_daily_quota_or_retry(self):
        with self.assertRaises(CloudError) as c:response_data(Response({},429))
        self.assertNotIn('用尽',str(c.exception));self.assertFalse(c.exception.retryable)
    def test_http200_application_error_get_is_retried(self):
        self.http.get.side_effect=[Response({'errorCode':10010}),Response({'code':0,'data':{'state':'pending'}})]
        self.assertEqual(self.client.get('https://example.test')['state'],'pending')
        self.assertEqual(self.http.get.call_count,2)
    def test_server_failure_reconciles_without_duplicate_upload(self):
        self.http.post.return_value=Response({},503)
        self.http.get.return_value=Response({'code':0,'data':{'extractResult':[{'jobId':'already-created'}]}})
        self.submit();self.assertEqual(self.http.post.call_count,1)
        self.assertEqual(self.state['jobId'],'already-created')
    def test_ambiguous_server_failure_or_malformed_success_stays_uncertain(self):
        for response in (Response({},503),Response({'code':0,'data':{}}),Response([])):
            self.state={'batchId':'test'};self.http.reset_mock();self.http.post.return_value=response
            self.http.get.return_value=Response({'code':0,'data':{'extractResult':[]}})
            with self.assertRaises(CloudError):self.submit()
            self.assertTrue(self.state['submitting']);self.assertEqual(self.http.post.call_count,1)
    def test_read_timeout_does_not_repost(self):
        self.http.post.side_effect=requests.ReadTimeout('SECRET')
        with self.assertRaisesRegex(CloudError,'先查询'):self.submit()
        self.assertTrue(self.state['submitting']);self.assertEqual(self.http.post.call_count,1)
    def test_long_retry_after_pauses_without_early_request(self):
        self.http.post.return_value=Response({'code':12002},429,{'Retry-After':'3600'})
        with self.assertRaisesRegex(CloudError,'已暂停'):self.submit()
        self.assertEqual(self.http.post.call_count,1);self.client.wait.assert_not_called()

    def test_malformed_batch_results_never_clear_uncertain_submission(self):
        self.http.post.side_effect=requests.ReadTimeout()
        with self.assertRaises(CloudError):self.client.parse(self.file,'test',1)
        saved=next((self.root/'cache').glob('*/remote.json'))
        state=json.loads(saved.read_text());state['submittedAt']=time.time()-120
        saved.write_text(json.dumps(state))
        for data in ({},{'extractResult':{}},{'extractResult':[{}]},
                     {'extractResult':[{'jobId':None}]},{'extractResult':['bad']},
                     {'extractResult':[],'batchId':'wrong'}):
            self.http.get.return_value=Response({'code':0,'data':data})
            with self.assertRaises(CloudError):self.client.parse(self.file,'test',1)
            self.assertTrue(json.loads(saved.read_text())['submitting'])
        self.assertEqual(self.http.post.call_count,1)
        # Only valid, explicit empty results can accumulate absence evidence.
        self.http.get.return_value=Response({'code':0,'data':{'extractResult':[]}})
        for _ in range(3):
            with self.assertRaises(CloudError):self.client.parse(self.file,'test',1)
        self.assertFalse(json.loads(saved.read_text())['submitting'])
        self.assertEqual(self.http.post.call_count,1)

if __name__=='__main__':unittest.main()
