"""One source-bound pipeline: identify -> translate -> typeset -> audit.

The output always begins with the original PDF, never with a patched revision.
"""
from __future__ import annotations
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from pathlib import Path
import pymupdf as fitz
from filelock import FileLock
from .config import atomic_json,sha256_file
from .paddle_cloud import CloudError,Cancelled
from .cloud_layout import (LAYOUT_SCHEMA,cloud_regions,inventory_native,raster_units,
                            IMAGE_LABELS,TABLE_LABELS,FORMULA_LABELS,intersection,area,table_cells,unit)
from .reading import (good_translation,MARKER,literal_text,natural_weight,safe_typeset,is_literal,
                       complex_math,tokens_for_line,make_input,union)
from .direct_pdf import chinese_style,prose_html,prose_counter

PIPELINE='paddle-cloud-layout6-compose3'

def deduplicate_raster(units):
    """Overlapping whole-figure/subfigure OCR must not own one label twice."""
    result=[]
    for u in units:
        text=literal_text(u['source'],u['objects']).strip()
        duplicate=next((v for v in result if u['kind']==v['kind']=='raster' and u['page']==v['page']
            and text==literal_text(v['source'],v['objects']).strip()
            and intersection(u['region'],v['region'])/max(.1,min(area(u['region']),area(v['region'])))>.8),None)
        if duplicate:
            duplicate['ocrObservations']=duplicate.get('ocrObservations',1)+1
        else:result.append(u)
    return result

def selected_pages(spec,count):
    if not spec:return list(range(count))
    result=set()
    for part in spec.split(','):
        values=[int(s) for s in part.strip().split('-')]
        if len(values)==1:values*=2
        if len(values)!=2 or not 1<=values[0]<=values[1]<=count:raise ValueError('页码范围无效')
        result.update(range(values[0]-1,values[1]))
    return sorted(result)

def normalize(source,target):
    with fitz.open(source) as doc:
        for p in doc:
            if p.rotation:p.remove_rotation()
        doc.save(target,garbage=3,deflate=True,no_new_id=True)

def chunks(document,pages,folder):
    """Bound both bytes and page count; split oversized batches before upload."""
    folder.mkdir(parents=True,exist_ok=True)
    def emit(indices):
        path=folder/('pages-'+ '-'.join(map(str,indices))+'.pdf')
        if not path.exists():
            with fitz.open() as part:
                for n in indices:part.insert_pdf(document,from_page=n,to_page=n)
                part.save(path,garbage=3,deflate=True,no_new_id=True)
        if path.stat().st_size>40_000_000:
            if len(indices)==1:raise CloudError(f'第 {indices[0]+1} 页超过上传限制，未截断页面')
            mid=len(indices)//2
            yield from emit(indices[:mid]);yield from emit(indices[mid:])
        else:yield indices,path
    for start in range(0,len(pages),20):yield from emit(pages[start:start+20])

def crop_file(page,region,folder,name):
    path=folder/(name+'.png')
    if not path.exists():page.get_pixmap(matrix=fitz.Matrix(3,3),clip=fitz.Rect(region),alpha=False).save(path)
    return path

def split_complex_units(page,units):
    """Keep intricate math anchored; translate surrounding text runs in-place."""
    result=[]
    for u in units:
        if u['status']!='missing' or not complex_math(u['objects'],page):result.append(u);continue
        raw=page.get_text('rawdict',clip=fitz.Rect(u['region']))
        children=[];anchors=[]
        for b in raw['blocks']:
            for line in b.get('lines',[]):
                parts=tokens_for_line(line);run=[]
                def flush():
                    if not run:return
                    text=''.join(p['text'] for p in run).strip();boxes=[p['box'] for p in run if p.get('box')]
                    if boxes and natural_weight(text):
                        region=list(union(boxes));key=hashlib.sha256((u['id']+text+str(region)).encode()).hexdigest()[:24]
                        children.append({**u,'id':key,'source':text,'objects':[],'region':region,
                                         'words':natural_weight(text),'context':u['source']})
                    run.clear()
                for part in parts:
                    if 'math' in part:
                        flush()
                        box=list(part['math']);key=hashlib.sha256((u['id']+str(box)).encode()).hexdigest()[:24]
                        anchors.append({**u,'id':key,'region':box,'source':'{v0}',
                            'objects':[{'box':box,'text':part['source'],'kind':'literal' if part.get('literal') else 'math'}],
                            'words':0,'status':'preserved','reason':'anchored_math','immutable':True})
                    else:run.append(part)
                flush()
        if children:
            # Every natural-language run is still counted; formulas remain native.
            result.extend(children+anchors)
        else:result.append(u)
    return result

