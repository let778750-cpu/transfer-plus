"""Conservative postflight. Failed regions are restored from source PDF objects."""
from __future__ import annotations
from collections import Counter
from difflib import SequenceMatcher
import json
import math
from pathlib import Path
import re
import pymupdf as fitz
from .config import atomic_json, sha256_file
from .structure import reference_regions

def formula_chars(paragraph):
    return [c for part in paragraph.get("pdf_paragraph_composition", [])
            for c in (part.get("pdf_formula") or {}).get("pdf_character", [])]

def glyph_signature(chars):
    return Counter((c.get("char_unicode"), c.get("pdf_character_id"),
                    (c.get("pdf_style") or {}).get("font_id")) for c in chars)

def painted_box(paragraph):
    boxes=[]
    for part in paragraph.get('pdf_paragraph_composition',[]):
        chars=([part['pdf_character']] if part.get('pdf_character') else
               (part.get('pdf_formula') or {}).get('pdf_character',[]))
        boxes += [c['box'] for c in chars if c.get('box') and str(c.get('char_unicode') or '').strip()]
    if not boxes:return paragraph.get('box')
    return {'x':min(b['x'] for b in boxes),'y':min(b['y'] for b in boxes),
            'x2':max(b['x2'] for b in boxes),'y2':max(b['y2'] for b in boxes)}

def formula_geometry_ok(before, after):
    if glyph_signature(before) != glyph_signature(after):
        return False
    # Pair each protected character by original font/glyph and render order.
    # The matched glyph sequence already establishes correspondence. Typesetting
    # rewrites render_order per paragraph, so independently sorting would pair
    # different characters (notably inserted whitespace and subscripts).
    a, b = list(before), list(after)
    if not a:
        return True
    # Positions across different inline formulas may move independently. The
    # caller checks each formula separately. Uniform scaling is permitted.
    sa = float((a[0].get("pdf_style") or {}).get("font_size") or 1)
    sb = float((b[0].get("pdf_style") or {}).get("font_size") or 1)
    if sb / sa < .849:
        return False
    aa, bb = a[0].get("box"), b[0].get("box")
    if not aa or not bb:
        return False
    for x, y in zip(a, b):
        if not x.get("box") or not y.get("box"):
            return False
        for axis in ("x", "y"):
            if abs((x["box"][axis] - aa[axis]) / sa - (y["box"][axis] - bb[axis]) / sb) > .12:
                return False
        xa = float((x.get("pdf_style") or {}).get("font_size") or 0) / sa
        ya = float((y.get("pdf_style") or {}).get("font_size") or 0) / sb
        if abs(xa-ya) > .02:
            return False
        for axis, end in (("x", "x2"), ("y", "y2")):
            aw = (x["box"][end] - x["box"][axis]) / sa
            bw = (y["box"][end] - y["box"][axis]) / sb
            if abs(aw-bw) > .12:
                return False
    return True

def formulas(paragraph):
    return [p["pdf_formula"] for p in paragraph.get("pdf_paragraph_composition", []) if p.get("pdf_formula")]

def recognition_issues(paragraph, fonts):
    protected = formulas(paragraph)
    if not protected:
        return []
    # A formula that already spans source lines cannot safely be moved as a
    # rectangular group. Partial protection of italic bases is likewise unsafe:
    # e.g. only the 'cur' superscript protected while O(...) remains ordinary text.
    for formula in protected:
        chars = formula.get('pdf_character', [])
        sizes = [float((c.get('pdf_style') or {}).get('font_size') or 0) for c in chars]
        box = formula.get('box')
        if box and sizes and box['y2']-box['y'] > max(sizes)*1.9:
            return ['multiline_math_recognition_uncertain']
    for part in paragraph.get('pdf_paragraph_composition', []):
        span = part.get('pdf_same_style_characters')
        if not span: continue
        chars = span.get('pdf_character', [])
        italic = [c for c in chars if fonts.get((c.get('pdf_style') or {}).get('font_id'),{}).get('italic')]
        value = ''.join(c.get('char_unicode') or '' for c in italic).strip()
        if value and (len(value) <= 4 or any(c in value for c in '|()=+')):
            return ['partial_math_recognition_uncertain']
    return []

