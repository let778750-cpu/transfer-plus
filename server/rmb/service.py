"""Authenticated loopback task API. The UI never receives model credentials."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from flask import Flask, jsonify, request, send_file
from werkzeug.serving import make_server
from . import VERSION
from .config import ConfigStore, DEFAULTS, atomic_json, config_fingerprint, sha256_file
from .winjob import ProcessTree
from .checkpoint import copy_resume

def load_task_manager():
    path = Path(__file__).resolve().parents[1] / "upstream" / "utils" / "task_manager.py"
    spec = importlib.util.spec_from_file_location("pdf2zh_task_manager", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TaskManager()

class JobService:
    def __init__(self, data_dir: Path):
        self.data = Path(data_dir).resolve()
        for name in ("config", "jobs", "cache", "reports", "logs", "glossary"):
            (self.data / name).mkdir(parents=True, exist_ok=True)
        self.config = ConfigStore(self.data)
        self.manager = load_task_manager()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pdf-queue")
        self.processes = {}
        self.trees = {}
        self.cancelled = set()
        self.lock = threading.RLock()
        self.closing = False
        # A crashed process must not leave an old task permanently 'running'.
        for p in (self.data / "jobs").glob("*/job.json"):
            try:
                state = json.loads(p.read_text(encoding="utf-8"))
                if state.get("status") in ("queued", "running"):
                    state.update(status="interrupted", message="任务已中断，已保留进度；再次点击翻译即可继续", active=False, resumable=True)
                    atomic_json(p, state)
            except (OSError, ValueError):
                pass

    def read_job(self, job_id):
        if not isinstance(job_id, str) or not all(c in "0123456789abcdef" for c in job_id) or len(job_id) != 32:
            raise ValueError("Invalid task ID")
        path = self.data / "jobs" / job_id / "job.json"
        if not path.exists():
            raise KeyError(job_id)
        state = json.loads(path.read_text(encoding="utf-8"))
        progress = path.with_name("progress.json")
        if state["status"] == "running" and progress.exists():
            try:
                state.update(json.loads(progress.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return state

    def save_job(self, state):
        atomic_json(self.data / "jobs" / state["taskId"] / "job.json", state)
        self.manager.update_task(state["taskId"], state)

    def best_archive(self,source_hash,config,pages=None,legacy=False,output_hash=None):
        import hashlib
        fingerprints={config_fingerprint(config)}
        if legacy:
            public={k:config[k] for k in DEFAULTS};public['pipeline']='rmb-0.3.0-paddle-cloud-layout5'
            fingerprints.add(hashlib.sha256(json.dumps(public,sort_keys=True,ensure_ascii=False).encode()).hexdigest())
            public['pipeline']='rmb-0.3.4-paddle-cloud-layout5-compose2'
            fingerprints.add(hashlib.sha256(json.dumps(public,sort_keys=True,ensure_ascii=False).encode()).hexdigest())
            for pipeline in ('rmb-0.2.3-babeldoc-0.7.1-directpdf4','rmb-0.1.2-babeldoc-0.7.1-guard9','rmb-0.1.6-babeldoc-0.7.1-guard10','rmb-0.2.0-babeldoc-0.7.1-reading1','rmb-0.2.1-babeldoc-0.7.1-reading2','rmb-0.2.2-babeldoc-0.7.1-directpdf3'):
                public={k:config[k] for k in DEFAULTS if k not in ('ocrProvider','ocrModel')};public['pipeline']=pipeline
                fingerprints.add(hashlib.sha256(json.dumps(public,sort_keys=True,ensure_ascii=False).encode()).hexdigest())
        candidates=[]
        for path in (self.data/'cache').glob('*/report.json'):
            if len(path.parent.name)!=64 or any(c not in '0123456789abcdef' for c in path.parent.name):continue
            try:
                report=json.loads(path.read_text(encoding='utf-8'))
                if (report.get('sourceHash')!=source_hash or report.get('configFingerprint') not in fingerprints
                    or report.get('status') not in ('completed','completed_with_warnings')
                    or report.get('requestedPages')!=pages or (output_hash and report.get('outputHash')!=output_hash)):continue
                if pages is None and any(p.get('reason')=='not_selected' for p in report.get('preserved',[])):continue
                coverage=report.get('coverage',{});total=max(1,coverage.get('total',1))
                rank=(report.get('configFingerprint')==config_fingerprint(config),
                      coverage.get('pdf',0)/total,
                      (coverage.get('pdf',0)+coverage.get('supplement',0))/total,path.stat().st_mtime)
                candidates.append((rank,path.parent,report))
            except (OSError,ValueError,TypeError):continue
        for _,folder,report in sorted(candidates,reverse=True):
            try:
                if sha256_file(folder/'result.pdf')==report['outputHash']:return folder,report
            except OSError:continue
        return None

    def submit(self, source_path, source_key="", pages=None,improve=False,base_output_hash=None,automatic=False):
        source = Path(source_path).resolve(strict=True)
        if source.suffix.lower() != ".pdf" or not source.is_file():
            raise ValueError("请选择 PDF 附件")
        with source.open("rb") as f:
            if b"%PDF-" not in f.read(1024):
                raise ValueError("文件不是有效 PDF")
        config = self.config.read()
        if pages is not None and (not isinstance(pages, str) or not __import__('re').fullmatch(r"[0-9, -]+", pages)):
            raise ValueError("页码范围无效")
        source_hash = sha256_file(source)
        fingerprint = config_fingerprint(config)
        if not isinstance(improve,bool) or not isinstance(automatic,bool):raise ValueError('Invalid improve option')
        if automatic:improve=True;base_output_hash=None
        import hashlib
        cache_key = hashlib.sha256((source_hash + fingerprint + json.dumps(pages)).encode()).hexdigest()
        base=None;audit=None;audited={};needs_improve=False
        if config.get('pipeline')!='babeldoc-engine-v1':
            base=self.best_archive(source_hash,config,pages,legacy=improve,output_hash=base_output_hash if improve else None)
            if improve and base_output_hash and base is None:raise ValueError('原存档与当前翻译设置不匹配，请使用普通翻译生成新版本')
            if automatic and base:
                from .cloud_pipeline import audit_archive
                audit=audit_archive(source,base[0]/'result.pdf',base[1],pages=pages)
            audited={base[0].name:audit} if base and audit else {}
            needs_improve=improve and base and (audit['needsWork'] if automatic and audit else
                          (base[1].get('configFingerprint')!=fingerprint or base[1].get('schema',1)<2 or
                           base[1].get('coverage',{}).get('missing',1)>0))
            if needs_improve:cache_key=hashlib.sha256((cache_key+base[0].name+sha256_file(base[0]/'report.json')).encode()).hexdigest()
            elif base:cache_key=base[0].name
        with self.lock:
            if self.closing:
                raise ValueError("服务正在退出")
            for current in self.manager.get_active_tasks_list():
                same_work=current.get('sourceHash')==source_hash and current.get('configFingerprint')==fingerprint and current.get('pages')==pages
                if (current.get("cacheKey") == cache_key or (automatic and same_work)) and current.get("status") in ("queued", "running"):
                    return self.read_job(current["taskId"])
            # A prior attempt may already occupy this derived revision. Audit
            # that exact result too; never return it merely because the base was
            # audited, and never overwrite an incomplete archived revision.
            if automatic and config.get('pipeline')!='babeldoc-engine-v1':
                from .cloud_pipeline import audit_archive
                for _ in range(64):
                    candidate=self.data/'cache'/cache_key
                    if not candidate.exists():break
                    try:
                        existing=json.loads((candidate/'report.json').read_text(encoding='utf-8'))
                        if (existing.get('sourceHash')!=source_hash or existing.get('configFingerprint')!=fingerprint
                            or existing.get('status') not in ('completed','completed_with_warnings')
                            or sha256_file(candidate/'result.pdf')!=existing.get('outputHash')):raise ValueError('Invalid archive')
                        checked=audited.get(candidate.name)
                        if checked is None:checked=audit_archive(source,candidate/'result.pdf',existing,pages=pages)
                    except (OSError,ValueError,KeyError,TypeError):
                        cache_key=hashlib.sha256((cache_key+uuid.uuid4().hex).encode()).hexdigest();break
                    if not checked['needsWork']:
                        audit=checked;break
                    base=(candidate,existing);audit=checked;needs_improve=True
                    cache_key=hashlib.sha256((source_hash+fingerprint+json.dumps(pages)+candidate.name+sha256_file(candidate/'report.json')).encode()).hexdigest()
                else:raise ValueError('存档修订链过长，未覆盖任何已有结果')
            job_id = uuid.uuid4().hex
            job_dir = self.data / "jobs" / job_id
            job_dir.mkdir()
            state = {"taskId": job_id, "status": "queued", "active": True, "progress": 0,
                     "message": "等待翻译", "sourceKey": str(source_key), "sourceHash": source_hash,
                     "configFingerprint": fingerprint, "cacheKey": cache_key,
                     "fileName": source.name, "createdAt": time.time(), "pages": pages}
            if needs_improve:state['improveBase']=base[0].name
            if audit:state['archiveAudit']=audit
            cache_dir = self.data / "cache" / cache_key
            if (cache_dir / "result.pdf").exists() and (cache_dir / "report.json").exists():
                try:
                    report = json.loads((cache_dir / "report.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    report = {}
                if (report.get("sourceHash") == source_hash and report.get("configFingerprint") == fingerprint
                    and report.get("status") in ("completed", "completed_with_warnings")
                    and report.get("outputHash") == sha256_file(cache_dir / "result.pdf")):
                    shutil.copy2(cache_dir / "result.pdf", job_dir / "result.pdf")
                    shutil.copy2(cache_dir / "report.json", job_dir / "report.json")
                    message="本机检查完成，使用现有译文" if automatic else "使用已保存的译文"
                    if audit and audit.get('unfinished'):message+=f"；仍有 {audit['unfinished']} 处未完成，请查看核对入口"
                    state.update(status=report["status"], active=False, progress=100, cached=True,
                                 message=message, warningCount=len(report.get("issues", [])))
                    self.save_job(state)
                    return state
            self.config.secret()
            if config.get('pipeline')!='babeldoc-engine-v1':
                self.config.ocr_secret()  # Offline cache hits above need neither credential.
            shutil.copy2(source, job_dir / "source.pdf")
            atomic_json(job_dir / "config.json", {k: v for k, v in config.items() if k not in ("hasApiKey","hasPaddleApiKey")})
            previous=[]
            for path in (self.data/'jobs').glob('*/job.json'):
                try:
                    old=json.loads(path.read_text(encoding='utf-8'))
                    if old.get('cacheKey')==cache_key and old.get('status') in ('cancelled','interrupted','failed'):
                        previous.append((old.get('createdAt',0),path.parent))
                except (OSError,ValueError): pass
            if previous:
                prior=max(previous,key=lambda value:value[0])[1]
                restored=copy_resume(prior,job_dir,state,include_stages=prior.name not in self.processes)
                if restored['savedTranslations'] or restored['stages']:
                    state.update(resumedFrom=prior.name,resume=restored,
                                 message=f"继续翻译：已恢复 {restored['savedTranslations']} 条译文")
            self.manager.add_task(job_id, state)
            self.save_job(state)
            self.pool.submit(self.run, job_id)
            return state

    def run(self, job_id):
        try:
            self._run(job_id)
        except Exception as exc:
            with self.lock:
                tree = self.trees.pop(job_id, None)
                if tree:
                    tree.close()
                proc = self.processes.pop(job_id, None)
                if proc and proc.poll() is None:
                    proc.terminate()
                state = self.read_job(job_id)
                state.update(status="cancelled" if job_id in self.cancelled or self.closing else "failed", active=False,
                             resumable=True,message="已取消，进度已保留；再次点击翻译即可继续" if job_id in self.cancelled or self.closing else "任务启动或交付失败：" + type(exc).__name__)
                self.save_job(state)

    def _run(self, job_id):
        job_dir = self.data / "jobs" / job_id
        with self.lock:
            state = self.read_job(job_id)
            if job_id in self.cancelled or self.closing:
                state.update(status="cancelled", active=False, resumable=True, message="已取消，进度已保留；再次点击翻译即可继续")
                self.save_job(state)
                return
            state.update(status="running", message="正在解析 PDF")
            self.save_job(state)
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["PYTHONNOUSERSITE"] = "1"
            env["PYTHONUTF8"] = "1"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            # Vendor asset/cache resolution uses these controlled directories.
            env["RMB_FONT_DIR"] = str(Path(__file__).resolve().parents[2] / "resources" / "fonts")
            with (job_dir / "worker.log").open("wb") as log:
                proc = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve().parents[1] / "worker_entry.py"), "--data-dir", str(self.data),
                                     "--job-id", job_id], env=env, stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.processes[job_id] = proc
            self.trees[job_id] = ProcessTree(proc)
        try:
            return_code = proc.wait()
        finally:
            with self.lock:
                tree = self.trees.pop(job_id, None)
                if tree:
                    tree.close()
        with self.lock:
            self.processes.pop(job_id, None)
            state = self.read_job(job_id)
            if job_id in self.cancelled or self.closing:
                state.update(status="cancelled", active=False, resumable=True, message="已取消，进度已保留；再次点击翻译即可继续")
            elif return_code == 0 and (job_dir / "report.json").exists():
                report = json.loads((job_dir / "report.json").read_text(encoding="utf-8"))
                report['requestedPages']=state.get('pages')
                atomic_json(job_dir/'report.json',report)
                state.update(status=report["status"], active=False, progress=100,
                             message=report["message"], warningCount=len(report.get("issues", [])))
                if state["status"] in ("completed", "completed_with_warnings") and (job_dir / "result.pdf").exists():
                    cache_dir = self.data / "cache" / state["cacheKey"]
                    cache_dir.mkdir(exist_ok=True)
                    shutil.copy2(job_dir / "result.pdf", cache_dir / "result.pdf")
                    shutil.copy2(job_dir / "report.json", cache_dir / "report.json")
            else:
                error_file = job_dir / "error.json"
                error = json.loads(error_file.read_text(encoding="utf-8")) if error_file.exists() else {}
                state.update(status="failed", active=False, resumable=error.get("resumable",True), message=error.get("message", "翻译进程未完成，请重试"))
            self.save_job(state)

    def cancel(self, job_id):
        with self.lock:
            state = self.read_job(job_id)
            if state["status"] not in ("queued", "running"):
                return state
            self.cancelled.add(job_id)
            (self.data / "jobs" / job_id / "cancel.request").touch()
            proc = self.processes.get(job_id)
            if proc and proc.poll() is None:
                proc.terminate()
            tree = self.trees.get(job_id)
            if tree:
                tree.close()
            state.update(status="cancelled", active=False, resumable=True, message="已取消，进度已保留；再次点击翻译即可继续")
            self.save_job(state)
            return state

    def result_path(self, job_id):
        """Every consumer reads the permanent archive, not another's temp copy."""
        state = self.read_job(job_id)
        if state['status'] not in ('completed','completed_with_warnings'):
            raise ValueError('译文尚未完成')
        cache = self.data/'cache'/state['cacheKey']
        report = json.loads((cache/'report.json').read_text(encoding='utf-8'))
        path = cache/'result.pdf'
        if (report.get('sourceHash') != state['sourceHash']
            or report.get('configFingerprint') != state['configFingerprint']
            or report.get('outputHash') != sha256_file(path)):
            raise ValueError('本机译文存档校验失败，请重新生成')
        return path

    def shutdown(self):
        with self.lock:
            self.closing = True
            for task in self.manager.get_active_tasks_list():
                if task.get("active"):
                    self.cancel(task["taskId"])
        self.pool.shutdown(wait=True, cancel_futures=True)