def refine_table_units(page,units,cells):
    """Assign complete native lines exactly once; partial cell lists lose nothing."""
    lines=[l for b in page.get_text('rawdict')['blocks'] for l in b.get('lines',[])]
    result=[]
    for u in units:
        if u['label'] not in TABLE_LABELS:result.append(u);continue
        box=fitz.Rect(u['region'])+(-.01,-.01,.01,.01)
        own=[l for l in lines if box.contains(fitz.Rect(l['bbox']))]
        if not own or make_input({'lines':own})[0]!=u['source']:
            result.append(u);continue
        groups={};complete=True
        for line in own:
            matches=[i for i,c in enumerate(cells) if fitz.Rect(c).contains(fitz.Rect(line['bbox']))]
            if len(matches)!=1:complete=False;break
            groups.setdefault(matches[0],[]).append(line)
        if not complete:
            result.append(u);continue
        result.extend(unit(page,{'lines':g,'bbox':list(union([l['bbox'] for l in g]))},'table_cell') for g in groups.values())
    return result

def reference_policy(page,u):
    if u['label'] not in ('reference','references','reference_content'):return [u]
    # Detect title by actual quote glyphs and translate only the quoted span.
    raw=page.get_text('rawdict',clip=fitz.Rect(u['region']))
    selected=[];inside=False;seen=False
    for block in raw['blocks']:
        for line in block.get('lines',[]):
            spans=[]
            for span in line['spans']:
                chars=[]
                for c in span['chars']:
                    if c['c'] in ('“','”','"'):
                        inside=not inside;seen=True;continue
                    if inside:chars.append(c)
                if chars:spans.append({**span,'chars':chars,'bbox':list(union([c['bbox'] for c in chars]))})
            if spans:selected.append({'spans':spans,'bbox':list(union([s['bbox'] for s in spans]))})
    if not seen or not selected:
        return [{**u,'status':'preserved','reason':'bibliographic_identity'}]
    # Split by physical line so replacing a title never erases author/journal text.
    from .cloud_layout import unit
    return [unit(page,{'lines':[line],'bbox':line['bbox']},'reference_title') for line in selected]