def split_word_paragraphs(paragraphs):
    """Detect words broken across independently translated source fragments."""
    affected=set()
    for left in paragraphs:
        box=left.get('box');text=str(left.get('unicode') or '').rstrip()
        if not box or not left.get('debug_id') or not re.search(r'[A-Za-z]$',text):continue
        size=float((left.get('pdf_style') or {}).get('font_size') or 10)
        for right in paragraphs:
            other=right.get('box');value=str(right.get('unicode') or '').lstrip()
            if not other or not right.get('debug_id') or not re.match(r'^[A-Za-z]',value):continue
            gap=other['x']-box['x2']
            if (0<=gap<size*.2 and abs(other['y']-box['y'])<size*.25
                and abs(other['y2']-box['y2'])<size*.25):
                affected.update((left['debug_id'],right['debug_id']))
    return affected

def paragraph_issues(before, after, min_scale=.85):
    if after is None:
        return ["paragraph_missing"]
    issues = []
    if float(after.get("scale") or 1) < min_scale - .001:
        issues.append("font_below_minimum")
    # 0.7.1 typesetting flattens PdfFormula into PdfCharacter compositions.
    # Validate original formula runs against those final glyphs, not IR node
    # counts (which necessarily change after successful typesetting).
    final = []
    for part in after.get("pdf_paragraph_composition", []):
        if part.get("pdf_character"):
            final.append(part["pdf_character"])
        elif part.get("pdf_formula"):
            final.extend(part["pdf_formula"].get("pdf_character", []))
    if str(before.get('unicode') or '').strip():
        if not str(after.get('unicode') or '').strip():
            issues.append('paragraph_text_missing')
        drawn = ''.join(c.get('char_unicode') or '' for c in final)
        if not drawn.strip():
            issues.append('paragraph_not_drawn')
        # The final IR may retain translated text even when its glyph list is
        # truncated. All Chinese glyphs in that text must reach the writer.
        expected = Counter(c for c in str(after.get('unicode') or '') if '\u4e00'<=c<='\u9fff')
        actual = Counter(c for c in drawn if '\u4e00'<=c<='\u9fff')
        if expected-actual:
            issues.append('translated_glyph_missing')
    key = lambda c: (c.get("char_unicode"), c.get("pdf_character_id"), (c.get("pdf_style") or {}).get("font_id"))
    used = set()
    for formula in formulas(before):
        original = formula.get("pdf_character", [])
        expected = [key(c) for c in original]
        matches = [i for i in range(len(final)-len(expected)+1)
                   if not any(j in used for j in range(i,i+len(expected)))
                   and [key(c) for c in final[i:i+len(expected)]] == expected]
        if not matches:
            issues.append("math_glyph_changed")
            break
        index = next((i for i in matches if formula_geometry_ok(original, final[i:i+len(expected)])), None)
        if index is None:
            issues.append("math_geometry_changed")
            break
        used.update(range(index,index+len(expected)))
    return issues

def rect(box, height):
    if not box:
        return None
    values = [box.get(k) for k in ("x", "y", "x2", "y2")]
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        return None
    return fitz.Rect(values[0], height - values[3], values[2], height - values[1])

def normalize(text):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\{v\d+\}|<[^>]+>", "", text).lower())


def reference_text(paragraphs):
    """Readable Chinese reference only; never present engine tokens as math."""
    lines=[]
    for paragraph in sorted(paragraphs,key=lambda p:p.get('render_order') or 0):
        text=str(paragraph.get('unicode') or '')
        text=re.sub(r'<\s*/?\s*style\b[^>]*>','',text)
        if not re.search(r'[\u4e00-\u9fff]',text):continue
        text=re.sub(r'\{\s*v\s*\d+\s*\}','〔公式或标记见原文〕',text)
        text=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',text)
        if text.strip():lines.append(text.strip())
    return '\n'.join(lines)


