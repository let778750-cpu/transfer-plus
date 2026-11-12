"""Reconstruct semantic paragraphs from PDF glyphs, including detached math.

PDF content-stream blocks are drawing order, not paragraph ownership. A formula
can be emitted after every paragraph on a page. Assign each glyph once to a
layout region and rebuild physical lines before translation.
"""
import re
import statistics
import copy
from collections import Counter
import pymupdf as fitz
from .reading import union, make_input, natural_weight, MARKER, is_literal


def bind_formula_artwork(page,u):
    candidate=copy.deepcopy(u)
    if not _bind_formula_artwork(page,candidate):return False
    u.update(candidate)
    return True


def _bind_formula_artwork(page,u):
    """Attach path-painted brackets/operators to their native formula clips.

    Some publishers paint parentheses and '=' as paths, absent from rawdict.
    Every path removed with prose must belong to a copied math object first.
    """
    region=fitz.Rect(u['region']);objects=u['objects'];size=u['fontSize']
    allchars=[c for b in page.get_text('rawdict')['blocks'] for l in b.get('lines',[]) for s in l['spans'] for c in s['chars'] if c['c'].strip()]
    # A superscript / denominator can occupy a separate PDF line. Merge its
    # object back into an overlapping formula only when the combined rectangle
    # contains exactly those objects' glyphs and no natural-language characters.
    changed=True
    while changed:
        changed=False;used=list(map(int,MARKER.findall(u['source'])))
        for a in used:
            if changed:break
            for b in used:
                if a>=b or (is_literal(objects[a]) and is_literal(objects[b])):continue
                x,y=fitz.Rect(objects[a]['box']),fitz.Rect(objects[b]['box'])
                adjacent=re.search(r'\{v'+str(a)+r'\}\s*\{v'+str(b)+r'\}',u['source'])
                # Tall objects on consecutive prose lines are distinct uses,
                # even when their font boxes touch. Only detached small scripts
                # may merge across intervening prose.
                if not adjacent and min(x.height,y.height)>size*.8:continue
                if not (x+(-.2,-.2,.2,.2)).intersects(y):continue
                merged=x|y
                expected=Counter(c for c in objects[a]['text']+objects[b]['text'] if not c.isspace())
                actual=Counter(c['c'] for c in allchars if merged.contains(fitz.Point((c['bbox'][0]+c['bbox'][2])/2,(c['bbox'][1]+c['bbox'][3])/2)))
                if expected!=actual:continue
                objects[a]={'box':list(merged),'text':objects[a]['text']+' '+objects[b]['text'],'kind':'math'}
                u['source']=u['source'].replace('{v'+str(b)+'}','');changed=True;break
    drawings=[];path_masks=[];path_rects=[]
    for drawing in page.get_drawings():
        stroke=float(drawing.get('width',0) or 0) if 's' in drawing.get('type','') else 0
        pad=max(.15,stroke/2+.05)
        rect=fitz.Rect(drawing['rect'])+(-pad,-pad,pad,pad)
        if u['label']=='footnote' and rect.width>size*3 and rect.height<size*.2 and not region.contains(rect):continue
        if rect.width>region.width*.8 and rect.height<size*.5 and not region.contains(rect):continue
        used=list(map(int,MARKER.findall(u['source'])))
        near=any((fitz.Rect(o['box'])+(-size*2,-size*.6,size*2,size*.6)).intersects(rect)
                 for i,o in enumerate(objects) if i in used and not is_literal(o))
        if not region.contains(rect) and not near:continue
        if not region.contains(rect):
            expanded=region|rect
            if not page.rect.contains(expanded):return False
            foreign=False
            for c in allchars:
                pt=fitz.Point((c['bbox'][0]+c['bbox'][2])/2,(c['bbox'][1]+c['bbox'][3])/2)
                if expanded.contains(pt) and not region.contains(pt):foreign=True;break
            if foreign:
                # An adjacent paragraph's path belongs to that paragraph. A
                # crossing stroke that actually touches this formula is unsafe.
                if any((fitz.Rect(objects[i]['box'])+(-.5,-.5,.5,.5)).intersects(rect) for i in used if not is_literal(objects[i])):return False
                continue
            region=expanded;u['region']=list(region)
        drawings.append(rect)
        # Remove selected paths by intersection, then check the complete path
        # inventory in the scratch PDF. Covered-path removal has a much larger
        # stroke/miter envelope and can leave operators at their old positions.
        margin=.05
        path_masks.append(list(fitz.Rect(drawing['rect'])+(-margin,-margin,margin,margin)))
        path_rects.append(list(drawing['rect']))
    for rect in drawings:
        choices=[]
        for i,o in enumerate(objects):
            if i not in used:continue
            box=fitz.Rect(o['box'])
            dy=max(0,rect.y0-box.y1,box.y0-rect.y1)
            dx=max(0,rect.x0-box.x1,box.x0-rect.x1)
            if dy<size*.5 and dx<size*2 and rect.height<size*4 and rect.width<size*12:
                choices.append((dy*4+dx,i))
        if not choices:return False
        i=min(choices)[1];objects[i]['box']=list(fitz.Rect(objects[i]['box'])|rect);objects[i]['kind']='math'
    # Adjacent objects can now share a path or a bracket. Merge only consecutive
    # placeholders separated by whitespace; preserve the actual source rectangle.
    changed=True
    while changed:
        changed=False
        used=list(map(int,MARKER.findall(u['source'])))
        for a,b in [(a,b) for a in used for b in used if a<b]:
            x,y=fitz.Rect(objects[a]['box']),fitz.Rect(objects[b]['box'])
            adjacent=re.search(r'\{v'+str(a)+r'\}\s*\{v'+str(b)+r'\}',u['source'])
            if not adjacent and min(x.height,y.height)>size*.8:continue
            merged=x|y
            expected=Counter(c for c in objects[a]['text']+objects[b]['text'] if not c.isspace())
            actual=Counter(c['c'] for c in allchars if merged.contains(fitz.Point((c['bbox'][0]+c['bbox'][2])/2,(c['bbox'][1]+c['bbox'][3])/2)))
            if expected==actual and (x+(-.5,-.5,.5,.5)).intersects(y) and not (is_literal(objects[a]) and is_literal(objects[b])):
                objects[a]={'box':list(x|y),'text':objects[a]['text']+' '+objects[b]['text'],'kind':'math'}
                u['source']=u['source'].replace('{v'+str(b)+'}','');changed=True;break
    # A clip must not sweep up neighbouring prose. Numeric literals may be
    # absorbed only when they are explicitly part of the protected object.
    for index in map(int,MARKER.findall(u['source'])):
        o=objects[index]
        if is_literal(o):continue
        box=fitz.Rect(o['box'])
        if not (region+(-.3,-.3,.3,.3)).contains(box):return False
        actual=Counter(c['c'] for c in allchars if box.contains(fitz.Point(
            (c['bbox'][0]+c['bbox'][2])/2,(c['bbox'][1]+c['bbox'][3])/2)))
        if actual!=Counter(c for c in o['text'] if not c.isspace()):return False
    u['removeFormulaPaths']=bool(drawings)
    u['formulaPathMasks']=path_masks
    u['formulaPathRects']=path_rects
    return True


