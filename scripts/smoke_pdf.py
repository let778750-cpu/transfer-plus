import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.service import JobService
p=argparse.ArgumentParser()
p.add_argument('--data',type=Path,required=True)
p.add_argument('--pdf',type=Path,required=True)
p.add_argument('--pages')
a=p.parse_args()
service=JobService(a.data)
try:
    task=service.submit(a.pdf,pages=a.pages)
    print(json.dumps({'taskId':task['taskId']},ensure_ascii=True),flush=True)
    previous=None
    while task['active']:
        current=(task['status'],task.get('message'),round(task.get('progress',0)))
        if current!=previous:
            print(json.dumps(current,ensure_ascii=True),flush=True);previous=current
        time.sleep(2)
        task=service.read_job(task['taskId'])
    print(json.dumps(task,ensure_ascii=True),flush=True)
    if task['status']=='failed':sys.exit(1)
finally:service.shutdown()