def nomenclature_regions(blocks,width,height):
    regions=[]
    for block in blocks:
        if not re.fullmatch(r'NOMENCLATURE|LIST OF SYMBOLS',str(block[4]).strip(),re.I):continue
        right=block[0]>=width/2
        endings=[b[1] for b in blocks if b[1]>block[3] and (b[0]>=width/2)==right
                 and re.match(r'^[IVX]+\.\s+[A-Z]',str(b[4]).strip())]
        boundary=min(endings) if endings else height
        column=[b for b in blocks if (b[0]>=width/2)==right and b[1]>=block[1]-1 and b[3]<boundary-1]
        # Use the inner content edge, not the geometric half-page: translated
        # left-column text may legitimately occupy part of the empty gutter.
        x0=min(b[0] for b in column)-1 if right and column else 0
        x1=width if right else max(b[2] for b in column)+1 if column else width/2
        regions.append(fitz.Rect(x0,block[1]-1,x1,boundary-2))
    return regions

def restoration_group(identity, before, after, height, page_rect):
    """Close over every source/final paragraph touched by a restoration mask.

    This handles nested source fragments such as DOI sub-runs without erasing
    adjacent translations. The entire closed region is copied from the source.
    """
    members = {identity}
    original = rect(before[identity].get('box'),height)
    target = rect((after.get(identity) or before[identity]).get('box'),height)
    if original is None or target is None:return None, members
    area = (original | target) + (-1,-1,1,1)
    changed = True
    while changed:
        changed = False
        for key, para in before.items():
            if key in members:continue
            old = rect(para.get('box'),height)
            new = rect((after.get(key) or para).get('box'),height)
            if old is None or new is None:continue
            # MuPDF removes an entire text glyph even for a sliver overlap.
            # Tiny punctuation/superscript fragments must join the restoration.
            if (area & old).get_area() > 1e-5 or (area & new).get_area() > 1e-5:
                members.add(key);area = area | (old+(-1,-1,1,1)) | (new+(-1,-1,1,1));changed=True
        if area.get_area() > page_rect.get_area()*.6:return None,members
    return area & page_rect, members

def form_colorspaces(document):
    result = {}
    for xref in range(1,document.xref_length()):
        if document.xref_get_key(xref,'Subtype')[1] != '/Form':continue
        filename = document.xref_get_key(xref,'PTEX.FileName')
        colors = document.xref_get_key(xref,'Resources/ColorSpace')
        if filename[0]=='string' and colors[0]=='dict' and '/ICCBased' in colors[1] and 'null' not in colors[1]:
            profiles = {}
            for match in re.finditer(r'/ICCBased\s+(\d+)\s+0\s+R',colors[1]):
                ref = int(match[1])
                if document.xref_is_stream(ref):
                    profiles[ref] = (document.xref_object(ref),document.xref_stream(ref))
            result.setdefault(filename[1],{})[colors[1]] = profiles
    return result

def disjoint_regions(regions):
    """Partition a union into nonoverlapping clips, so no glyph is painted twice."""
    result=[]
    for region in regions:
        pieces=[fitz.Rect(region)]
        for covered in result:
            remaining=[]
            for piece in pieces:
                overlap=piece & covered
                if overlap.is_empty:
                    remaining.append(piece);continue
                remaining.extend(fitz.Rect(*coords) for coords in (
                    (piece.x0,piece.y0,piece.x1,overlap.y0),
                    (piece.x0,overlap.y1,piece.x1,piece.y1),
                    (piece.x0,overlap.y0,overlap.x0,overlap.y1),
                    (overlap.x1,overlap.y0,piece.x1,overlap.y1))
                    if coords[2]-coords[0]>1e-5 and coords[3]-coords[1]>1e-5)
            pieces=remaining
        result.extend(pieces)
    return result

def drawn_glyphs(page):
    for block in page.get_text('rawdict')['blocks']:
        for line in block.get('lines',[]):
            for span in line.get('spans',[]):
                for char in span.get('chars',[]):
                    if not char.get('c','').strip():continue
                    yield char

