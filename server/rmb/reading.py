"""Audit and improve the visible translated PDF, retaining reusable text locally."""
import base64
from collections import Counter
import hashlib
import html
import json
import re
import statistics
import unicodedata
import pymupdf as fitz
from .config import atomic_json, sha256_file
from .structure import reference_regions

WORD=re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
MARKER=re.compile(r'\{v(\d+)\}')
MATH_FONT=re.compile(r'(?:math|symbol|cmsy|cmmi|cmex|msam|msbm|rsfs|mtmi|mtsyn|AdvSPS|AdvPi)',re.I)
READING_SCHEMA=4

def object_box(value):return fitz.Rect(value['box'] if isinstance(value,dict) else value)
def is_literal(value):return isinstance(value,dict) and value.get('kind')=='literal'
def literal_text(text,objects):
    return MARKER.sub(lambda m:objects[int(m[1])]['text'] if is_literal(objects[int(m[1])]) else m[0],text)

def cjk(text):return sum('\u4e00'<=c<='\u9fff' for c in text)
def union(boxes):
    result=fitz.Rect(boxes[0])
    for box in boxes[1:]:result|=fitz.Rect(box)
    return result

def tokens_for_line(line):
    """Reconstruct words across font fragments before classifying italic words."""
    chars=[]
    for span in line.get('spans',[]):
        for char in span.get('chars',[]):chars.append({**char,'font':span.get('font',''),'size':span.get('size',10),'flags':span.get('flags',0)})
    text=''.join(c['c'] for c in chars)
    size=statistics.median([c['size'] for c in chars if c['c'].isalpha()] or [10])
    baseline=statistics.median([c['origin'][1] for c in chars if c['c'].isalpha() and c['size']>=size*.9] or [0])
    parts=[]
    for match in re.finditer(r'\S+|\s+',text):
        group=chars[match.start():match.end()];value=match.group()
        if not value.strip():parts.append({'text':' '});continue
        # Whole ordinary italic words are prose. Single letter variables,
        # super/subscript runs, digits, operators and math fonts stay atomic.
        script=any(c['size']<size*.8 and abs(c['origin'][1]-baseline)>size*.12 for c in group)
        operator=any(unicodedata.category(c['c']) in ('Sm','Co') or '\u0370'<=c['c']<='\u03ff' for c in group)
        special=any(MATH_FONT.search(c['font']) for c in group)
        letters=''.join(c['c'] for c in group if c['c'].isalpha())
        variable=len(letters)==1 and letters not in ('a','A','I')
        italic=any(re.search('ital|oblique|Adv.*-I|Adv.*-BI',c['font'],re.I) or c['flags']&2 for c in group)
        ordinary_short={'a','an','and','as','at','be','by','for','if','in','is','it','of','on','or','the','to','we'}
        italic_variable=italic and (len(letters)==1 or bool(re.search(r'[a-z][A-Z]|_',value)))
        mathematical=script or operator or special or variable or italic_variable or any(c['c'].isdigit() for c in group)
        if mathematical:
            # Numbers/IDs embedded in ordinary prose must stay exact, but they
            # are not formula images. Preserve them as literal placeholders.
            literal=not (script or operator or special or variable or italic_variable)
            if re.fullmatch(r'\([A-Za-z]\)',value) and not (script or special):literal=True
            parts.append({'math':union([c['bbox'] for c in group]),'source':value,'literal':literal})
        else:parts.append({'text':value,'box':list(union([c['bbox'] for c in group]))})
    return parts

