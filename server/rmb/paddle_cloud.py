"""AI Studio async transport. No inference framework; durable remote job IDs.

Only explicit user tasks call this module. No token / signed result URL is logged.
"""
from __future__ import annotations
import hashlib
import json
import time
import math
from email.utils import parsedate_to_datetime
from pathlib import Path
import requests
from filelock import FileLock
from .config import atomic_json, sha256_file

JOBS_URL = 'https://paddleocr.aistudio-app.com/api/v2/ocr/jobs'
OCR_SCHEMA = 1
RESULT_SCHEMA = 2  # Separate from request identity: reuse existing remote jobs.

class CloudError(RuntimeError):
    def __init__(self, message, *, resumable=True, code=None, http_status=None, retryable=False, retry_after=None):
        self.user_message = message
        self.resumable = resumable
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(message)

    def diagnostic(self):
        # Never persist response bodies, authorization headers or signed URLs.
        return {'code':self.code,'httpStatus':self.http_status,'retryable':self.retryable}

class Cancelled(CloudError):
    def __init__(self):
        super().__init__('已取消，进度已保留；再次点击翻译即可继续')

def response_data(response):
    try: payload = response.json()
    except (ValueError, TypeError): payload = {}
    if not isinstance(payload,dict):payload={}
    code = payload.get('code', payload.get('errorCode', 0))
    try:code=int(code or 0)
    except (ValueError,TypeError):code=-1
    status=response.status_code
    detail={'code':code,'http_status':status,'retry_after':retry_after_seconds(response)}
    if code == 12001:
        raise CloudError('飞桨当日解析额度已用尽（12001），已保存进度，请额度恢复后继续',**detail)
    if code == 10010:
        raise CloudError('飞桨任务提交队列已满（10010）',retryable=True,**detail)
    if code == 12002:
        raise CloudError('飞桨请求频率受限（12002）',retryable=True,**detail)
    if status in (401,403) or code==401:
        raise CloudError('AI Studio 访问令牌无效或权限不足，请在中文对照设置中检查',**detail)
    if status==429:
        raise CloudError('飞桨返回 HTTP 429，限制类型未明确；已保存进度，请稍后继续',**detail)
    if status in (500,502,503,504) or code==500:
        raise CloudError('飞桨服务暂时异常',retryable=True,**detail)
    if response.status_code >= 400 or code:
        messages = {10001:'上传文件为空',10003:'文件超过飞桨上传限制',10004:'飞桨不支持此文件格式',
                    10005:'飞桨无法解析文件内容',10006:'文件超过飞桨页数限制',
                    10007:'飞桨模型参数不受支持',10008:'飞桨识别参数不受支持',
                    11001:'飞桨任务不存在',11002:'飞桨任务已过期',11003:'飞桨未能解析该文件'}
        raise CloudError(messages.get(code, '飞桨请求未成功，请检查连接后继续'),**detail)
    if not isinstance(payload.get('data'),dict):
        raise CloudError('飞桨响应格式异常，已保留本机进度',**detail)
    return payload['data']

def retry_after_seconds(response):
    value=getattr(response,'headers',{}).get('Retry-After')
    if not value:return None
    try:seconds=float(value)
    except (ValueError,TypeError):
        try:seconds=parsedate_to_datetime(value).timestamp()-time.time()
        except (ValueError,TypeError,OverflowError):return None
    return max(1,math.ceil(seconds)) if math.isfinite(seconds) else None