def collect_inventory(source,cloud,cache,model,selected,progress,check,retry_empty_pages=()):
    result=[];page_layout=[];by_page={};crops=[]
    with fitz.open(source) as doc:
        for indices,path in chunks(doc,selected,cache/'batches'):
            check();progress('云端识别：解析页面布局',5,'recognizing')
            raws=cloud.parse(path,model,len(indices),{'useDocOrientationClassify':False,
                'useDocUnwarping':False,'useLayoutDetection':True,'useOcrForImageBlock':True})
            for n,raw in zip(indices,raws):
                from .native_flow import semantic_native
                check();page=doc[n];regions=cloud_regions(raw,page)
                native=semantic_native(page,regions,inventory_native(page,regions))
                units=[]
                for u in native:units.extend(reference_policy(page,u))
                for r in regions:
                    if r['label'] in FORMULA_LABELS:continue
                    contained=[u for u in native if intersection(u['region'],r['region'])/max(.1,area(u['region']))>.6]
                    need_ocr=r['label'] in IMAGE_LABELS or (r['text'].strip() and not contained)
                    if r['label'] in TABLE_LABELS:
                        crop=crop_file(page,r['region'],cache,f'table-{n}-{r["id"]}')
                        structure=cloud.parse(crop,'PP-StructureV3',1,{'useDocOrientationClassify':False,
                            'useDocUnwarping':False,'useTableRecognition':True})[0]
                        atomic_json(cache/f'table-{n}-{r["id"]}.json',structure)
                        cells=table_cells(structure,r['region'])
                        if cells:
                            units=refine_table_units(page,units,cells)
                            native=refine_table_units(page,native,cells)
                        # Hybrid tables can have both native and rasterized cells.
                        # Always inspect the crop; deduplicate individual matching boxes.
                        need_ocr=True
                    if need_ocr:
                        crop=crop_file(page,r['region'],cache,f'crop-{n}-{r["id"]}')
                        crops.append({'page':n,'region':r['region'],'path':crop,'label':r['label'],'native':native})
                # A scan without layout text must not be misreported as complete.
                if not units and page.get_images() and not any(c['page']==n for c in crops):
                    crops.append({'page':n,'region':list(page.rect),'path':crop_file(page,page.rect,cache,f'page-{n}'),
                                  'label':'text','native':native})
                by_page[n]=units;page_layout.append({'page':n+1,'regions':regions})
        for item,ocr in recognize_crops(crops,cloud,cache,progress,check,retry_empty_pages):
            for u in raster_units(doc[item['page']],item['region'],ocr,item['label']):
                if any(intersection(u['region'],a['region'])>area(u['region'])*.6 for a in item['native']):continue
                by_page[item['page']].append(u)
        for n,units in by_page.items():
            units=deduplicate_raster(units)
            page=doc[n]
            if not units and page.get_images():
                units=[{'id':f'unrecognized-{n}','page':n+1,'region':list(page.rect),'source':'',
                    'objects':[],'fontSize':10,'label':'unrecognized','kind':'raster','words':0,
                    'status':'missing','reason':'recognition_empty'}]
            # Semantic units own their inline glyphs already. Splitting their
            # enclosing boxes again would duplicate prose and formula fragments.
            for u in units:
                result.extend([u] if u.get('semantic') else split_complex_units(page,[u]))
    return {'schema':LAYOUT_SCHEMA,'units':result,'layouts':page_layout}


def recognize_crops(crops,cloud,cache,progress,check,retry_empty_pages=()):
    """Submit multiple image regions as PDF pages, avoiding a job per label.

    Each batch retains an explicit page-to-crop mapping. Rasterization and PDF
    construction stay on this thread; the OCR API is unchanged.
    """
    options={'useDocOrientationClassify':False,'useDocUnwarping':False}
    from .paddle_cloud import OCR_SCHEMA,RESULT_SCHEMA,has_ocr_text
    crop_cache_dir=cache/'ocr-crops';crop_cache_dir.mkdir(exist_ok=True)
    def path(item):return crop_cache_dir/(sha256_file(item['path'])+'.json')
    def load(item):
        try:
            v=json.loads(path(item).read_text(encoding='utf-8'))
            if v.get('schema')==[OCR_SCHEMA,RESULT_SCHEMA] and v.get('options')==options:
                if item.get('page') in retry_empty_pages and not has_ocr_text(v['raw']):return None
                return v['raw']
        except (OSError,ValueError):pass
        return None
    def save(item,raw):atomic_json(path(item),{'schema':[OCR_SCHEMA,RESULT_SCHEMA],'options':options,'raw':raw})
    missing=[c for c in crops if load(c) is None]
    if missing and hasattr(cloud,'cached'):
        for item in missing:
            raw=cloud.cached(item['path'],'PP-OCRv6',1,options)
            if raw is not None:save(item,raw[0])
        missing=[c for c in missing if load(c) is None]
        # Migrate previous multi-crop jobs without resubmitting them just because
        # the new native classifier needs a different subset/order of crops.
        def pixel_key(pix):return (pix.width,pix.height,hashlib.sha256(pix.samples).hexdigest())
        lookup={pixel_key(fitz.Pixmap(c['path'])):c for c in missing}
        for old in (cache/'ocr-batches').glob('*/parts/*.pdf'):
            check()
            with fitz.open(old) as part:
                raws=cloud.cached(old,'PP-OCRv6',len(part),options)
                if raws is None:continue
                for page,raw in zip(part,raws):
                    images=page.get_images()
                    if len(images)!=1:continue
                    item=lookup.get(pixel_key(fitz.Pixmap(part,images[0][0])))
                    if item is not None:save(item,raw)
    pending=[]
    for item in crops:
        check();raw=load(item)
        if raw is not None:yield item,raw
        else:pending.append(item)
    for offset in range(0,len(pending),20):
        group=pending[offset:offset+20];check()
        identity=hashlib.sha256(json.dumps([sha256_file(c['path']) for c in group]).encode()).hexdigest()
        folder=cache/'ocr-batches'/identity;folder.mkdir(parents=True,exist_ok=True)
        file=folder/'crops.pdf'
        if not file.exists():
            with fitz.open() as batch:
                for item in group:
                    pix=fitz.Pixmap(item['path'])
                    page=batch.new_page(width=pix.width/2,height=pix.height/2)
                    page.insert_image(page.rect,filename=str(item['path']))
                batch.save(file,garbage=3,deflate=True,no_new_id=True)
        with fitz.open(file) as batch:
            for indices,part in chunks(batch,list(range(len(group))),folder/'parts'):
                progress(f'图中文字识别：{offset+1}–{offset+len(group)}/{len(crops)} 个区域（批量）',20,'recognizing')
                if any(group[i].get('page') in retry_empty_pages for i in indices) and hasattr(cloud,'refresh_empty'):
                    cloud.refresh_empty(part,'PP-OCRv6',len(indices),options)
                raws=cloud.parse(part,'PP-OCRv6',len(indices),options)
                if len(raws)!=len(indices):raise CloudError('图片批次返回不完整，已保留进度')
                for index,raw in zip(indices,raws):save(group[index],raw);yield group[index],raw