def make_input(block):
    # Rejoin a typographic drop cap with the rest of its word before treating
    # single letters as mathematical variables (e.g. a large A + normal S AN).
    lines=block.get('lines',[])
    if len(lines)>1 and all('bbox' in l for l in lines[:2]):
        first=''.join(c['c'] for s in lines[0]['spans'] for c in s.get('chars',[]))
        a=fitz.Rect(lines[0]['bbox']);b=fitz.Rect(lines[1]['bbox'])
        if len(first)==1 and first.isupper() and a.height>b.height*1.8 and b.y0<a.y1 and 0<=b.x0-a.x1<10:
            joined={'spans':lines[0]['spans']+lines[1]['spans']}
            block={**block,'lines':[joined]+lines[2:]}
    pieces=[];objects=[]
    for line in block.get('lines',[]):
        parts=tokens_for_line(line)
        # Adjacent mathematical tokens and their whitespace form one source clip.
        i=0
        while i<len(parts):
            part=parts[i]
            if 'math' in part:
                boxes=[part['math']];sources=[part['source']];literal=part.get('literal',False);j=i+1
                while j+1<len(parts) and parts[j].get('text')==' ' and 'math' in parts[j+1]:
                    boxes.append(parts[j+1]['math']);sources.append(parts[j+1]['source']);literal=literal and parts[j+1].get('literal',False);j+=2
                objects.append({'box':list(union(boxes)),'text':' '.join(sources),'kind':'literal' if literal else 'math'})
                pieces.append('{v'+str(len(objects)-1)+'}');i=j
            else:pieces.append(part['text']);i+=1
        pieces.append('\n')
    # PDF line-end hyphenation is reconstructed only between letters.
    value=''.join(pieces).strip()
    value=re.sub(r'(?<=[A-Za-z])-\n(?=[a-z])','',value)
    value=re.sub(r'\s+',' ',value)
    # A leading section enumerator is a label, not an inline variable. Keep its
    # exact text while allowing the heading to use the normal Chinese font.
    if (len(block.get('lines',[]))<=2 and value.startswith('{v0} ') and objects
        and re.fullmatch(r'[A-Z]\.',objects[0]['text']) and natural_weight(value)>=3):
        objects[0]['kind']='literal'
    return value,objects

def natural_weight(value):return len(WORD.findall(MARKER.sub('',value)))

def pdf_body_present(source,current):
    # Acronyms intentionally retained in Chinese prose are not untranslated
    # sentences (ALNS / DIC / EDH / API). Keep the CJK and prose checks.
    prose=lambda t:[w.lower() for w in WORD.findall(t) if not(len(w)>1 and w.isupper())]
    words=Counter(prose(MARKER.sub('',source)))
    remaining=Counter(prose(current))
    weight=sum(words.values());shared=sum((words&remaining).values())
    return cjk(current)>=max(2,weight*.65) and shared<max(4,weight*.2)

def reading_blocks(page):
    """Retain complex formula bands, translate the prose above/below them."""
    from .direct_pdf import native_paragraphs
    result=[]
    for block in native_paragraphs(page):
        text,objects=make_input(block)
        # Author-name lists are metadata, not prose to invent/transliterate.
        words=WORD.findall(text)
        if page.number==0 and text.count(',')>=3 and words and sum(w[0].isupper() for w in words)>len(words)*.8:
            continue
        if not complex_math(objects,page):result.append(block);continue
        # Split only complete horizontal bands without math. Every math-bearing
        # line (including detached superscripts/denominators) stays anchored.
        lines=block['lines'];mathlines=[]
        for line in lines:
            parts=tokens_for_line(line)
            if any('math' in p and not p.get('literal') for p in parts):mathlines.append(fitz.Rect(line['bbox']))
        groups=[];current=[]
        for line in lines:
            b=fitz.Rect(line['bbox'])
            blocked=any(min(b.y1,m.y1)>max(b.y0,m.y0) for m in mathlines)
            if blocked:
                if current:groups.append(current);current=[]
            else:current.append(line)
        if current:groups.append(current)
        children=[{**block,'bbox':tuple(union([l['bbox'] for l in g])),'lines':g} for g in groups]
        # The retained formula/prose band remains in the inventory so any
        # untranslated English there is honestly counted as unfinished.
        used={id(l) for b in children for l in b['lines']}
        retained=[l for l in lines if id(l) not in used]
        if retained:children.append({**block,'bbox':tuple(union([l['bbox'] for l in retained])),'lines':retained})
        result.extend(children)
    return result

def complex_math(objects,page):
    objects=[object_box(o) for o in objects if not is_literal(o)]
    if not objects:return False
    cached=getattr(page,'_rmb_math_layout',None)
    if cached is None:
        candidates=[]
        for block in page.get_text('rawdict')['blocks']:
            for line in block.get('lines',[]):
                parts=tokens_for_line(line)
                isolated=not any(WORD.search(p.get('text','')) for p in parts)
                candidates += [(p['math'],isolated) for p in parts if 'math' in p and not p.get('literal')]
        cached=(candidates,page.get_drawings());page._rmb_math_layout=cached
    candidates,drawings=cached
    for box in objects:
        for other,isolated in candidates:
            if all(abs(a-b)<.1 for a,b in zip(box,other)):continue
            horizontal=min(box.x1,other.x1)-max(box.x0,other.x0)
            vertical=max(0,max(box.y0,other.y0)-min(box.y1,other.y1))
            if isolated and horizontal>0 and vertical<max(box.height,other.height)*1.3 and abs((box.y0+box.y1-other.y0-other.y1)/2)>min(box.height,other.height)*.6:
                return True
        if box.height>max(30,box.width*2):return True
        # Fractions, radicals and multiline group brackets can contain vector
        # strokes outside glyph boxes; never rebuild them from separate crops.
        nearby=box+(-4,-box.height,4,box.height)
        for drawing in drawings:
            rect=fitz.Rect(drawing['rect'])
            if rect.width<max(80,box.width*3+15) and (nearby & (rect+(-.5,-.5,.5,.5))).get_area()>0:return True
    return False