def parse_jsonl(text, expected_pages):
    """Normalize official pruned results, ordered pages and outer dataInfo.

    The serving API removes page_index; the official async example preserves
    JSONL/array order. Explicit indices, when supplied, remain strictly checked.
    """
    pages = []
    for line in text.splitlines():
        if not line.strip(): continue
        try:value = json.loads(line)
        except (ValueError,TypeError):raise CloudError('飞桨结果不是有效 JSONL，任务编号已保留') from None
        if not isinstance(value,dict):raise CloudError('飞桨页面结果格式异常')
        result = value.get('result', value)
        if not isinstance(result,dict):raise CloudError('飞桨页面结果格式异常')
        if value.get('errorCode',0) or result.get('errorCode',0):
            raise CloudError('飞桨页面解析返回错误，未生成不完整译文')
        items=result.get('layoutParsingResults', result.get('ocrResults'))
        if not isinstance(items,list):raise CloudError('飞桨结果缺少页面列表')
        info=result.get('dataInfo') or {}
        if not isinstance(info,dict):raise CloudError('飞桨页面尺寸元数据格式异常')
        metadata_pages=info.get('pages')
        if metadata_pages is not None and (not isinstance(metadata_pages,list) or
                len(metadata_pages) not in (len(items),expected_pages)):
            raise CloudError('飞桨页面尺寸元数据与页数不匹配')
        if info.get('numPages') is not None and info['numPages'] not in (len(items),expected_pages):
            raise CloudError('飞桨结果元数据页数不匹配，未生成不完整译文')
        offset=len(pages)
        for local_index,item in enumerate(items):
            if not isinstance(item,dict):raise CloudError('飞桨页面结果格式异常')
            raw = item.get('prunedResult')
            if not isinstance(raw, dict):
                raise CloudError('飞桨未返回页面结构坐标，未生成不完整译文')
            raw=dict(raw)
            if metadata_pages is not None:
                index=local_index if len(metadata_pages)==len(items) else raw.get('page_index',offset+local_index)
                if index is None:index=offset+local_index
                if type(index) is not int or not 0<=index<len(metadata_pages):
                    raise CloudError('飞桨页面尺寸与页序不匹配')
                dimensions=metadata_pages[index]
            else:dimensions=info
            if not isinstance(dimensions,dict):raise CloudError('飞桨页面尺寸元数据格式异常')
            for field in ('width','height'):
                if raw.get(field) is None and field in dimensions:raw[field]=dimensions[field]
                if field in raw and (type(raw[field]) not in (int,float) or
                        not math.isfinite(raw[field]) or raw[field]<=0):
                    raise CloudError('飞桨返回无效页面尺寸')
            pages.append(raw)
    if len(pages) != expected_pages:
        raise CloudError(f'飞桨返回页数不完整（预期 {expected_pages} 页，返回 {len(pages)} 页），已保留进度')
    indexes = [p.get('page_index') for p in pages]
    if all(n is None for n in indexes):indexes=list(range(expected_pages))
    if any(type(n) is not int for n in indexes) or sorted(indexes) != list(range(expected_pages)):
        raise CloudError('飞桨返回页码重复或缺失，已保留进度')
    return [{**p,'page_index':n} for n,p in sorted(zip(indexes, pages))]

def has_ocr_text(raw):
    return any(str(s).strip() for s in raw.get('rec_texts',[])) or any(
        str(r.get('block_content','')).strip() for r in raw.get('parsing_res_list',[]))


