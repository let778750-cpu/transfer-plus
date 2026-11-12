"""Package only allowlisted product roots; never package runtime data or papers."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]
VERSION=next(ast.literal_eval(n.value) for n in ast.parse((ROOT/'server/rmb/__init__.py').read_text()).body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in n.targets))
PLUGIN_VERSION=json.loads((ROOT/'plugin/research-max/manifest.json').read_text(encoding='utf-8'))['version']
SKIP={'node_modules','work','dist','.git','.scaffold','.venv','__pycache__','.pytest_cache'}

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def files(root):
    for p in sorted(root.rglob('*')):
        relative=p.relative_to(root)
        if p.is_file() and not any(x in SKIP for x in relative.parts) and p.suffix not in {'.pyc','.pyo','.log','.sqlite','.db','.dpapi'}:
            if p.name.endswith('.local.json') or p.name=='.env':continue
            yield p,relative.as_posix()

def archive(target, entries, extra=None):
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for source,name in entries:z.write(source,name)
        for name,value in (extra or {}).items():z.writestr(name,json.dumps(value,ensure_ascii=False,indent=2))
    with zipfile.ZipFile(target) as z:
        if z.testzip():raise ValueError('Archive CRC check failed')

def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--output',type=Path,default=ROOT/'dist');p.add_argument('--reuse-runtime',action='store_true')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    version=a.runtime/'versions'/VERSION
    required=[version/'python'/'python.exe',version/'server'/'launch.py',ROOT/'docs'/'ACCEPTANCE.md']
    if not all(p.is_file() for p in required):raise ValueError('Runtime or acceptance record missing')
    if not any((version/'resources'/'fonts').glob('*.ttf')):raise ValueError('Missing bundled fonts')
    entries=[]
    for folder in ['python','server','licenses','resources']:
        entries += [(p,f'versions/{VERSION}/{folder}/{rel}') for p,rel in files(version/folder)]
    entries.append((ROOT/'scripts'/'install-runtime.ps1','install-runtime.ps1'))
    manifest={'version':VERSION,'files':[{'path':name,'sha256':sha(p),'bytes':p.stat().st_size} for p,name in entries]}
    inventory_code="import importlib.metadata as m,json; print(json.dumps(sorted([{'name':d.metadata['Name'],'version':d.version,'licenseExpression':d.metadata.get('License-Expression'),'projectUrls':d.metadata.get_all('Project-URL',[])} for d in m.distributions()],key=lambda x:x['name'].lower())))"
    dependencies=json.loads(subprocess.check_output([str(version/'python'/'python.exe'),'-I','-c',inventory_code],encoding='utf-8'))
    forbidden={'pdf2zh-next','paddlepaddle','torch','gradio'}
    if forbidden & {d['name'].lower() for d in dependencies}:raise ValueError('Heavy inference dependency in lightweight runtime')
    runtime_zip=a.output/f'research-max-bilingual-runtime-{VERSION}-win64.zip'
    if a.reuse_runtime:
        with zipfile.ZipFile(runtime_zip) as z:
            if json.loads(z.read('runtime-manifest.json')) != manifest:
                raise ValueError('Runtime contents changed; rebuild the runtime archive')
    else:archive(runtime_zip,entries,{'runtime-manifest.json':manifest,'dependency-inventory.json':dependencies})
    print(runtime_zip.name,flush=True)
    source_entries=[]
    for folder in ['plugin','server','engine-patches','scripts','tests','docs','licenses']:
        source_entries += [(p,f'{folder}/{rel}') for p,rel in files(ROOT/folder)]
    for name in ['README.md','.gitignore','package.json','package-lock.json','upstream-lock.json']:
        source_entries.append((ROOT/name,name))
    source_manifest=[{'path':name,'sha256':sha(p)} for p,name in source_entries]
    source_zip=a.output/f'research-max-bilingual-source-{PLUGIN_VERSION}.zip'
    archive(source_zip,source_entries,{'source-manifest.json':source_manifest})
    print(source_zip.name,flush=True)
    xpi=a.output/f'research-max-bilingual-{PLUGIN_VERSION}.xpi'
    if not xpi.exists():raise ValueError('Build XPI first')
    try:commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,encoding='utf-8').strip()
    except subprocess.CalledProcessError:commit=None
    release={'version':PLUGIN_VERSION,'runtimeVersion':VERSION,'status':'candidate-until-acceptance-complete',
        'sourceBaseCommit':commit,'sourceState':'working-tree snapshot; see source archive manifest',
        'sourceTreeSha256':hashlib.sha256(json.dumps(source_manifest,sort_keys=True).encode()).hexdigest(),
        'acceptanceRecord':'docs/ACCEPTANCE.md','acceptanceSha256':sha(ROOT/'docs'/'ACCEPTANCE.md'),
        'dependencies':dependencies,'artifacts':[{'file':f.name,'sha256':sha(f),'bytes':f.stat().st_size} for f in [xpi,runtime_zip,source_zip]]}
    (a.output/'release-manifest.json').write_text(json.dumps(release,ensure_ascii=False,indent=2),encoding='utf-8')
    (a.output/'SHA256SUMS.txt').write_text(''.join(f"{item['sha256']}  {item['file']}\n" for item in release['artifacts']),encoding='utf-8')
    print('Release manifest and SHA256SUMS written',flush=True)

if __name__=='__main__':main()