def segments_from_translation(text,objects,page,reference_only=False,region=None):
    result=[];position=0
    for match in MARKER.finditer(text):
        if match.start()>position:result.append({'text':text[position:match.start()]})
        index=int(match.group(1));obj=objects[index]
        if is_literal(obj):
            result.append({'text':obj['text']});position=match.end();continue
        if reference_only and complex_math([obj],page):
            result.append({'reference':True,'label':'〔原式〕','sourceRegion':list(object_box(obj))})
            position=match.end();continue
        box=(object_box(obj)+(-.5,-.5,.5,.5))&page.rect
        scale=min(2.2,1200/max(1,box.width),700/max(1,box.height))
        pix=page.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=box,alpha=False)
        result.append({'image':'data:image/png;base64,'+base64.b64encode(pix.tobytes('png')).decode(),
                       'width':box.width,'height':box.height,'alt':'原文数学对象','sourceRegion':list(box)})
        position=match.end()
    if position<len(text):result.append({'text':text[position:]})
    return result

def good_translation(source,text,objects):
    expected=Counter(MARKER.findall(source));actual=Counter(MARKER.findall(text))
    prose=lambda value:Counter(w.lower() for w in WORD.findall(MARKER.sub('',value)) if not w.isupper())
    original=prose(source)
    if sum((original&prose(text)).values())>=max(5,sum(original.values())*.6):return False
    return (cjk(text)>=max(1,natural_weight(source)*.65) and expected==actual and all(int(k)<len(objects) for k in actual)
            and not re.search(r'<\s*/?style\b',text) and len(text)<=max(200,len(source)*4))

def overlapping(a,b,fraction=.02):
    return (a&b).get_area()>max(1,min(a.get_area(),b.get_area())*fraction)

def protected_block(block,regions):
    # Native PDF blocks can enclose wide blank areas or both reference columns.
    # Classify actual glyphs, not the area of that loose enclosing rectangle.
    chars=[c for line in block.get('lines',[]) for span in line.get('spans',[]) for c in span.get('chars',[]) if c['c'].strip()]
    if not chars:return False
    count=0
    for char in chars:
        box=fitz.Rect(char['bbox']);center=fitz.Point((box.x0+box.x1)/2,(box.y0+box.y1)/2)
        count+=any(r.contains(center) for r in regions)
    return count>len(chars)*.6

def restore_english_regions(report,page):
    return [None if i.get('fallback')=='page' else fitz.Rect(i['region']) if i.get('region') else None
            for i in report.get('issues',[]) if i['page']==page]

def same_original_text(original,current,area):
    from .quality import drawn_glyphs
    expected=[g for g in drawn_glyphs(original) if (fitz.Rect(g['bbox'])&area).get_area()>0]
    actual=[g for g in drawn_glyphs(current) if (fitz.Rect(g['bbox'])&area).get_area()>0]
    if len(expected)!=len(actual):return False
    for glyph in expected:
        x,y=glyph['origin']
        match=next((i for i,g in enumerate(actual) if g['c']==glyph['c'] and abs(g['origin'][0]-x)<.2 and abs(g['origin'][1]-y)<.2),None)
        if match is None:return False
        actual.pop(match)
    return True

def replacement_masks(original,current,area):
    """Allow neighbouring font boxes at an edge without erasing their glyphs."""
    from .quality import drawn_glyphs
    def center(g):
        b=fitz.Rect(g['bbox']);return fitz.Point((b.x0+b.x1)/2,(b.y0+b.y1)/2)
    expected=[g for g in drawn_glyphs(original) if area.contains(center(g))]
    actual=[g for g in drawn_glyphs(current) if (fitz.Rect(g['bbox'])&area).get_area()>0]
    if not expected:return None
    for g in expected:
        x,y=g['origin']
        i=next((i for i,a in enumerate(actual) if a['c']==g['c'] and abs(a['origin'][0]-x)<.2 and abs(a['origin'][1]-y)<.2),None)
        if i is None:return None
        actual.pop(i)
    if any(area.contains(center(g)) for g in actual):return None
    if not actual:return [area]
    masks=[]
    for g in expected:
        point=center(g);mask=fitz.Rect(point.x-.05,point.y-.05,point.x+.05,point.y+.05)
        if any((mask&fitz.Rect(a['bbox'])).get_area()>0 for a in actual):return None
        masks.append(mask)
    return masks