class PaddleCloud:
    def __init__(self, token, cache, *, session=None, cancelled=lambda:False, progress=lambda *a:None):
        self.token=token; self.cache=Path(cache); self.cache.mkdir(parents=True,exist_ok=True)
        self.http=session or requests.Session(); self.cancelled=cancelled; self.progress=progress

    def check(self):
        if self.cancelled(): raise Cancelled()

    def cached(self,file,model,expected_pages,options=None):
        identity={'fileHash':sha256_file(file),'model':model,'options':options or {},'schema':OCR_SCHEMA}
        key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        try:
            value=json.loads((self.cache/key/'parsed.json').read_text(encoding='utf-8'))
            if (value.get('identity')==identity and value.get('resultSchema')==RESULT_SCHEMA
                    and len(value.get('pages',[]))==expected_pages):return value['pages']
        except (OSError,ValueError):pass
        return None

    def refresh_empty(self,file,model,expected_pages,options=None):
        """Retry a completed empty result, retaining its remote audit trail."""
        identity={'fileHash':sha256_file(file),'model':model,'options':options or {},'schema':OCR_SCHEMA}
        key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        folder=self.cache/key
        if not folder.exists():return
        with FileLock(str(folder/'access.lock'),timeout=1800):
            rows=self.cached(file,model,expected_pages,options)
            if rows is None or any(has_ocr_text(r) for r in rows):return
            import uuid
            history=folder/'empty-history'/uuid.uuid4().hex;history.mkdir(parents=True)
            for name in ('parsed.json','remote.json'):
                path=folder/name
                if path.exists():path.replace(history/name)

    def wait(self, seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            self.check(); time.sleep(min(.2, max(0,end-time.monotonic())))

    def retry_wait(self, exc, attempt):
        delay=max(min(30,5*2**attempt),exc.retry_after or 0)
        # Long provider cooldowns pause durably instead of occupying a worker.
        if attempt>=4 or delay>60:
            raise CloudError(exc.user_message+'；自动重试已暂停，进度已保存，请稍后点击翻译继续',
                             code=exc.code,http_status=exc.http_status) from None
        self.progress(f'{exc.user_message}，{delay} 秒后自动重试（{attempt+1}/4），可取消')
        self.wait(delay)

    def get(self, url):
        for attempt in range(5):
            self.check()
            try:
                r=self.http.get(url, headers={'Authorization':'Bearer '+self.token}, timeout=(15,60))
                return response_data(r)
            except CloudError as exc:
                if not exc.retryable:raise
                self.retry_wait(exc,attempt)
            except requests.RequestException:
                self.retry_wait(CloudError('飞桨连接暂时不可用，远端任务记录已保留'),attempt)

    def submit(self,file,model,options,state,state_file):
        """Retry only explicit queue/frequency rejections, never an ambiguous POST."""
        for attempt in range(5):
            self.check()
            state.update(submitting=True,submittedAt=time.time())
            atomic_json(state_file,state)
            try:
                with file.open('rb') as stream:
                    r=self.http.post(JOBS_URL,headers={'Authorization':'Bearer '+self.token},
                        data={'model':model,'batchId':state['batchId'],'optionalPayload':json.dumps(options)},
                        files={'file':(file.name,stream,'application/pdf' if file.suffix.lower()=='.pdf' else 'image/png')},
                        timeout=(15,120))
            except requests.ConnectTimeout:
                state['submitting']=False;atomic_json(state_file,state)
                raise CloudError('尚未建立飞桨上传连接，已保留进度；再次点击翻译可重试') from None
            except requests.RequestException:
                raise CloudError('上传响应中断，已记录远端批次；再次点击时先查询，不重复上传') from None
            try:
                data=response_data(r)
                job=data.get('jobId')
                if not isinstance(job,str) or not job.strip():
                    raise CloudError('飞桨未返回任务编号；已记录批次，再次点击时先查询提交结果',http_status=r.status_code)
            except CloudError as exc:
                state['lastError']={**exc.diagnostic(),'operation':'submit','at':time.time()}
                explicit_rejection=(exc.code in (10010,12002,12001) or
                    (400<=r.status_code<500) or (10001<= (exc.code or 0)<=10009))
                if explicit_rejection:state['submitting']=False
                atomic_json(state_file,state)
                if exc.code in (10010,12002):
                    self.retry_wait(exc,attempt)
                    continue
                # Server errors may occur after acceptance. Reconcile the batch
                # immediately; if absent retain uncertainty for the next resume.
                if not explicit_rejection and exc.retryable:
                    self.progress('飞桨提交响应异常，正在查询远端批次，避免重复上传')
                    recovered=self.recover_submission(state)
                    if recovered:
                        state.update(jobId=recovered,submitting=False);atomic_json(state_file,state)
                        return
                    raise CloudError('飞桨提交响应异常，已保留批次；稍后点击翻译将先查询，不重复上传',
                                     code=exc.code,http_status=exc.http_status) from None
                raise
            state.update(jobId=job,submitting=False);state.pop('lastError',None)
            atomic_json(state_file,state)
            return

    def recover_submission(self, state):
        data=self.get(JOBS_URL+'/batch/'+state['batchId'])
        rows=data.get('extractResult')
        if not isinstance(rows,list) or any(not isinstance(row,dict) or
                not isinstance(row.get('jobId'),str) or not row['jobId'].strip() for row in rows):
            raise CloudError('飞桨批次查询结果不完整，保留提交记录，未重复上传')
        if data.get('batchId',state['batchId'])!=state['batchId']:
            raise CloudError('飞桨批次查询不匹配，保留提交记录，未重复上传')
        if len(rows)==1:return rows[0]['jobId']
        if len(rows)>1:raise CloudError('远端任务记录异常，未重复提交文件')
        return None

    def parse(self, file, model, expected_pages, options=None):
        file=Path(file); options=options or {}
        identity={'fileHash':sha256_file(file),'model':model,'options':options,'schema':OCR_SCHEMA}
        key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        folder=self.cache/key;folder.mkdir(exist_ok=True)
        with FileLock(str(folder/'access.lock'),timeout=1800):
            saved=folder/'parsed.json'; state_file=folder/'remote.json'
            if saved.exists():
                value=json.loads(saved.read_text(encoding='utf-8'))
                if (value.get('identity')==identity and len(value.get('pages',[]))==expected_pages
                        and value.get('resultSchema')==RESULT_SCHEMA):
                    return value['pages']
            state=json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {}
            self.check()
            if not state.get('jobId'):
                if state.get('submitting'):
                    job=self.recover_submission(state)
                    if not job:
                        state['emptyChecks']=state.get('emptyChecks',0)+1
                        if state['emptyChecks']>=3 and time.time()-state.get('submittedAt',time.time())>=60:
                            state['submitting']=False
                            atomic_json(state_file,state)
                            raise CloudError('多次查询确认未找到上次任务；已解除等待，再次点击翻译可重新提交')
                        atomic_json(state_file,state)
                        raise CloudError('正在确认上次飞桨提交结果，未重复上传；请稍后再次点击翻译')
                    state['jobId']=job;atomic_json(state_file,state)
                else:
                    if file.stat().st_size>40_000_000:raise CloudError('单批文件超过 40 MB，需进一步分批')
                    import uuid
                    state={'batchId':'rmb-'+uuid.uuid4().hex,'submitting':True,'identity':identity,'submittedAt':time.time()}
                    atomic_json(state_file,state) # persist BEFORE the potentially ambiguous POST
                    self.submit(file,model,options,state,state_file)
            deadline=time.monotonic()+1800;poll_interval=3
            while True:
                self.check()
                try:data=self.get(JOBS_URL+'/'+state['jobId'])
                except CloudError as exc:
                    if exc.code in (11001,11002):
                        state.update(jobId=None,submitting=False);atomic_json(state_file,state)
                        raise CloudError('该批远端任务已失效，其他进度已保留；再次点击翻译可重新解析此批') from None
                    raise
                status=data.get('state')
                if status=='done':break
                if status=='failed':
                    state.update(jobId=None,submitting=False);atomic_json(state_file,state)
                    raise CloudError('飞桨未能解析该批文件，已保留其他批次进度')
                progress=data.get('extractProgress') or {}
                if status not in ('pending','running'):
                    raise CloudError('飞桨返回未知任务状态，已保存任务编号')
                self.progress('飞桨已接收，正在排队，可取消' if status=='pending' else
                              '云端识别：'+str(progress.get('extractedPages',0))+'/'+str(expected_pages)+' 页')
                if time.monotonic()>deadline:raise CloudError('飞桨仍在处理，已保留任务编号；稍后继续即可')
                self.wait(poll_interval)
                poll_interval=min(15,poll_interval*1.5)
            url=(data.get('resultUrl') or {}).get('jsonUrl')
            from urllib.parse import urlparse
            if not isinstance(url,str) or urlparse(url).scheme!='https':raise CloudError('飞桨未返回有效结果地址')
            self.check()
            # Signed storage URLs must never receive the AI Studio token.
            try:
                r=self.http.get(url,timeout=(15,120));r.raise_for_status();pages=parse_jsonl(r.text,expected_pages)
            except requests.RequestException:raise CloudError('飞桨结果下载中断，任务编号已保存') from None
            atomic_json(saved,{'identity':identity,'resultSchema':RESULT_SCHEMA,'pages':pages})
            return pages

    def test_connection(self):
        # Read-only query: validates the credential without uploading a paper.
        data=self.get(JOBS_URL+'/batch/rmb-connection-check')
        return {'ok':True,'message':'AI Studio 身份验证成功；模型解析将在首次翻译时验证'}
