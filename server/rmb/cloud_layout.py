"""Coordinate-aware document inventory combining native glyphs and cloud layout."""
from __future__ import annotations
import hashlib
import re
import statistics
import pymupdf as fitz
from .reading import make_input, union, natural_weight, tokens_for_line
from .direct_pdf import native_paragraphs

LAYOUT_SCHEMA=6
FORMULA_LABELS={'formula','display_formula','inline_formula','isolate_formula','formula_number'}
IMAGE_LABELS={'image','figure','chart'}
TABLE_LABELS={'table','table_body'}

def area(box): return max(0,fitz.Rect(box).get_area())
def intersection(a,b): return area(fitz.Rect(a)&fitz.Rect(b))

def map_box(box, raw, page):
    width=raw.get('width');height=raw.get('height')
    if not isinstance(width,(int,float)) or not isinstance(height,(int,float)) or min(width,height)<=0:
        raise ValueError('飞桨未返回页面尺寸，无法安全对齐坐标')
    if not isinstance(box,(tuple,list)) or len(box)!=4:raise ValueError('飞桨区域坐标无效')
    result=fitz.Rect(box)
    result=fitz.Rect(result.x0*page.rect.width/width,result.y0*page.rect.height/height,
                     result.x1*page.rect.width/width,result.y1*page.rect.height/height)
    if result.is_empty or not (page.rect+(-1,-1,1,1)).contains(result):raise ValueError('飞桨区域坐标越界')
    return list(result & page.rect)

def cloud_regions(raw,page):
    rows=raw.get('parsing_res_list')
    if not isinstance(rows,list):raise ValueError('飞桨未返回布局区域列表')
    regions=[]
    for i,row in enumerate(rows):
        box=map_box(row.get('block_bbox'),raw,page)
        regions.append({'id':i,'label':row.get('block_label','text'),'region':box,'text':row.get('block_content','')})
    return regions

def classification(box,regions):
    matches=[(intersection(box,r['region'])/max(.1,area(box)),r) for r in regions]
    return max(matches,key=lambda v:v[0])[1] if matches and max(x[0] for x in matches)>.4 else {'id':-1,'label':'text'}

def unit(page,block,label,*,kind='native'):
    text,objects=make_input(block)
    size=statistics.median([s['size'] for l in block.get('lines',[]) for s in l['spans']] or [10])
    identity=hashlib.sha256((str(page.number)+repr(tuple(round(x,3) for x in block['bbox']))+text).encode()).hexdigest()[:24]
    return {'id':identity,'page':page.number+1,'region':list(block['bbox']),'source':text,
            'objects':objects,'fontSize':size,'label':label,'kind':kind,'words':natural_weight(text),'status':'missing'}

def inventory_native(page,regions):
    """Split at semantic boundaries, without excluding short labels or italics."""
    result=[]
    for block in native_paragraphs(page):
        groups=[];group=[];previous=None
        for line in block['lines']:
            category=classification(line['bbox'],regions)
            key=(category['id'],category['label'])
            # Table cells / diagram labels should not be concatenated across columns.
            if category['label'] in TABLE_LABELS|IMAGE_LABELS:key=(*key,round(line['bbox'][0],0))
            if group and key!=previous:groups.append((group,previous[1]));group=[]
            group.append(line);previous=key
        if group:groups.append((group,previous[1]))
        for lines,label in groups:
            child={**block,'lines':lines,'bbox':list(union([l['bbox'] for l in lines]))}
            u=unit(page,child,label)
            if label in FORMULA_LABELS and natural_weight(u['source'])<=2:u['status']='preserved';u['reason']='formula'
            elif not re.search('[A-Za-z]',u['source']) or not natural_weight(u['source']):u['status']='preserved';u['reason']='identifiers_or_non_english'
            result.append(u)
    return result

