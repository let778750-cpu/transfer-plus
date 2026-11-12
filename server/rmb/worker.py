"""One worker per user-selected document; cloud recognition, local composition."""
from __future__ import annotations
import argparse
import json
import logging
from pathlib import Path
import sys
from .config import ConfigStore, atomic_json, sha256_file, translation_fingerprint
from .checkpoint import DurableCheckpoint, scope_of

def predominantly_chinese(sample):
    cjk=sum('\u4e00'<=c<='\u9fff' for c in sample)
    return cjk>=80 and (cjk/max(1,len(sample))>=.12 or sum('\u4e00'<=c<='\u9fff' for c in sample[:500])>=20)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',type=Path,required=True);parser.add_argument('--job-id',required=True)
    args=parser.parse_args();job=args.data_dir/'jobs'/args.job_id
    config=json.loads((job/'config.json').read_text(encoding='utf-8'));state=json.loads((job/'job.json').read_text(encoding='utf-8'))
    def progress(message,value=5,stage='recognizing'):
        atomic_json(job/'progress.json',{'message':message,'progress':value,'stage':stage})
    from .paddle_cloud import PaddleCloud
    from .babeldoc_flow import Cancelled as EngineCancelled
    def check():
        if (job/'cancel.request').exists():raise EngineCancelled()
    try:
        import pymupdf as fitz
        source=job/'source.pdf'
        with fitz.open(source) as doc:
            if predominantly_chinese(''.join(doc[n].get_text() for n in range(min(5,len(doc))))):
                atomic_json(job/'report.json',{'status':'skipped','message':'检测为中文论文，已跳过','sourceHash':state['sourceHash'],'issues':[],'pages':len(doc)})
                return
        store=ConfigStore(args.data_dir)
        if config.get('pipeline')=='babeldoc-engine-v1':
            from .babeldoc_flow import run_pipeline as run_engine
            logging.disable(logging.CRITICAL)
            run_engine(source,job,args.data_dir,state,config,store,progress,check)
            return
        from .translator import GuardedTranslator
        from .cloud_pipeline import run_pipeline
        logging.disable(logging.CRITICAL)
        memory_scope={'sourceHash':state['sourceHash'],'translationFingerprint':translation_fingerprint(config)}
        memory_path=args.data_dir/'cache'/'text'/state['sourceHash']/(memory_scope['translationFingerprint']+'.json')
        checkpoint=DurableCheckpoint(job/'translation-checkpoint.json',scope_of(state),memory_path,memory_scope)
        translator=GuardedTranslator(config,store.secret(),checkpoint,check=check)
        cloud=PaddleCloud(store.ocr_secret(),args.data_dir/'cache'/'ocr',cancelled=lambda:(job/'cancel.request').exists(),progress=progress)
        reports=[]
        if state.get('improveBase'):
            base=args.data_dir/'cache'/state['improveBase']
            old=json.loads((base/'report.json').read_text(encoding='utf-8'))
            if old.get('sourceHash')==state['sourceHash'] and old.get('outputHash')==sha256_file(base/'result.pdf'):reports.append(old)
        run_pipeline(source,job,args.data_dir,state,config,cloud,translator,progress,check,reports)
    except Exception as exc:
        atomic_json(job/'error.json',{'message':getattr(exc,'user_message','翻译未完成：'+type(exc).__name__),
                    'errorType':type(exc).__name__,'resumable':getattr(exc,'resumable',True),
                    'diagnostic':exc.diagnostic() if hasattr(exc,'diagnostic') else None})
        import traceback
        atomic_json(job/'error-detail.json',{'type':type(exc).__name__,'frames':[
            {'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(exc.__traceback__)]})
        sys.exit(1)

if __name__=='__main__':main()
