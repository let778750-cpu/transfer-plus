"""Read-only installation checks. Output contains no credentials or document text."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from rmb.config import ConfigStore
p=argparse.ArgumentParser();p.add_argument('--data-dir',type=Path,required=True);a=p.parse_args()
expected=dict(line.strip().split('==') for line in (Path(__file__).parent/'requirements.lock').read_text().splitlines() if '==' in line)
checks={}
for name,version in expected.items():
    try:checks[f'{name} version']=importlib.metadata.version(name)==version
    except importlib.metadata.PackageNotFoundError:checks[f'{name} version']=False
checks['isolated Python']=bool(sys.flags.isolated)
checks['Python 3.12']=sys.version_info[:2]==(3,12)
for name in ['SourceHanSerifCN-Regular.ttf','NotoSerif-Regular.ttf']:
    checks[name]=(Path(__file__).resolve().parents[1]/'resources'/'fonts'/name).is_file()
checks['no local inference framework']=not any(d.metadata['Name'].lower() in {'babeldoc','pdf2zh-next','paddlepaddle','torch','onnxruntime'} for d in importlib.metadata.distributions())
store=ConfigStore(a.data_dir)
if store.secret_path.exists():
    try:checks['credential decrypts']=bool(store.secret())
    except Exception:checks['credential decrypts']=False
if store.ocr_secret_path.exists():
    try:checks['OCR credential decrypts']=bool(store.ocr_secret())
    except Exception:checks['OCR credential decrypts']=False
print(json.dumps({'checks':checks,'credentialConfigured':store.secret_path.exists(),'paddleConfigured':store.ocr_secret_path.exists(),'networkTests':False,'passed':all(checks.values())},ensure_ascii=True,indent=2))
sys.exit(0 if all(checks.values()) else 1)