def translation_failure_reason(unit,translator):
    # A short chart token can be a variable or an OCR fragment. Two successful
    # responses that deliberately echo it are different from a network failure.
    # Keep it visibly unfinished; repeated clicks must not recompose the paper
    # just to demand Chinese for the same unresolved symbol again.
    if unit['kind']=='raster' and re.fullmatch(r'[A-Za-z]{2,16}',unit['source'].strip()):
        records=[r for r in list(getattr(translator,'records',())) if r.get('source')==unit['source']][-2:]
        if len(records)==2 and all(r.get('issue')=='reading_content_incomplete' and
                r.get('translation','').strip()==unit['source'].strip() for r in records):
            return 'unresolved_ocr_label'
    return 'translation_incomplete'


def translate_units(units,translator,old_reports,progress,check):
    memory={}
    for report in old_reports:
        for u in report.get('reading',{}).get('units',[]):
            if u.get('translation'):memory[u.get('source','')]=u['translation']
    active=[u for u in units if u['status']=='missing' and u.get('reason') not in
            ('ocr_needs_review','ocr_math_alignment','recognition_empty')]
    pending=[];done=0
    progress('检查已有译文缓存',25,'translating')
    for i,u in enumerate(active):
        check()
        validate=lambda value:good_translation(u['source'],value,u['objects'])
        known=memory.get(u['source'])
        if known and validate(known):u['translation']=known;done+=1;continue
        if hasattr(translator,'translate_batch_checked'):
            pending.append(u);continue
        progress(f'翻译内容：{i+1}/{len(active)}',25+int(45*i/max(1,len(active))),'translating')
        translated=translator.translate_checked(u['source'],validate)
        if not validate(translated):translated=translator.translate_checked(u['source'],validate,retry=True)
        if validate(translated):u['translation']=translated
        else:u['reason']=translation_failure_reason(u,translator)
    if not pending:return
    # Small coherent batches bound token use; workers overlap network latency.
    batches=[];batch=[];size=0
    for u in pending:
        if batch and (len(batch)>=8 or size+len(u['source'])>4500):batches.append(batch);batch=[];size=0
        batch.append(u);size+=len(u['source'])
    if batch:batches.append(batch)
    def run(batch):
        check()
        rows=[{'id':u['id'],'input':u['source'],
               'validator':lambda value,u=u:good_translation(u['source'],value,u['objects'])} for u in batch]
        results=translator.translate_batch_checked(rows)
        failed=[r for r in rows if not r['validator'](results.get(r['id'],''))]
        if failed:
            check();results.update(translator.translate_batch_checked(failed,retry=True))
        return results
    pool=ThreadPoolExecutor(max_workers=4);inflight={};iterator=iter(batches)
    try:
        while True:
            check()
            while len(inflight)<4:
                batch=next(iterator,None)
                if batch is None:break
                inflight[pool.submit(run,batch)]=batch
            if not inflight:break
            completed,_=wait(inflight,timeout=.2,return_when=FIRST_COMPLETED)
            for future in completed:
                batch=inflight.pop(future);results=future.result()
                for u in batch:
                    translated=results.get(u['id'],'')
                    if good_translation(u['source'],translated,u['objects']):u['translation']=translated;u.pop('reason',None)
                    else:u['reason']=translation_failure_reason(u,translator)
                done+=len(batch)
                progress(f'翻译内容：已处理 {done}/{len(active)} 段（译文已保存）',25+int(45*done/max(1,len(active))),'translating')
    finally:
        # At most four in-flight requests finish into durable checkpoints. No
        # further batches are submitted after cancellation or quota failure.
        pool.shutdown(wait=True,cancel_futures=True)