def ocr_lines(raw,region):
    """PP-OCRv6 polygons are coordinates of the explicitly rendered crop."""
    texts=raw.get('rec_texts',[]);polys=raw.get('rec_polys',raw.get('dt_polys',[]));scores=raw.get('rec_scores',[])
    w,h=raw.get('width'),raw.get('height')
    if not w or not h or len(texts)!=len(polys):raise ValueError('图片文字缺少对应坐标')
    crop=fitz.Rect(region);lines=[]
    for i,(text,poly) in enumerate(zip(texts,polys)):
        if not isinstance(poly,list) or len(poly)<4:continue
        x=[float(p[0]) for p in poly];y=[float(p[1]) for p in poly]
        b=fitz.Rect(crop.x0+min(x)/w*crop.width,crop.y0+min(y)/h*crop.height,
                    crop.x0+max(x)/w*crop.width,crop.y0+max(y)/h*crop.height)
        # Rotated lines need their transform preserved, not flattened to an axis box.
        rotated=abs(poly[1][1]-poly[0][1])>max(2,(max(y)-min(y))*.15)
        lines.append({'text':text,'region':list(b),'score':scores[i] if i<len(scores) else None,'rotated':rotated})
    return lines

def raster_units(page,region,raw,label):
    result=[]
    for i,line in enumerate(ocr_lines(raw,region)):
        text=line['text'];objects=[]
        # Preserve numbers/identifiers as literal tokens without guessing formulas.
        def protect(match):
            objects.append({'text':match[0],'kind':'literal','box':line['region']})
            return '{v'+str(len(objects)-1)+'}'
        text=re.sub(r'\([a-zA-Z]\)|\b\d+(?:[.,]\d+)*(?:e[+-]?\d+)?(?:%|[A-Za-z]+)?\b|https?://\S+|\S+@\S+|\b[A-Za-z]*[A-Z][A-Za-z]*[A-Z][A-Za-z]*\b',protect,text)
        identity=hashlib.sha256((str(page.number)+str(line['region'])+text).encode()).hexdigest()[:24]
        value={'id':identity,'page':page.number+1,'region':line['region'],'source':text,'objects':objects,
               'fontSize':max(5,fitz.Rect(line['region']).height*.72),'label':label,'kind':'raster',
               'words':natural_weight(text),'status':'missing'}
        if not natural_weight(text) or re.fullmatch(r'[A-Za-z](?:\d+)?',line['text'].strip()):value.update(status='preserved',reason='identifiers_or_non_english')
        elif line['rotated'] or (line['score'] is not None and line['score']<.8):value['reason']='ocr_needs_review'
        elif re.search(r'[=<>∑∫√α-ωΑ-Ω_^]|\b[b-zB-HJ-Z]\b|[a-z][A-Z]',re.sub(r'\{v\d+\}','',text)):
            # Line polygons cannot establish positions of inline mathematical glyphs.
            # Never turn an OCR guess into a freshly typeset formula.
            value['reason']='ocr_math_alignment'
        result.append(value)
    return result

def table_cells(raw,region):
    """Convert StructureV3 cell polygons to page coordinates for text splitting."""
    cells=[];crop=fitz.Rect(region);w,h=raw.get('width'),raw.get('height')
    if not w or not h:return cells
    for table in raw.get('table_res_list',[]):
        for value in table.get('cell_box_list',[]):
            if len(value)==8:
                xs=value[::2];ys=value[1::2];value=[min(xs),min(ys),max(xs),max(ys)]
            if len(value)!=4:continue
            b=fitz.Rect(value)
            box=fitz.Rect(crop.x0+b.x0/w*crop.width,crop.y0+b.y0/h*crop.height,
                          crop.x0+b.x1/w*crop.width,crop.y0+b.y1/h*crop.height)
            if crop.contains(box) and not box.is_empty:cells.append(list(box))
    return cells

def protect_metadata(u):
    """Preserve bibliographic identity while translating quoted reference titles."""
    if u['label'] not in ('reference','references','reference_content'):return u
    text=u['source'];match=re.search('[“"]([^”"]{5,})[”"]',text)
    if not match:
        u.update(status='preserved',reason='bibliographic_identity');return u
    before,after=text[:match.start(1)],text[match.end(1):]
    prefix=len(u['objects']);u['objects'] += [dict(kind='literal',text=before,box=u['region']),dict(kind='literal',text=after,box=u['region'])]
    u['source']='{v'+str(prefix)+'}'+match[1]+'{v'+str(prefix+1)+'}'
    return u