def safe_typeset(document,page_index,region,text,size,protected,other_regions,source_page,objects=None,*,complete_math=False,remove_formula_paths=False,formula_path_masks=None,formula_path_rects=None,staged=None):
    """Try independent prose fitting; reject any loss outside the region."""
    from .quality import loses_unrestored_glyphs,render_errors
    from .direct_pdf import compose_inline,chinese_style,prose_counter,prose_html
    objects=objects or []
    if not complete_math and complex_math(objects,source_page):return None
    page=document[page_index]
    candidates=[region]
    # Adjacent font boxes may overlap even when their visible ink is separate.
    # Trim the target at a neighbouring line, then let exact glyph masks and
    # outside-glyph preservation decide whether this is safe.
    edges=[r.y0 for r in other_regions if region.y1-size*.3<r.y0<region.y1 and min(r.x1,region.x1)>max(r.x0,region.x0)]
    if edges:candidates.insert(0,fitz.Rect(region.x0,region.y0,region.x1,min(edges)-.1))
    upper_edges=[r.y1 for r in other_regions if region.y0<r.y1<region.y0+size*.3 and min(r.x1,region.x1)>max(r.x0,region.x0)]
    if upper_edges:candidates.insert(0,fitz.Rect(region.x0,max(upper_edges)+.1,region.x1,region.y1))
    # Reuse whitespace down to the next block, keeping the original column width.
    bottom=min([r.y0-1 for r in other_regions if r.y0>=region.y1 and min(r.x1,region.x1)>max(r.x0,region.x0)]+[page.rect.y1-12])
    if bottom>region.y1+.1:candidates.append(fitz.Rect(region.x0,region.y0,region.x1,min(bottom,region.y1+size*3)))
    top=max([r.y1+1 for r in other_regions if r.y1<=region.y0 and min(r.x1,region.x1)>max(r.x0,region.x0)]+[12])
    if top<region.y0-.1:candidates.append(fitz.Rect(region.x0,max(top,region.y0-size*3),region.x1,region.y1))
    for area in candidates:
        if not page.rect.contains(area) or any(overlapping(area,r) for r in protected):continue
        if any(overlapping(area,r) for r in other_regions):continue
        # A neighbouring translated paragraph can flow into this source box.
        # Only replace a region that still contains exactly its original glyphs.
        # Growing the destination into whitespace must never broaden ownership
        # of source glyphs (e.g. a neighbouring numerator or table label).
        from .quality import drawn_glyphs
        foreign=False
        for glyph in drawn_glyphs(source_page):
            b=fitz.Rect(glyph['bbox']);center=fitz.Point((b.x0+b.x1)/2,(b.y0+b.y1)/2)
            if area.contains(center) and not region.contains(center):foreign=True;break
        if foreign:continue
        masks=replacement_masks(source_page,page,region)
        if masks is None:continue
        from .pdf_transaction import copy_artwork,commit_artwork
        trial=fitz.open();copy_artwork(document,page_index,trial);p=trial[0];retained=False
        try:
            formula_covers=[object_box(objects[int(i)])+(-.75,-.75,.75,.75) for i in MARKER.findall(text)
                            if not is_literal(objects[int(i)])] if remove_formula_paths else []
            from .raster_text import cover_is_clear
            if formula_covers and not cover_is_clear(source_page,formula_covers,formula_path_rects):continue
            if any(any(box.contains(fitz.Point((g['bbox'][0]+g['bbox'][2])/2,(g['bbox'][1]+g['bbox'][3])/2))
                and not region.contains(fitz.Point((g['bbox'][0]+g['bbox'][2])/2,(g['bbox'][1]+g['bbox'][3])/2))
                for g in drawn_glyphs(source_page)) for box in formula_covers):continue
            if formula_path_rects is not None and any(not any((b+(-.01,-.01,.01,.01)).contains(fitz.Rect(r))
                for b in formula_covers) for r in formula_path_rects):continue
            for mask in masks:p.add_redact_annot(mask,fill=None)
            p.apply_redactions(images=0,graphics=0,text=0)
            # Reused PDF Form XObjects can paint the same operator in several
            # locations. Graphics redaction can erase an unrelated occurrence.
            # Cover only certified, glyph-isolated math rectangles; copy the
            # original formula artwork to its new position without modifying
            # shared drawing streams. The native binder rejects figure fills.
            for box in formula_covers:p.draw_rect(box,color=None,fill=(1,1,1),overlay=True)
            if any(not is_literal(o) for o in objects):
                snippet=None
                for scale in (1,.95,.9,.85):
                    for leading in (1.2,1.35,1.5):
                        snippet=compose_inline(source_page,area,text,objects,size,scale,leading=leading)
                        if snippet is not None:break
                    if snippet is not None:break
                if snippet is None:continue
                try:p.show_pdf_page(area,snippet,0)
                finally:snippet.close()
            else:
                plain=literal_text(text,objects)
                archive,css=chinese_style(size,size*1.15)
                if region.height<size*1.8:css+='body{padding:0;}'
                spare,scale=p.insert_htmlbox(area,'<div>'+prose_html(plain)+'</div>',
                    css=css,archive=archive,scale_low=.85)
                if spare<0 or scale<.85:continue
                actual=prose_counter(p.get_text(clip=area))
                if prose_counter(plain)-actual:continue
            if loses_unrestored_glyphs(page,p,masks,[]) or render_errors(p)-render_errors(page):continue
            if staged is not None:
                staged.append({'document':trial,'area':area,'masks':masks,
                    'formulaCovers':formula_covers})
                retained=True
            else:commit_artwork(document,page_index,trial)
            return list(area)
        finally:
            if not retained:trial.close()
    return None