def physical_lines(spans):
    chars=[{**c,'font':s.get('font',''),'size':s.get('size',10),'flags':s.get('flags',0)}
           for s in spans for c in s.get('chars',[]) if c['c'].strip()]
    if not chars:return []
    size=statistics.median(c['size'] for c in chars)
    rows=[]
    # Baselines are established by full-size glyphs. Sub/superscripts attach to
    # the closest baseline, rather than becoming independent prose paragraphs.
    for c in sorted(chars,key=lambda c:(-c['size'],c['origin'][1],c['origin'][0])):
        tolerance=size*(.56 if c['size']<size*.82 else .22)
        matches=[r for r in rows if abs(r['base']-c['origin'][1])<=tolerance]
        if matches:
            row=min(matches,key=lambda r:abs(r['base']-c['origin'][1]));row['chars'].append(c)
        else:rows.append({'base':c['origin'][1],'chars':[c]})
    result=[]
    for row in sorted(rows,key=lambda r:r['base']):
        ordered=sorted(row['chars'],key=lambda c:(c['bbox'][0],c['origin'][1]));out=[];previous=None
        for c in ordered:
            if previous:
                gap=c['bbox'][0]-previous['bbox'][2]
                # Recreate real spaces, ignoring synthetic PDF spaces that sit
                # on top of the following character (obs + ervation, for example).
                if gap>min(c['size'],previous['size'])*.14:
                    out.append({'font':'','size':size,'chars':[{'c':' ','bbox':previous['bbox'],'origin':previous['origin']}]})
            out.append({'font':c['font'],'size':c['size'],'flags':c['flags'],'chars':[c]});previous=c
        result.append({'bbox':list(union([c['bbox'] for c in ordered])),'spans':out})
    return result


