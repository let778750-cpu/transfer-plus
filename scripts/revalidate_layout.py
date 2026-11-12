"""Recheck an existing clean writer artifact without another model request.

Use only with the same pinned engine and formula-recognition patch. This replays
postflight, not translation. Old job evidence and imported attachments are kept.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.config import atomic_json,config_fingerprint,sha256_file
from rmb.quality import verify_and_compose
p=argparse.ArgumentParser();p.add_argument('job',type=Path);p.add_argument('--publish-cache',type=Path)
a=p.parse_args();job=a.job.resolve();cfg=json.loads((job/'config.json').read_text(encoding='utf-8'))
state=json.loads((job/'job.json').read_text(encoding='utf-8'));old_fingerprint=state['configFingerprint']
state['configFingerprint']=config_fingerprint(cfg)
out=job/'revalidated'/state['configFingerprint'];out.mkdir(parents=True,exist_ok=True)
verify_and_compose(job/'source.pdf',job/'engine-work/clean/source.zh-CN.mono.pdf',job/'engine-work/inspection-ir/source',
                   out/'result.pdf',out/'report.json',cfg,json.loads((job/'translation-records.json').read_text(encoding='utf-8')),state,
                   final_ir_path=job/'engine-work/clean-ir/source/final-layout.json')
report=json.loads((out/'report.json').read_text(encoding='utf-8'))
report['revalidatedFrom']={'taskId':state['taskId'],'configFingerprint':old_fingerprint}
atomic_json(out/'report.json',report)
if a.publish_cache:
    if report['status'] not in ('completed','completed_with_warnings'):raise ValueError('Result failed postflight')
    key=hashlib.sha256((state['sourceHash']+state['configFingerprint']+json.dumps(state.get('pages'))).encode()).hexdigest()
    target=a.publish_cache/'cache'/key
    if (target/'result.pdf').exists():raise ValueError('Refuse to overwrite an existing archive revision')
    target.mkdir(parents=True,exist_ok=True)
    shutil.copy2(out/'result.pdf',target/'result.pdf');shutil.copy2(out/'report.json',target/'report.json')
print(json.dumps({'output':str(out),'status':report['status'],'chinesePages':report['pagesWithChinese'],
                  'issues':len(report['issues']),'outputHash':sha256_file(out/'result.pdf')},ensure_ascii=True))