def enhance_reading(source_path,pdf_path,report_path,translator,*,pages=None,progress=None):
    from .quality import selected_pages
    from .direct_pdf import native_paragraphs
    report=json.loads(report_path.read_text(encoding='utf-8'))
    if report.get('sourceHash')!=sha256_file(source_path) or report.get('outputHash')!=sha256_file(pdf_path):
        raise ValueError('Reading layer source/result identity mismatch')
    selected=selected_pages(pages,report['pages']);units=[];repairs=[];page_previews={}
    previous={u['id']:u for u in report.get('reading',{}).get('units',[]) if u.get('id')}
    with fitz.open(source_path) as source,fitz.open(pdf_path) as output:
        refs=reference_regions(source)
        for number,page in enumerate(source):
            if number not in selected:continue
            raw=reading_blocks(page)
            protected=[fitz.Rect(p['region']) for p in report.get('preserved',[]) if p['page']==number+1 and p.get('region')
                       and p.get('reason') in ('figure','table','isolate_formula','nomenclature','abandon','original_image')]
            protected+=refs[number]
            issues=restore_english_regions(report,number+1)
            # Other blocks can span columns and a full-width footer, enclosing
            # empty space. Only their actual lines may obstruct local reflow.
            all_regions=[[fitz.Rect(l['bbox']) for l in b['lines']] for b in raw]
            omitted=[fitz.Rect(l['bbox']) for b in native_paragraphs(page) for l in b['lines'] if not any(l in a['lines'] for a in raw)]
            for index,block in enumerate(raw):
                region=fitz.Rect(block['bbox']);input_text,objects=make_input(block);weight=natural_weight(input_text)
                if weight<4:continue
                if protected_block(block,protected):continue
                # Exclude source Chinese paragraphs from the English coverage denominator.
                if cjk(input_text)>len(input_text)*.15:continue
                identity=hashlib.sha256((str(number)+str(tuple(region))+input_text).encode()).hexdigest()[:20]
                unit={'id':identity,'page':number+1,'region':list(region),'words':weight,'source':input_text,'status':'missing'}
                old_english=any(r is None or overlapping(region,r) for r in issues)
                old=previous.get(identity,{})
                view_region=old.get('placedRegion',region) if old.get('source')==input_text else region
                chinese=output[number].get_text(clip=view_region)
                # Historical issues describe the old engine candidate. Current
                # visible Chinese is the evidence, including prior local repairs.
                if pdf_body_present(input_text,chinese):
                    unit['status']='pdf'
                    if old.get('placedRegion'):unit['placedRegion']=old['placedRegion']
                    units.append(unit);continue
                if progress:progress(f'补全第 {number+1} 页正文',95)
                # Single coherent paragraph; only failed input retries once.
                validator=lambda text:good_translation(input_text,text,objects)
                old=previous.get(identity,{})
                translated=old.get('translation','') if old.get('source')==input_text else ''
                if not validator(translated):translated=translator.translate_checked(input_text,validator)
                if not good_translation(input_text,translated,objects):
                    translated=translator.translate_checked(input_text,validator,retry=True)
                if good_translation(input_text,translated,objects):
                    unit['translation']=translated
                    sizes=[s['size'] for line in block['lines'] for s in line['spans'] if s.get('chars')]
                    fixed=safe_typeset(output,number,region,translated,statistics.median(sizes or [10]),protected,
                                       [r for j,regions in enumerate(all_regions) if j!=index for r in regions]+omitted,page,objects)
                    if fixed:
                        unit['status']='pdf';unit['repaired']=True;unit['placedRegion']=fixed;repairs.append({'page':number+1,'region':fixed})
                    else:
                        unit['message']='译文已存档，但尚未安全排入 PDF；再次点击翻译会复查。'
                        unit['reason']='layout_pending'
                else:unit['message']='未获得完整且符号一致的译文，可再次补全。'
                units.append(unit)
        if repairs:
            output.subset_fonts()
            temporary=pdf_path.with_name('reading-result.pdf');output.save(temporary,garbage=3,deflate=True)
        chinese_pages=sum(cjk(p.get_text())>=5 for p in output)
    if repairs:temporary.replace(pdf_path)
    if sha256_file(source_path)!=report['sourceHash']:raise ValueError('Source changed during reading enhancement')
    totals={status:sum(u['words'] for u in units if u['status']==status) for status in ('pdf','supplement','missing')}
    report.update(schema=2,outputHash=sha256_file(pdf_path),pagesWithChinese=chinese_pages,
                  reading={'schema':READING_SCHEMA,'units':units,'repairs':repairs,'pagePreviews':page_previews},
                  coverage={'unit':'source English words in eligible text blocks','selectedPages':sorted(n+1 for n in selected),
                            'total':sum(totals.values()),**totals})
    available=totals['pdf']
    report['status']='completed_with_warnings' if available and (totals['missing'] or report['issues'] or totals['supplement']) else 'completed' if available else 'failed'
    report['message']=f"正文已排入 PDF {totals['pdf']} / 未完成 {totals['missing']}（按原文词数）"
    atomic_json(report_path,report)
    return report