def typeset_raster(document,index,u,obstacles):
    """Replace only text on a verified flat background, leaving chart strokes intact."""
    from PIL import Image
    from .raster_text import crossing_path,has_solid_marker
    page=document[index];source=fitz.Rect(u['region'])
    from .pdf_transaction import copy_artwork,commit_artwork
    # OCR polygons touch ink at their edge. Search a small blank margin instead
    # of rejecting every antialiased character touching the original polygon.
    for margin in (.2,.7,1.5,2.5):
        box=source+(-margin,-margin,margin,margin)
        if not page.rect.contains(box) or any(intersection(box,b)>1 for b in obstacles):continue
        if crossing_path(page,box):continue
        pix=page.get_pixmap(matrix=fitz.Matrix(3,3),clip=box,alpha=False)
        image=Image.frombytes('RGB',(pix.width,pix.height),pix.samples);pixels=image.load();border=[]
        for x in range(image.width):border.extend([pixels[x,0],pixels[x,image.height-1]])
        for y in range(image.height):border.extend([pixels[0,y],pixels[image.width-1,y]])
        background=Counter(border).most_common(1)[0][0]
        if any(max(abs(a-b) for a,b in zip(c,background))>12 for c in border):continue
        # A flat outer border does not prove that the interior is only text.
        # Reject mixed-colour foreground (e.g. a black label plus a red legend
        # marker); whole-line OCR cannot safely separate those components.
        inks=[c for c in image.getdata() if max(abs(a-b) for a,b in zip(c,background))>55]
        chromatic=sum(max(c)-min(c)>35 for c in inks)
        neutral=sum(max(c)-min(c)<15 and min(c)<180 for c in inks)
        if chromatic>3 and neutral>3:continue
        if has_solid_marker(image,background):continue
        trial=fitz.open();copy_artwork(document,index,trial)
        try:
            p=trial[0];p.draw_rect(box,color=None,fill=tuple(c/255 for c in background),overlay=True)
            text=literal_text(u['translation'],u['objects']);archive,css=chinese_style(u['fontSize'],u['fontSize']*1.05)
            css+='body{padding:0;}'
            rotation=90 if source.height>source.width*1.5 and len(text)>1 else 0
            spare,scale=p.insert_htmlbox(box,prose_html(text),css=css,archive=archive,scale_low=.85,rotate=rotation)
            if spare<0 or prose_counter(text)-prose_counter(p.get_text(clip=box)):continue
            commit_artwork(document,index,trial);return list(box)
        finally:trial.close()
    return None