def create_app(service, token):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

    @app.before_request
    def authenticate():
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify(error="Loopback only"), 403
        if request.headers.get("Origin"):
            return jsonify(error="Browser origins are not accepted"), 403
        if not secrets.compare_digest(request.headers.get("X-RMB-Token", ""), token):
            return jsonify(error="Unauthorized"), 401

    @app.errorhandler(Exception)
    def error(exc):
        if hasattr(exc,"user_message"):
            return jsonify(error=exc.user_message), 400
        if isinstance(exc, KeyError):
            return jsonify(error="任务不存在"), 404
        if isinstance(exc, (ValueError, FileNotFoundError)):
            return jsonify(error=str(exc)), 400
        # Do not expose request bodies, filesystem stack traces or credentials.
        return jsonify(error="本机服务出错，请查看诊断记录", errorType=type(exc).__name__), 500

    @app.get("/health")
    def health():
        return jsonify(status="ok", version=VERSION, protocol=1, capabilities=['resume-checkpoint-v1'])

    @app.route("/api/config", methods=["GET", "POST"])
    def configuration():
        return jsonify(service.config.update(request.get_json()) if request.method == "POST" else service.config.read())

    @app.post("/api/config/test-paddle")
    def test_paddle():
        from .paddle_cloud import PaddleCloud
        cloud=PaddleCloud(service.config.ocr_secret(),service.data/"cache"/"ocr")
        return jsonify(cloud.test_connection())

    @app.post("/translate")
    def translate():
        data = request.get_json()
        return jsonify(service.submit(data["sourcePath"], data.get("sourceKey", ""), data.get("pages"),
                                      data.get('improve',False),data.get('baseOutputHash'),data.get('automatic',True))), 202

    @app.get("/api/tasks/<job_id>")
    def task(job_id):
        return jsonify(service.read_job(job_id))

    @app.post("/api/tasks/<job_id>/cancel")
    def cancel(job_id):
        return jsonify(service.cancel(job_id))

    @app.get("/api/tasks/<job_id>/report")
    def report(job_id):
        service.read_job(job_id)
        return send_file(service.data / "jobs" / job_id / "report.json", mimetype="application/json")

    @app.get("/translatedFile/<job_id>")
    def download(job_id):
        return send_file(service.result_path(job_id), mimetype="application/pdf")

    @app.get('/api/tasks/<job_id>/result')
    def result_path(job_id):
        return jsonify(path=str(service.result_path(job_id)))

    @app.post("/api/tasks/<job_id>/imported")
    def imported(job_id):
        state = service.read_job(job_id)
        if state["status"] not in ("completed", "completed_with_warnings"):
            raise ValueError("译文尚未完成")
        data = request.get_json()
        import re
        if not isinstance(data, dict) or not re.fullmatch(r'[A-Z0-9]{8}',str(data.get('attachmentKey',''))) or not isinstance(data.get('libraryID'),int):
            raise ValueError('附件交付信息无效')
        report_path = service.data / "jobs" / job_id / "report.json"
        report = json.loads(report_path.read_text(encoding='utf-8'))
        if data.get('deliveryId'):
            delivery_id=str(data['deliveryId'])
            if not re.fullmatch(r'[a-f0-9]{64}',delivery_id):raise ValueError('交付记录无效')
            delivery=json.loads((report_path.parent/'deliveries'/(delivery_id+'.json')).read_text(encoding='utf-8'))
            if (delivery['attachmentKey']!=data['attachmentKey'] or delivery['libraryID']!=data['libraryID']
                or delivery['report']['outputHash']!=data.get('outputHash')
                or sha256_file(Path(delivery['path']))!=data['outputHash']):
                raise ValueError('交付文件与准备记录不匹配')
            report=delivery['report']
        elif data.get('outputHash') and data['outputHash']!=report['outputHash']:
            raise ValueError('交付文件与存档不匹配')
        report['attachment'] = {'key':data['attachmentKey'],'libraryID':data['libraryID'],
                                'sourceKey':str(data.get('sourceKey',state['sourceKey']))[:150]}
        delivery_path=service.data / 'reports' / (state['cacheKey']+'.json')
        with service.lock:
            previous=json.loads(delivery_path.read_text(encoding='utf-8')) if delivery_path.exists() else {}
            attachments=previous.get('attachments') or ([previous['attachment']] if previous.get('attachment') else [])
            if report['attachment'] not in attachments:attachments.append(report['attachment'])
            report['attachments']=attachments
            atomic_json(delivery_path,report)
            atomic_json(service.data/'reports'/f"attachment-{data['libraryID']}-{data['attachmentKey']}.json",report)
        # Original PDF, temporary engine artifacts and log are no longer needed.
        source_copy = service.data / "jobs" / job_id / "source.pdf"
        source_copy.unlink(missing_ok=True)
        work = service.data / "jobs" / job_id / "engine-work"
        if work.exists():
            shutil.rmtree(work)
        for filename in ('prompt-cache.json','translation-records.json','translation-checkpoint.json','stages.json','worker.log','config.json','result.pdf','normalized.pdf','inventory.json'):
            (source_copy.parent/filename).unlink(missing_ok=True)
        state['imported'] = report['attachment']
        service.save_job(state)
        return jsonify(ok=True)

    @app.post('/api/tasks/<job_id>/prepare-update')
    def prepare_attachment_update(job_id):
        from .attachment_update import prepare_update
        return jsonify(prepare_update(service,job_id,request.get_json()))

    @app.post("/shutdown")
    def shutdown():
        callback = app.config.get("RMB_SHUTDOWN")
        if callback:
            threading.Thread(target=callback, daemon=True).start()
        return jsonify(ok=True)

    return app

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--endpoint-file", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    service = JobService(args.data_dir)
    token = secrets.token_urlsafe(32)
    app = create_app(service, token)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    app.config["RMB_SHUTDOWN"] = server.shutdown
    atomic_json(args.endpoint_file, {"url": f"http://127.0.0.1:{server.server_port}", "token": token,
                                     "pid": os.getpid(), "version": VERSION})
    def watch_parent():
        if os.name != "nt" or not args.parent_pid:
            return
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x00100000, False, args.parent_pid)
        if handle:
            kernel.WaitForSingleObject(ctypes.c_void_p(handle), 0xFFFFFFFF)
            kernel.CloseHandle(ctypes.c_void_p(handle))
        server.shutdown()
    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        service.shutdown()
        args.endpoint_file.unlink(missing_ok=True)

if __name__ == "__main__":
    main()