def semantic_native(page,regions,fallback):
    """Rebuild body regions, retaining the cell/figure-specific native path.

    A glyph has exactly one owner; unclassified glyphs remain in the fallback
    inventory. Cloud text is only a layout hint and never replaces PDF symbols.
    """
    from .cloud_layout import unit,FORMULA_LABELS,TABLE_LABELS,IMAGE_LABELS
    eligible=[r for r in regions if r['label'] not in TABLE_LABELS|IMAGE_LABELS|
              {'reference','references','reference_content'}]
    buckets={r['id']:[] for r in eligible};leftover=[]
    raw=page.get_text('rawdict')
    for b in raw['blocks']:
        for line in b.get('lines',[]):
            for span in line['spans']:
                owned={}
                for c in span['chars']:
                    box=fitz.Rect(c['bbox']);center=fitz.Point((box.x0+box.x1)/2,(box.y0+box.y1)/2)
                    options=[]
                    for r in eligible:
                        rb=fitz.Rect(r['region']);overlap=(rb&box).get_area()/max(.01,box.get_area())
                        if overlap>.3 or (rb+(-1,-4,1,2)).contains(center):options.append((overlap,-rb.get_area(),r))
                    owner=max(options,key=lambda x:x[:2])[2]['id'] if options else None
                    owned.setdefault(owner,[]).append(c)
                for owner,cs in owned.items():
                    target=leftover if owner is None else buckets[owner]
                    target.append({**span,'chars':cs})
    result=[]
    for r in eligible:
        lines=physical_lines(buckets[r['id']])
        if not lines:continue
        if r['label']=='algorithm':
            # Keep pseudocode's math and indentation at their original positions.
            # Translate only the natural-language runs between those anchors;
            # a detached superscript must not migrate with the previous line.
            for line in lines:
                child=unit(page,{'lines':[line],'bbox':line['bbox']},'algorithm')
                boxes=[fitz.Rect(o['box']) for o in child['objects']]
                for i,o in enumerate(child['objects']):
                    result.append({**child,'id':child['id']+'-math-'+str(i),'region':o['box'],
                        'source':o['text'],'objects':[],'words':0,'status':'preserved','reason':'formula','immutable':True})
                groups=[];group=[]
                for span in line['spans']:
                    for c in span['chars']:
                        pt=fitz.Point((c['bbox'][0]+c['bbox'][2])/2,(c['bbox'][1]+c['bbox'][3])/2)
                        if any(b.contains(pt) for b in boxes):
                            if group:groups.append(group);group=[]
                        else:group.append({**span,'chars':[c]})
                if group:groups.append(group)
                for group in groups:
                    box=list(union([c['bbox'] for s in group for c in s['chars']]))
                    segment=unit(page,{'lines':[{'spans':group,'bbox':box}],'bbox':box},'algorithm')
                    if not segment['words']:segment.update(status='preserved',reason='identifiers_or_non_english')
                    result.append(segment)
            continue
        block={'lines':lines,'bbox':list(union([l['bbox'] for l in lines]))}
        u=unit(page,block,r['label']);u['semantic']=True
        if r['label'] not in FORMULA_LABELS:u['completeMath']=bind_formula_artwork(page,u)
        plain=MARKER.sub('',u['source']);words=re.findall(r'[A-Za-z]+',plain)
        author=(page.number==0 and block['bbox'][1]<page.rect.height*.35 and plain.count(',')>=2
                and words and sum(w[0].isupper() for w in words)/len(words)>.85)
        if author or r['label']=='header' or re.fullmatch(r'(?:[A-Z]\.?\s*)?[A-Z][a-z]+\s+et al\.',plain.strip()):
            u.update(status='preserved',reason='bibliographic_identity')
        elif re.fullmatch(r'(?:https?://|www\.)\S+',plain.strip()):
            u.update(status='preserved',reason='identifiers_or_non_english')
        elif r['label'] in FORMULA_LABELS:
            # Operator names and compact variable names inside a display
            # equation are mathematical notation, not untranslated prose.
            if all(w.lower() in {'exp','log','ln','sin','cos','tan','max','min','arg','softmax'} or len(w)<=2 or re.search('[a-z][A-Z]',w) for w in words):
                u.update(status='preserved',reason='formula')
            else:u['reason']='formula_contains_prose'
        elif not natural_weight(u['source']):u.update(status='preserved',reason='identifiers_or_non_english')
        result.append(u)
    # Preserve unassigned native blocks/cells. Clip their ownership to actual
    # leftover glyphs; never reintroduce glyphs already assigned above.
    remaining={(c['c'],tuple(c['origin'])) for s in leftover for c in s['chars']}
    for u in fallback:
        region=fitz.Rect(u['region']);lines=[]
        for b in raw['blocks']:
            for line in b.get('lines',[]):
                spans=[]
                for s in line['spans']:
                    cs=[c for c in s['chars'] if (c['c'],tuple(c['origin'])) in remaining and
                        region.contains(fitz.Point((c['bbox'][0]+c['bbox'][2])/2,(c['bbox'][1]+c['bbox'][3])/2))]
                    if cs:spans.append({**s,'chars':cs})
                if spans:lines.append({'spans':spans,'bbox':list(union([c['bbox'] for s in spans for c in s['chars']]))})
        if not lines:continue
        child=unit(page,{'lines':lines,'bbox':list(union([l['bbox'] for l in lines]))},u['label'])
        if not child['words']:child.update(status='preserved',reason='identifiers_or_non_english')
        result.append(child)
        remaining.difference_update((c['c'],tuple(c['origin'])) for l in lines for s in l['spans'] for c in s['chars'])
    return result