def assemble(source,target,inventory,config,progress,check):
    units=inventory['units']
    with fitz.open(source) as original,fitz.open(source) as output,ExitStack() as scratch:
        for n in range(len(original)):
            check();page_units=[u for u in units if u['page']==n+1];staged=[]
            layout=next((x for x in inventory['layouts'] if x['page']==n+1),{'regions':[]})
            anchors=[fitz.Rect(u['region'])+(-.5,-.5,.5,.5) for u in page_units if u.get('immutable')]
            glyph_anchors=tuple(anchors)
            # Native-discovered fraction bars and radicals need protection too.
            for drawing in original[n].get_drawings():
                rect=fitz.Rect(drawing['rect'])+(-.5,-.5,.5,.5)
                if any(rect.width<b.width*2+4 and rect.height<b.height*3 and
                    intersection(rect,b+(-2,-b.height,2,b.height))>0 for b in glyph_anchors):anchors.append(rect)
            for u in page_units:
                if not u.get('translation') or u['status']=='preserved':continue
                protected=list(anchors)
                for r in layout['regions']:
                    if r['label'] not in FORMULA_LABELS:continue
                    box=fitz.Rect(r['region'])
                    own_inline=(r['label']=='inline_formula' and any(not is_literal(o) and
                        (box+(-2,-2,2,2)).contains(fitz.Rect(o['box'])) for o in u['objects']))
                    if not own_inline:protected.append(box)
                check();progress(f'排版：第 {n+1}/{len(original)} 页',72+int(23*n/len(original)),'typesetting')
                others=[fitz.Rect(v.get('placedRegion',v['region'])) for v in page_units if v['id']!=u['id']]+protected
                if u['kind']=='native':
                    if u.get('semantic') and not u.get('completeMath') and any(not is_literal(o) for o in u['objects']):
                        u['reason']='math_artwork_ambiguous';continue
                    placed=safe_typeset(output,n,fitz.Rect(u['region']),u['translation'],u['fontSize'],
                        protected,others,original[n],u['objects'],complete_math=u.get('completeMath',False),
                        remove_formula_paths=u.get('removeFormulaPaths',False),formula_path_masks=u.get('formulaPathMasks'),
                        formula_path_rects=u.get('formulaPathRects'),staged=staged)
                    if placed and staged:
                        document=staged[-1]['document']
                        scratch.callback(lambda d=document:d.close() if not d.is_closed else None)
                else:placed=typeset_raster(output,n,u,others+protected)
                if placed:u.update(status='pdf',placedRegion=placed);u.pop('reason',None)
                else:u['reason']='layout_pending'
            # Redact original artwork before adding ANY translated vector clip.
            # Re-redacting imported Form XObjects can change shared clipping
            # streams and render differently in PDF.js/Poppler versus MuPDF.
            try:
                page=output[n]
                for item in staged:
                    for box in item['masks']:page.add_redact_annot(box,fill=None)
                if staged:page.apply_redactions(images=0,graphics=0,text=0)
                for item in staged:
                    for box in item['formulaCovers']:page.draw_rect(box,color=None,fill=(1,1,1),overlay=True)
                for item in staged:page.show_pdf_page(item['area'],item['document'],0,clip=item['area'])
            finally:
                for item in staged:item['document'].close()
        # Trial page imports may contain duplicate image/font streams. Deduplicate
        # them at final save; keep page/annotation objects and their targets intact.
        output.save(target,garbage=4,deflate=True,no_new_id=True)

def report_for(source,result,inventory,state):
    units=inventory['units'];pending=[u for u in units if u['status']=='missing']
    coverage={'total':sum(u['words'] for u in units if u['status']!='preserved'),
              'pdf':sum(u['words'] for u in units if u['status']=='pdf'),
              'missing':sum(u['words'] for u in pending),'supplement':0}
    coverage['byKind']={kind:{key:sum(u['words'] for u in units if u['kind']==kind and
        (u['status']!='preserved' if key=='total' else u['status']==key)) for key in ('total','pdf','missing')}
        for kind in ('native','raster')}
    issues=[{'page':u['page'],'region':u['region'],'type':u.get('reason','missing_content'),
             'fallback':'region','unitId':u['id']} for u in pending]
    with fitz.open(source) as before,fitz.open(result) as after:
        if len(before)!=len(after) or any(a.rect!=b.rect for a,b in zip(before,after)):
            raise ValueError('输出页数或尺寸不匹配')
        pages=len(before)
        for u in units:
            if u['status']=='pdf':
                expected=prose_counter(MARKER.sub('',literal_text(u['translation'],u['objects'])))
                actual=prose_counter(after[u['page']-1].get_text(clip=u['placedRegion']))
                if expected-actual:raise ValueError('最终 PDF 中译文不完整，已保留旧文件和任务进度')
    inventory_hash=inventory_digest(units)
    return {'schema':3,'pipeline':PIPELINE,'sourceHash':state['sourceHash'],'inventoryHash':inventory_hash,
            'configFingerprint':state['configFingerprint'],'outputHash':sha256_file(result),
            'status':'completed_with_warnings' if issues else 'completed','pages':pages,'issues':issues,
            'completionRevision':PIPELINE,
            'message':(f'正文已写入 PDF，图中文字仍有 {len(issues)} 处待核对' if not coverage['byKind']['native']['missing']
                else f'译文已更新，仍有 {len(issues)} 处未完成，请查看核对入口') if issues else '翻译完成，已保存本机存档',
            'coverage':coverage,'reading':inventory,'requestedPages':state.get('pages'),'preserved':[]}