def loses_unrestored_glyphs(candidate, result, masks, prose):
    """Verify real rendered text after redaction, with font metrics intact.

    Glyph boxes can extend beyond visible ink, so mere bbox intersection would
    falsely reject intact headers. Compare surviving characters and positions
    instead; include all glyphs in prose that was not intentionally restored.
    """
    actual={}
    for glyph in drawn_glyphs(result):actual.setdefault(glyph['c'],[]).append(glyph['origin'])
    for glyph in drawn_glyphs(candidate):
        box=fitz.Rect(glyph['bbox']);center=fitz.Point((box.x0+box.x1)/2,(box.y0+box.y1)/2)
        if any(mask.contains(center) for mask in masks) and not any(area.contains(center) for area in prose):continue
        positions=actual.get(glyph['c'],[]);x,y=glyph['origin']
        match=next((i for i,(a,b) in enumerate(positions) if abs(a-x)<.2 and abs(b-y)<.2),None)
        if match is None:return True
        positions.pop(match)
    return False

def restore_form_colorspaces(document, saved):
    # MuPDF 1.26 can clone a clipped Form while dropping its ICC stream reference
    # during text redaction. Restore only an unambiguous, exact original resource
    # dictionary associated with the same embedded-form filename.
    for xref in range(1,document.xref_length()):
        if document.xref_get_key(xref,'Subtype')[1] != '/Form':continue
        filename = document.xref_get_key(xref,'PTEX.FileName')[1]
        colors = document.xref_get_key(xref,'Resources/ColorSpace')
        matches = saved.get(filename,{})
        if colors[0]=='dict' and '/ICCBased' in colors[1] and len(matches)==1:
            value,profiles = next(iter(matches.items()))
            for old,(definition,data) in profiles.items():
                if not document.xref_is_stream(old) or document.xref_stream(old) != data:
                    replacement = document.get_new_xref()
                    document.update_object(replacement,definition)
                    document.update_stream(replacement,data)
                    value = re.sub(r'(?<=/ICCBased)\s+'+str(old)+r'\s+0\s+R',f' {replacement} 0 R',value)
            document.xref_set_key(xref,'Resources/ColorSpace',value)

def selected_pages(value, count):
    if value is None:
        return set(range(count))
    selected = set()
    for part in value.split(','):
        part = part.strip()
        if not part:
            raise ValueError("Invalid page range")
        if '-' in part:
            a, b = part.split('-')
            start, end = int(a or 1), int(b or count)
        else:
            start = end = int(part)
        if start < 1 or end < start or end > count:
            raise ValueError("Page range outside document")
        selected.update(range(start-1,end))
    return selected

def render_errors(page):
    """Exercise the actual PDF display list; ignore non-fatal font metrics notices."""
    fitz.TOOLS.mupdf_warnings(reset=True)
    try:
        page.get_pixmap(matrix=fitz.Matrix(.5,.5), alpha=False)
        warning = fitz.TOOLS.mupdf_warnings(reset=True).lower()
    except Exception:
        return {'render_exception'}
    return {name for name in ('invalid icc', 'object is not a stream', 'unknown cid',
                             'cannot load', 'cannot find', 'syntax error') if name in warning}

