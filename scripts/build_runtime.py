"""Build an isolated, inference-free runtime from an existing Python 3.12."""
import argparse,os,shutil,subprocess,json,ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
# Plugin-only fixes can ship with the unchanged runtime version.
VERSION=next(ast.literal_eval(node.value) for node in ast.parse((ROOT/'server/rmb/__init__.py').read_text()).body
    if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in node.targets))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--python-root',type=Path,required=True)
    parser.add_argument('--runtime',type=Path,required=True);parser.add_argument('--fonts',type=Path,required=True)
    parser.add_argument('--refresh-lock',action='store_true')
    args=parser.parse_args();version=args.runtime/'versions'/VERSION;target=version/'python'
    target.mkdir(parents=True,exist_ok=True)
    for name in ('python.exe','pythonw.exe','python3.dll','python312.dll','vcruntime140.dll','vcruntime140_1.dll','LICENSE.txt'):
        shutil.copy2(args.python_root/name,target/name)
    for folder in ('Lib','DLLs'):
        shutil.copytree(args.python_root/folder,target/folder,dirs_exist_ok=True,
            ignore=shutil.ignore_patterns('site-packages','__pycache__','test','tests','idlelib','tkinter','turtledemo'))
    packages=target/'Lib'/'site-packages';packages.mkdir(exist_ok=True)
    requirements=ROOT/'server'/('requirements.in' if args.refresh_lock else 'requirements.lock')
    # The staged runtime python has no pip; allow a pip-capable installer python.
    pip_python=Path(os.environ.get('RMB_PIP_PYTHON') or args.python_root/'python.exe')
    subprocess.run([str(pip_python),'-I','-m','pip','install','--disable-pip-version-check',
        '--only-binary=:all:','--ignore-installed','--target',str(packages),'-r',str(requirements)],check=True)
    query="import importlib.metadata as m;print('\\n'.join(sorted(d.metadata['Name']+'=='+d.version for d in m.distributions())))"
    locked=subprocess.check_output([str(target/'python.exe'),'-I','-c',query],text=True,encoding='utf-8')
    if args.refresh_lock:(ROOT/'server'/'requirements.lock').write_text(locked,encoding='utf-8')
    shutil.copytree(ROOT/'server',version/'server',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copytree(ROOT/'licenses',version/'licenses',dirs_exist_ok=True)
    fonts=version/'resources'/'fonts';fonts.mkdir(parents=True,exist_ok=True)
    for name in ('SourceHanSerifCN-Regular.ttf','NotoSerif-Regular.ttf'):
        shutil.copy2(args.fonts/name,fonts/name)
    print(json.dumps({'version':VERSION,'runtime':str(version),'packages':len(locked.splitlines())}))

if __name__=='__main__':main()
