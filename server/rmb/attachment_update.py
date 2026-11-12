"""Prepare a reversible update; only Zotero commits its own attachment file."""
import json
import re
import shutil
import hashlib
from pathlib import Path
import pymupdf as fitz
from .config import atomic_json, sha256_file

def prepare_update(service, job_id, data):
    state=service.read_job(job_id)
    new_path=service.result_path(job_id)
    report=json.loads((service.data/'jobs'/job_id/'report.json').read_text(encoding='utf-8'))
    old=Path(data['path']).resolve(strict=True);old_hash=data['outputHash']
    if not re.fullmatch(r'[a-f0-9]{64}',str(old_hash)) or sha256_file(old)!=old_hash:
        raise ValueError('已有译文已在外部改变，未覆盖文件')
    if old_hash==state['sourceHash'] or old.suffix.lower()!='.pdf':raise ValueError('不能覆盖原文')
    key=str(data['attachmentKey']);library=data['libraryID']
    if not re.fullmatch(r'[A-Z0-9]{8}',key) or not isinstance(library,int):raise ValueError('附件身份无效')
    if data.get('sourceHash')!=report['sourceHash'] or sha256_file(new_path)!=report['outputHash']:
        raise ValueError('译文身份校验失败')
    annotations=data.get('annotations',[])
    if not isinstance(annotations,list):raise ValueError('批注信息无效')
    signature=hashlib.sha256(json.dumps(annotations,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    canonical_hash=report['outputHash']
    def prepared(path,effective):
        delivery_id=hashlib.sha256(f'{library}:{key}:{effective["outputHash"]}:{signature}'.encode()).hexdigest()
        effective['deliveryId']=delivery_id
        atomic_json(service.data/'jobs'/job_id/'deliveries'/(delivery_id+'.json'),{
            'attachmentKey':key,'libraryID':library,'path':str(path),'report':effective})
        return {'backupPath':str(backup),'outputHash':effective['outputHash'],'resultPath':str(path),'report':effective}
    pages=set(); anchors={}
    for a in annotations:
        position=a.get('position',{})
        if isinstance(position,str):position=json.loads(position)
        n=position.get('pageIndex')
        if not isinstance(n,int) or n<0:raise ValueError('批注页码无法确认，未覆盖文件')
        pages.add(n)
        rects=position.get('rects',[])
        # Zotero annotation geometry uses bottom-left PDF coordinates.
        if rects:
            if n not in anchors:anchors[n]=[]
            if anchors[n] is not None:anchors[n].extend(rects)
        else:anchors[n]=None
        if position.get('nextPageRects'):
            pages.add(n+1)
            if n+1 not in anchors:anchors[n+1]=[]
            if anchors[n+1] is not None:anchors[n+1].extend(position['nextPageRects'])
    with fitz.open(old) as before,fitz.open(new_path) as after:
        if len(before)!=len(after) or any(a.rect!=b.rect for a,b in zip(before,after)):
            raise ValueError('译文页数或尺寸发生变化，未覆盖文件')
        for n in pages:
            if n>=len(before):raise ValueError('中文批注页码超出文档范围')
    folder=service.data/'backups'/f'{library}-{key}'/old_hash
    folder.mkdir(parents=True,exist_ok=True)
    backup=folder/'result.pdf'
    if not backup.exists():
        temp=folder/'result.tmp';shutil.copyfile(old,temp)
        if sha256_file(temp)!=old_hash:raise ValueError('备份校验失败')
        temp.replace(backup)
    if sha256_file(backup)!=old_hash:raise ValueError('已有备份校验失败')
    atomic_json(folder/'annotations.json',annotations)
    atomic_json(folder/'report.json',data.get('report',{}))
    previous=data.get('report',{})
    if (previous.get('canonicalOutputHash')==canonical_hash and previous.get('annotationSignature')==signature
        and previous.get('outputHash')==old_hash):
        return prepared(old,previous)
    locked=[]
    with fitz.open(old) as before,fitz.open(new_path) as after:
        for n in pages:
            regions=[fitz.Rect(r)*before[n].transformation_matrix for r in anchors[n]] if anchors[n] else [before[n].rect]
            for rect in regions:
                rect=(rect+(-2,-2,2,2))&before[n].rect
                if before[n].get_pixmap(clip=rect,matrix=fitz.Matrix(2,2)).samples==after[n].get_pixmap(clip=rect,matrix=fitz.Matrix(2,2)).samples:continue
                # Expand to whole affected text units, so no newly drawn paragraph
                # is left half-covered by the old annotation anchor.
                units=report.get('reading',{}).get('units',[])+data.get('report',{}).get('reading',{}).get('units',[])
                for _ in range(len(units)+1):
                    expanded=fitz.Rect(rect)
                    for u in units:
                        b=fitz.Rect(u.get('placedRegion',u['region']))
                        if u['page']==n+1 and (b&rect).get_area()>1:expanded|=b
                    expanded&=before[n].rect
                    if expanded==rect:break
                    rect=expanded
                p=after[n];p.add_redact_annot(rect,fill=(1,1,1));p.apply_redactions(images=0,graphics=0,text=0)
                p.show_pdf_page(rect,before,n,clip=rect)
                locked.append((n,rect))
        if locked:
            candidate=folder/(report['outputHash']+'-anchored.pdf')
            after.save(candidate,garbage=3,deflate=True,no_new_id=True)
            # The original visible anchor, not just its DB record, must survive.
            with fitz.open(candidate) as checked:
                for n,rect in locked:
                    if before[n].get_pixmap(clip=rect,matrix=fitz.Matrix(2,2)).samples!=checked[n].get_pixmap(clip=rect,matrix=fitz.Matrix(2,2)).samples:
                        raise ValueError('无法保持中文批注位置，已保留原附件，未覆盖')
            for u in report.get('reading',{}).get('units',[]):
                if any(u['page']==n+1 and (fitz.Rect(u.get('placedRegion',u['region']))&rect).get_area()>1 for n,rect in locked):
                    u.update(status='missing',reason='annotation_anchor')
            report.setdefault('issues',[]);report['issues'] += [{'page':n+1,'region':list(rect),'type':'annotation_anchor','fallback':'region'} for n,rect in locked]
            units=report.get('reading',{}).get('units',[])
            if units:report['coverage']={'total':sum(u['words'] for u in units if u['status']!='preserved'),
                'pdf':sum(u['words'] for u in units if u['status']=='pdf'),'missing':sum(u['words'] for u in units if u['status']=='missing'),'supplement':0}
            report.update(outputHash=sha256_file(candidate),status='completed_with_warnings',message='译文已更新；中文批注所在区域保留原版本')
            report.update(canonicalOutputHash=canonical_hash,annotationSignature=signature)
            return prepared(candidate,report)
    return prepared(new_path,report)