def verify_and_compose(source_path, candidate_path, ir_dir, output_path, report_path, config, records, state, *, final_ir_path=None):
    source, candidate = fitz.open(source_path), fitz.open(candidate_path)
    if len(source) != len(candidate):
        raise ValueError("Page count changed")
    # Missing audit data is a failure, never a successful unverified result.
    def read(name):
        return json.loads((Path(ir_dir) / name).read_text(encoding="utf-8"))["page"]
    before = {p["page_number"]: p for p in read("styles_and_formulas.json")}
    if final_ir_path is None:
        raise ValueError("Clean output audit IR is required")
    final_document = json.loads(Path(final_ir_path).read_text(encoding="utf-8"))
    # The layout box can remain the original allocation even when glyphs flow
    # outside it. Check the actual writer's glyph bounds, not that allocation.
    for page in final_document['page']:
        for paragraph in page.get('pdf_paragraph',[]):
            paragraph['box']=painted_box(paragraph)
    after = {p["page_number"]: p for p in final_document["page"]}
    selected = selected_pages(state.get("pages"),len(source))
    issues, preserved = [], []
    output = fitz.open()
    reference_pages = reference_regions(source)
    cjk_pages = 0
    protected_count = 0
    for number in range(len(source)):
        src, dst = source[number], candidate[number]
        if src.rect != dst.rect or src.rotation != dst.rotation:
            raise ValueError("Page geometry changed")
        h = src.rect.height
        bpage, apage = before.get(number), after.get(number)
        restore = []
        page_fail = []
        if len(src.get_text().strip()) < 30:
            page_fail.append("scan_or_damaged_text_layer")
        if number not in selected:
            output.insert_pdf(source, from_page=number, to_page=number)
            preserved.append({"page": number + 1, "reason": "not_selected"})
            continue
        if bpage is None or apage is None:
            output.insert_pdf(source, from_page=number, to_page=number)
            issues.append({"page":number+1,"region":None,"types":["audit_page_missing"],"fallback":"page","referenceTranslation":""})
            continue
        bparas = bpage.get("pdf_paragraph", [])
        fragmented = split_word_paragraphs(bparas)
        image_boxes=[fitz.Rect(info['bbox']) for info in src.get_image_info()]
        horizontal_rules=[]
        for drawing in src.get_drawings():
            for part in drawing['items']:
                if part[0]=='l' and abs(part[1].y-part[2].y)<.5 and abs(part[1].x-part[2].x)>40:
                    horizontal_rules.append((min(part[1].x,part[2].x),max(part[1].x,part[2].x),part[1].y))
                elif part[0]=='re' and part[1].height<2 and part[1].width>40:
                    horizontal_rules.append((part[1].x0,part[1].x1,(part[1].y0+part[1].y1)/2))
        protected_boxes = [rect(layout.get('box'),h) for layout in bpage.get('page_layout',[])
                           if str(layout.get('class_name','')).lower() in ('figure','table','isolate_formula','abandon')]
        protected_boxes += image_boxes
        reference_boxes=reference_pages[number]
        protected_boxes += reference_boxes
        # Symbol lists are often classified as ordinary prose by layout models.
        # Preserve a labelled nomenclature column through its next numbered section.
        extra_protected=nomenclature_regions(src.get_text('blocks'),src.rect.width,h)
        protected_boxes.extend(extra_protected)
        def already_preserved(para):
            region = rect(para.get('box'),h)
            return region is not None and any(box is not None and box.contains(region) for box in protected_boxes)
        # Debug IDs are random between passes. The source drawing order is stable
        # and survives translation/typesetting. Refuse ambiguous correspondence.
        by_order = {}
        for p in apage.get("pdf_paragraph", []):
            if p.get("debug_id"):
                order = p.get("render_order")
                if order in by_order:
                    raise ValueError("Ambiguous final paragraph identity")
                by_order[order] = p
        aparas = {p["debug_id"]:by_order.get(p.get("render_order")) for p in bparas if p.get("debug_id")}
        tparas = {k:v for k,v in aparas.items() if v is not None}
        source_by_id = {p['debug_id']:p for p in bparas if p.get('debug_id')}
        # Newly intersecting paragraph boxes indicate an unsafe composition.
        overlapping=set()
        final_items = [(identity,p) for identity,p in tparas.items() if not already_preserved(source_by_id[identity])]
        for index, (identity, para) in enumerate(final_items):
            first = rect(para.get('box'), h)
            if not first: continue
            for other_id, other in final_items[index+1:]:
                second = rect(other.get('box'), h)
                if not second: continue
                overlap = (first & second).get_area()
                if overlap <= max(4, min(first.get_area(),second.get_area())*.02): continue
                old_first = rect(source_by_id[identity].get('box'),h)
                old_second = rect(source_by_id[other_id].get('box'),h)
                if old_first and old_second and (old_first & old_second).get_area() <= 4:
                    overlapping.update((identity,other_id))
        for p in bparas:
            identity = p.get("debug_id")
            if not identity:
                continue
            # These regions are unconditionally copied from the original below.
            # Their discarded candidate layout must not force a whole-page fallback.
            if already_preserved(p):
                continue
            region = rect(p.get("box"), h)
            text = p.get("unicode") or ""
            a = aparas.get(identity)
            fonts = {f['font_id']:f for f in bpage.get('pdf_font', [])}
            problems = recognition_issues(p, fonts) + paragraph_issues(p, a, config["minFontScale"])
            if identity in fragmented:problems.append('source_word_split_between_paragraphs')
            if identity in overlapping:problems.append('paragraph_overlap')
            size=float((p.get('pdf_style') or {}).get('font_size') or 10)
            if region and any(region.y0+size*.5<y<region.y1-size*.5 and
                              min(x1,region.x1)-max(x0,region.x0)>max(30,region.width*.4)
                              for x0,x1,y in horizontal_rules):
                problems.append('source_paragraph_crosses_graphic_rules')
            protected_count += len(formula_chars(p))
            if a:
                bounds = rect(a.get("box"), h)
                if bounds is None or not src.rect.contains(bounds):
                    problems.append("text_outside_page")
                elif any(bounds.y0+size*.5<y<bounds.y1-size*.5 and
                         min(x1,bounds.x1)-max(x0,bounds.x0)>max(30,bounds.width*.4)
                         for x0,x1,y in horizontal_rules):
                    problems.append('translated_paragraph_crosses_graphic_rules')
            if not problems and len(normalize(text)) > 30:
                for r in records:
                    if r.get("issue") and SequenceMatcher(None, normalize(r["source"]), normalize(text)).ratio() > .72:
                        problems.append(r["issue"])
                        break
            if problems:
                merged, members = restoration_group(identity,source_by_id,aparas,h,src.rect)
                safe = merged is not None
                if safe:
                    restore.append((merged, merged, "quality_fallback"))
                else:
                    page_fail.extend(problems)
                issues.append({"page": number + 1, "region": list(merged if safe else region) if region else None,
                               "paragraph": identity, "types": problems, "fallback": "paragraph" if safe else "page",
                               "affectedParagraphs":sorted(members),
                               "referenceTranslation": reference_text([tparas[key] for key in members if key in tparas])})
        # Protect detected figures and table interiors with their original vector
        # content. Captions are distinct layout labels and remain translatable.
        for layout in bpage.get("page_layout", []):
            label = str(layout.get("class_name", "")).lower()
            if label in ("figure", "table", "isolate_formula", "abandon"):
                region = rect(layout.get("box"), h)
                if region and not region.is_empty:
                    restore.append((region, region, label))
                    preserved.append({"page": number + 1, "region": list(region), "reason": label})
        for region in extra_protected:
            restore.append((region,region,'nomenclature'))
            preserved.append({'page':number+1,'region':list(region),'reason':'nomenclature'})
        for region in image_boxes:
            if region.is_empty:continue
            restore.append((region,region,'original_image'))
            preserved.append({'page':number+1,'region':list(region),'reason':'original_image'})
        for region in reference_boxes:
            restore.append((region,region,'references'))
            preserved.append({'page':number+1,'region':list(region),'reason':'references'})
        # A protected figure/table must not erase body text that flowed into it.
        collisions=set()
        for erase, original, reason in list(restore):
            if reason == 'quality_fallback': continue
            for identity, para in tparas.items():
                old = rect(source_by_id[identity].get('box'),h)
                new = rect(para.get('box'),h)
                if not old or not new: continue
                if any(reason2=='quality_fallback' and restored.contains(new)
                       for restored,_,reason2 in restore):continue
                if (old & original).get_area() > old.get_area()*.5: continue
                if (new & erase).get_area() > max(4,new.get_area()*.02,(old & original).get_area()+2):
                    collisions.add(identity)
        for identity in collisions:
            merged,members=restoration_group(identity,source_by_id,aparas,h,src.rect)
            if merged is None:page_fail.append('protected_region_overlap')
            else:restore.append((merged,merged,'quality_fallback'))
            issues.append({'page':number+1,'region':list(merged) if merged else None,'paragraph':identity,
                           'types':['protected_region_overlap'],'fallback':'paragraph' if merged else 'page',
                           'affectedParagraphs':sorted(members),'referenceTranslation':reference_text([tparas[k] for k in members if k in tparas])})
        # Even without a flagged paragraph, an all-English result must be visible
        # as incomplete. This catches model failures that upstream silently keeps.
        chinese = sum('\u4e00' <= c <= '\u9fff' for c in dst.get_text())
        if chinese < 5 and len(normalize(src.get_text())) > 200 and not reference_boxes:
            page_fail.append("no_chinese_translation")
        if page_fail:
            output.insert_pdf(source, from_page=number, to_page=number)
            issues.append({"page": number + 1, "region": None, "types": sorted(set(page_fail)),
                           "fallback": "page", "referenceTranslation": reference_text(tparas.values())})
            for issue in issues:
                if issue["page"] == number + 1:
                    issue["fallback"] = "page"
            continue
        output.insert_pdf(candidate, from_page=number, to_page=number)
        out = output[-1]
        # Several failed fragments can close over the identical region. Paint
        # its source only once, otherwise antialiasing makes letters look bold.
        whole_restore=restore
        restore=[(area,area,'restored') for area in disjoint_regions([x[0] for x in restore])]
        for erase, original, reason in restore:
            out.add_redact_annot(erase, fill=(1, 1, 1))
        if restore:
            colorspaces = form_colorspaces(output)
            # Keep original image/vector objects outside the opaque restoration
            # mask. Pixel-redacting ICC images can corrupt their color resources;
            # deleting an intersecting vector path can erase its outside portion.
            out.apply_redactions(images=0, graphics=0, text=0)
            restore_form_colorspaces(output,colorspaces)
            for erase, original, reason in restore:
                out.show_pdf_page(original, source, number, clip=original, overlay=True)
        prose=[]
        for identity,para in tparas.items():
            old=rect(source_by_id[identity].get('box'),h);new=rect(para.get('box'),h)
            if old is None or new is None or already_preserved(source_by_id[identity]):continue
            if not any(area.contains(old) and area.contains(new) for area,_,_ in whole_restore):prose.append(new+(-1,-1,1,1))
        postflight=[]
        if restore and loses_unrestored_glyphs(dst,out,[area for area,_,_ in restore],prose):
            postflight.append('restoration_clips_text_glyph')
        if render_errors(out) - render_errors(src):postflight.append('pdf_render_failed')
        if postflight:
            output.delete_page(len(output)-1)
            output.insert_pdf(source, from_page=number, to_page=number)
            issues.append({'page':number+1,'region':None,'types':postflight,
                           'fallback':'page','referenceTranslation':reference_text(tparas.values())})
            for issue in issues:
                if issue['page']==number+1:issue['fallback']='page'
            continue
        if sum('\u4e00' <= c <= '\u9fff' for c in out.get_text()) >= 5:
            cjk_pages += 1
    output.set_metadata({"title": "Chinese reading copy", "producer": "Research Max Bilingual 0.2.3 / BabelDOC 0.7.1"})
    output.save(output_path, garbage=3, deflate=True)
    output.close()
    if sha256_file(source_path) != state["sourceHash"]:
        raise ValueError("Source hash changed")
    status = "failed" if cjk_pages == 0 else "completed_with_warnings" if issues else "completed"
    atomic_json(report_path, {"schema": 1, "status": status,
        "message": "未生成可用中文页面，已保留原文" if cjk_pages == 0 else f"译文已生成，{len(issues)} 处待核对" if issues else "译文已生成",
        "sourceHash": state["sourceHash"], "outputHash": sha256_file(output_path),
        "configFingerprint": state["configFingerprint"], "pages": len(source),
        "candidateHash": sha256_file(candidate_path), "finalLayoutHash":sha256_file(final_ir_path),
        "pagesWithChinese": cjk_pages, "protectedMathGlyphsChecked": protected_count,
        "issues": issues, "preserved": preserved, "manualReviewRequired": True,
        "checks": ["page_geometry", "math_glyph_identity", "math_relative_geometry", "minimum_font_scale",
                   "placeholder_integrity", "paragraph_presence", "page_bounds", "paragraph_overlap", "protected_region_overlap", "pdf_render"],
        "limitations": ["Formula detection and visual overlap still require human review.",
                        "Source layout is matched to the actual clean writer IR by original drawing order."]})
    source.close()
    candidate.close()