def inventory_digest(units):
    return hashlib.sha256(json.dumps([{k:u[k] for k in ('id','page','region','source','words','kind')} for u in units],
        ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def audit_archive(source,pdf,report,*,pages=None):
    if report.get('reading',{}).get('schema')!=LAYOUT_SCHEMA or report.get('pipeline')!=PIPELINE:
        return {'needsWork':True,'reason':'recognition_upgrade'}
    if report.get('sourceHash')!=sha256_file(source) or report.get('outputHash')!=sha256_file(pdf):
        return {'needsWork':True,'reason':'file_changed'}
    units=report['reading']['units']
    if report.get('inventoryHash')!=inventory_digest(units):return {'needsWork':True,'reason':'inventory_changed'}
    pending=[u for u in units if u['status']=='missing']
    retryable=any(u.get('reason') in ('translation_incomplete','recognition_empty') for u in pending)
    needs_retry=bool(pending) and (report.get('completionRevision')!=PIPELINE or retryable)
    with fitz.open(pdf) as document:
        for u in units:
            if u['status']=='pdf':
                text=literal_text(u['translation'],u['objects'])
                # Formula clips add their original glyphs; translated prose must all exist.
                expected=prose_counter(MARKER.sub('',text))
                if expected-prose_counter(document[u['page']-1].get_text(clip=u['placedRegion'])):
                    return {'needsWork':True,'reason':'visible_text_missing'}
    return {'needsWork':needs_retry,'reason':'missing_content' if needs_retry else
            'layout_attempt_exhausted' if pending else 'local_audit','coverage':report['coverage'],
            'unfinished':len(pending)}

def run_pipeline(source,job,data,state,config,cloud,translator,progress,check,old_reports=()):
    timings={};started=time.monotonic()
    def record(phase,start):
        timings[phase]=round(time.monotonic()-start,3)
        atomic_json(job/'timings.json',{'seconds':timings,'translation':getattr(translator,'metrics',{})})
    normalized=job/'normalized.pdf'
    if not normalized.exists():normalize(source,normalized)
    cache=data/'cache'/'layout'/state['sourceHash']/config['ocrModel'];cache.mkdir(parents=True,exist_ok=True)
    with fitz.open(normalized) as doc:selected=selected_pages(state.get('pages'),len(doc))
    inventory_path=cache/('inventory-'+hashlib.sha256(json.dumps([LAYOUT_SCHEMA,selected]).encode()).hexdigest()+'.json')
    inventory=json.loads(inventory_path.read_text(encoding='utf-8')) if inventory_path.exists() else None
    retry_empty_pages={u['page']-1 for u in (inventory or {}).get('units',[]) if u.get('reason')=='recognition_empty'}
    if inventory is None or retry_empty_pages:
        inventory=collect_inventory(normalized,cloud,cache,config['ocrModel'],selected,progress,check,retry_empty_pages)
        atomic_json(inventory_path,inventory)
    record('recognition',started);started=time.monotonic()
    translate_units(inventory['units'],translator,old_reports,progress,check)
    record('translation',started)
    atomic_json(job/'inventory.json',inventory)
    # One compositor across workers; its OS lock is released after cancellation/crash.
    lock=FileLock(str(data/'cache'/'typesetting.lock'))
    started=time.monotonic()
    while True:
        check()
        try:lock.acquire(timeout=.2);break
        except TimeoutError:progress('等待本机排版',70,'typesetting')
    record('typesettingWait',started);started=time.monotonic()
    try:assemble(normalized,job/'result.pdf',inventory,config,progress,check)
    finally:lock.release()
    record('typesetting',started);started=time.monotonic()
    check();progress('核验译文、页数和内容清单',97,'verifying')
    report=report_for(source,job/'result.pdf',inventory,state)
    record('verification',started);report['timings']={'seconds':timings,'translation':getattr(translator,'metrics',{})}
    atomic_json(job/'report.json',report)
    return report