def audit_archive(source_path,pdf_path,report,*,pages=None):
    """Read-only per-click audit. Never requests a model or edits the archive."""
    from .quality import selected_pages
    from .direct_pdf import native_paragraphs
    if report.get('reading',{}).get('schema')!=READING_SCHEMA:
        return {'needsWork':True,'reason':'processing_rules_changed'}
    units={u['id']:u for u in report['reading'].get('units',[]) if u.get('id')}
    totals={'pdf':0,'supplement':0,'missing':0};seen=set()
    with fitz.open(source_path) as source,fitz.open(pdf_path) as output:
        if len(source)!=len(output):return {'needsWork':True,'reason':'page_count_changed'}
        selected=selected_pages(pages,len(source));refs=reference_regions(source)
        for n,page in enumerate(source):
            if n not in selected:continue
            protected=[fitz.Rect(p['region']) for p in report.get('preserved',[]) if p['page']==n+1 and p.get('region')
                       and p.get('reason') in ('figure','table','isolate_formula','nomenclature','abandon','original_image')]+refs[n]
            for block in reading_blocks(page):
                region=fitz.Rect(block['bbox']);text,objects=make_input(block);weight=natural_weight(text)
                if weight<4 or cjk(text)>len(text)*.15 or protected_block(block,protected):continue
                identity=hashlib.sha256((str(n)+str(tuple(region))+text).encode()).hexdigest()[:20]
                seen.add(identity);unit=units.get(identity,{});status='missing'
                if unit.get('source')==text and unit.get('words')==weight:
                    if unit.get('status')=='pdf' and pdf_body_present(text,output[n].get_text(clip=unit.get('placedRegion',region))):status='pdf'

                totals[status]+=weight
    coverage={'total':sum(totals.values()),**totals}
    recorded=report.get('coverage',{})
    consistent=seen==set(units) and all(recorded.get(k)==v for k,v in coverage.items())
    return {'needsWork':not consistent or bool(totals['missing']),'reason':'missing_content' if totals['missing'] else 'local_audit',
            'coverage':coverage}
