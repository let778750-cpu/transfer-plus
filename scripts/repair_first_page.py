"""Repair one archived first page using retained engine artifacts, without
retranslating other pages or overwriting any existing PDF/annotation attachment.
The selected job must contain source, inspection IR, clean PDF and writer IR.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import pymupdf as fitz
from rmb.config import DEFAULTS, atomic_json, config_fingerprint, sha256_file
from rmb.quality import verify_and_compose


def repair(job,base,output,cache=None):
    cfg=json.loads((job/'config.json').read_text(encoding='utf-8'))
    state=json.loads((job/'job.json').read_text(encoding='utf-8'))
    old=json.loads((base/'report.json').read_text(encoding='utf-8'))
    # A previous pipeline may change, but not language/model/glossary/layout settings.
    legacy={k:cfg[k] for k in DEFAULTS};legacy['pipeline']='rmb-0.1.2-babeldoc-0.7.1-guard9'
    legacy_hash=hashlib.sha256(json.dumps(legacy,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if old.get('configFingerprint')!=legacy_hash:raise ValueError('Base archive translation settings differ')
    source_hash=sha256_file(job/'source.pdf')
    if old.get('sourceHash')!=source_hash or state.get('sourceHash')!=source_hash:raise ValueError('Source identity mismatch')
    if old.get('outputHash')!=sha256_file(base/'result.pdf'):raise ValueError('Base archive is corrupt')
    if old.get('status') not in ('completed','completed_with_warnings'):raise ValueError('Base archive is incomplete')
    if any(p.get('page')!=1 and p.get('reason')=='not_selected' for p in old.get('preserved',[])):
        raise ValueError('Base archive omits pages outside the repaired first page')
    if output.resolve()==base.resolve():raise ValueError('Repair output must not replace the base archive')
    output.mkdir(parents=True,exist_ok=True)
    # This legacy migration does not run the reading enhancement pass. Keep its
    # archive identity at guard10 so it cannot masquerade as a 0.2 reading result.
    first_page_config={k:cfg[k] for k in DEFAULTS}
    first_page_config['pipeline']='rmb-0.1.6-babeldoc-0.7.1-guard10'
    first_page_hash=hashlib.sha256(json.dumps(first_page_config,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    state.update(pages='1',configFingerprint=first_page_hash)
    patch_pdf=output/'first-page-candidate.pdf';patch_report=output/'first-page-report.json'
    verify_and_compose(job/'source.pdf',job/'engine-work/clean/source.zh-CN.mono.pdf',job/'engine-work/inspection-ir/source',
                       patch_pdf,patch_report,cfg,json.loads((job/'translation-records.json').read_text(encoding='utf-8')),state,
                       final_ir_path=job/'engine-work/clean-ir/source/final-layout.json')
    report=json.loads(patch_report.read_text(encoding='utf-8'))
    if report['pagesWithChinese']!=1 or any(i['page']==1 and i['fallback']=='page' for i in report['issues']):
        raise ValueError('Repaired first page did not pass local composition checks')
    target=output/'result.pdf'
    with fitz.open(base/'result.pdf') as previous,fitz.open(patch_pdf) as patch,fitz.open() as merged:
        if len(previous)!=len(patch):raise ValueError('Page count mismatch')
        merged.insert_pdf(patch,from_page=0,to_page=0)
        if len(previous)>1:merged.insert_pdf(previous,from_page=1,to_page=len(previous)-1)
        merged.set_metadata({'title':'Chinese reading copy','producer':'Research Max Bilingual 0.1.6 / first-page repair'})
        merged.save(target,garbage=3,deflate=True)
    with fitz.open(target) as merged,fitz.open(base/'result.pdf') as previous:
        for number in range(1,len(previous)):
            if previous[number].get_pixmap(matrix=fitz.Matrix(.3,.3),alpha=False).samples!=merged[number].get_pixmap(matrix=fitz.Matrix(.3,.3),alpha=False).samples:
                raise ValueError('A retained page changed')
        report['pagesWithChinese']=sum(sum('\u4e00'<=c<='\u9fff' for c in p.get_text())>=5 for p in merged)
    report['issues']=[i for i in report['issues'] if i['page']==1]+[i for i in old['issues'] if i['page']!=1]
    report['preserved']=[i for i in report['preserved'] if i['page']==1]+[i for i in old['preserved'] if i['page']!=1]
    report.update(outputHash=sha256_file(target),status='completed_with_warnings' if report['issues'] else 'completed',
                  message=f"首页已修复，{len(report['issues'])} 处待核对" if report['issues'] else '首页已修复',
                  repairedPages=[1],checksScope={'recheckedPages':[1],'retainedPages':'render-equivalent to previous archive'},
                  retainedPagesFrom={'outputHash':old['outputHash'],'configFingerprint':old['configFingerprint']})
    atomic_json(output/'report.json',report)
    destination=None
    if cache:
        key=hashlib.sha256((state['sourceHash']+state['configFingerprint']+json.dumps(None)).encode()).hexdigest()
        destination=cache/key
        if destination.exists():raise ValueError('Refuse to overwrite an archive revision')
        cache.mkdir(parents=True,exist_ok=True)
        staging=Path(tempfile.mkdtemp(prefix='.first-page-repair-',dir=cache))
        try:
            shutil.copy2(target,staging/'result.pdf');shutil.copy2(output/'report.json',staging/'report.json')
            staging.rename(destination)
        finally:
            if staging.exists():
                # Only remove files created in this uniquely owned staging directory.
                for name in ('result.pdf','report.json'):(staging/name).unlink(missing_ok=True)
                staging.rmdir()
    return {'output':str(output),'archive':str(destination) if destination else None,'sourceHash':state['sourceHash'],
            'oldOutputHash':old['outputHash'],'outputHash':report['outputHash'],'issues':len(report['issues'])}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--job',type=Path,required=True);p.add_argument('--base-archive',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--publish-cache',type=Path)
    a=p.parse_args();print(json.dumps(repair(a.job,a.base_archive,a.output,a.publish_cache),ensure_ascii=True))
