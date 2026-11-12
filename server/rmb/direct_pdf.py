"""Local paragraph boundaries and vector-preserving inline typesetting."""
from collections import Counter
import html
import io
import os
from pathlib import Path
import statistics
import re
import unicodedata
import pymupdf as fitz

def prose_counter(text):
    # CJK fonts can map the same glyph to its canonically equivalent compatibility
    # ideograph. Normalize only those characters; never normalize math/scripts.
    normalized=''.join(unicodedata.normalize('NFC',c) if '\uf900'<=c<='\ufaff' or '\U0002f800'<=c<='\U0002fa1f' else c for c in text)
    # Source Han maps its prose hyphen glyph to U+2011 on PDF extraction.
    normalized=normalized.replace('\u2011','-').replace('\u22ef','\u2026')
    return Counter(c for c in normalized if not c.isspace())

def chinese_style(size,line_height):
    """Use the bundled academic Chinese font, independent of the source tree."""
    candidates=[Path(__file__).resolve().parents[2]/'resources/fonts',Path(__file__).resolve().parents[2]/'resources/babeldoc/fonts']
    if os.environ.get('RMB_FONT_DIR'):candidates.insert(0,Path(os.environ['RMB_FONT_DIR']))
    cache=os.environ.get('BABELDOC_CACHE_FOLDER')
    if cache:candidates.insert(0,Path(cache)/'fonts')
    font=next((p/'SourceHanSerifCN-Regular.ttf' for p in candidates if (p/'SourceHanSerifCN-Regular.ttf').is_file()),None)
    archive=fitz.Archive();face=''
    family='serif'
    if font:
        archive.add(str(font.parent))
        face="@font-face{font-family:RMBCJK;src:url(SourceHanSerifCN-Regular.ttf);}"
        family='RMBCJK,serif'
        latin=font.parent/'NotoSerif-Regular.ttf'
        if latin.exists():
            face+="@font-face{font-family:RMBLatin;src:url(NotoSerif-Regular.ttf);}"
    css=face+f'body,div{{margin:0;padding:0;font-family:{family};font-size:{size}pt;line-height:{line_height}pt;}}'
    if font:css+=f'body{{padding-top:{size*.3}pt;padding-bottom:{size*.3}pt;}}'
    return archive,css

def prose_html(text):
    return ''.join('<span style="font-family:RMBLatin,serif">'+html.escape(part)+'</span>'
                   if part.isascii() else html.escape(part)
                   for part in re.findall(r'[\x00-\x7f]+|[^\x00-\x7f]+',text))


def native_paragraphs(page):
    """Split only visible prose boundaries; never split same-baseline fragments."""
    result=[]
    for block in page.get_text('rawdict')['blocks']:
        lines=block.get('lines',[])
        if not lines:continue
        groups=[[]]
        left=min(l['bbox'][0] for l in lines)
        for line in lines:
            if groups[-1]:
                prev=groups[-1][-1];a=fitz.Rect(prev['bbox']);b=fitz.Rect(line['bbox'])
                sizes=[s['size'] for s in line['spans']];size=statistics.median(sizes or [10])
                oldsize=statistics.median([s['size'] for s in prev['spans']] or [10])
                oldtext=''.join(c['c'] for s in prev['spans'] for c in s.get('chars',[])).strip()
                advance=b.y0-a.y0>min(size,oldsize)*.7
                indented=(len(lines)>=6 and size*.55<b.x0-left<size*2.5
                          and (re.search(r'[.!?:][”\"\)]?$',oldtext) or a.x1<block['bbox'][2]-size*2))
                gap=b.y0-a.y1>max(size,oldsize)*.45
                fontchange=max(size,oldsize)>min(size,oldsize)*1.6
                if advance and (indented or gap or fontchange):groups.append([])
            groups[-1].append(line)
        if len(groups)==1:result.append(block);continue
        for group in groups:
            box=fitz.Rect(group[0]['bbox'])
            for line in group[1:]:box|=fitz.Rect(line['bbox'])
            result.append({**block,'bbox':tuple(box),'lines':group})
    result.sort(key=lambda b:(int(b['bbox'][0]>=page.rect.width/2),b['bbox'][1],b['bbox'][0]))
    return result


def compose_inline(source,region,text,objects,size,scale,*,leading=1.5):
    """Reserve inline slots, then place original PDF vectors into those slots.

    The temporary PNGs are blank layout spacers only. No formula is rasterized,
    retyped or sent to a vision model. Unisolated / multiline objects are rejected
    by the caller before this function is entered.
    """
    from .reading import MARKER,is_literal,object_box,literal_text
    blank=fitz.open()
    p=blank.new_page(width=2,height=2)
    spacer=p.get_pixmap().tobytes('png');blank.close()
    boxes=[];parts=[];position=0
    for match in MARKER.finditer(text):
        parts.append(prose_html(text[position:match.start()]));obj=objects[int(match[1])]
        if is_literal(obj):parts.append(prose_html(obj['text']))
        else:
            rawbox=object_box(obj)
            box=(rawbox+(-.25,-.25,.25,.25))&source.rect
            if box.is_empty or not (region+(-.3,-.3,.3,.3)).contains(box):return None
            boxes.append(box)
            parts.append(f'<img src="slot.png" style="width:{box.width*scale}pt;height:{box.height*scale}pt;vertical-align:middle;margin-left:1pt;margin-right:1pt"/>')
        position=match.end()
    parts.append(prose_html(text[position:]))
    rect=fitz.Rect(0,0,region.width,region.height)
    line_height=max([size*scale*1.15]+[b.height*scale*leading for b in boxes])
    archive,css=chinese_style(size*scale,line_height)
    archive.add((spacer,'slot.png'))
    story=fitz.Story('<div>'+''.join(parts)+'</div>',user_css=css,archive=archive)
    top=max([0]+[b.height*scale-size*scale*.75 for b in boxes])+1
    more,filled=story.place(fitz.Rect(0,top,rect.x1,rect.y1))
    if more or fitz.Rect(filled).y1>rect.y1+.05:return None
    buffer=io.BytesIO();writer=fitz.DocumentWriter(buffer)
    device=writer.begin_page(rect);story.draw(device);writer.end_page();writer.close()
    snippet=fitz.open(stream=buffer.getvalue(),filetype='pdf');p=snippet[0]
    slots=p.get_image_info()
    if len(slots)!=len(boxes):snippet.close();return None
    for slot,box in zip(slots,boxes):
        dest=fitz.Rect(slot['bbox'])
        if not rect.contains(dest):snippet.close();return None
        # The Story line box does not always reserve the image's full vertical
        # extent. Reject any slot touching another visible glyph before placing
        # vectors, including an object on the preceding line.
        for block in p.get_text('rawdict')['blocks']:
            for line in block.get('lines',[]):
                for span in line['spans']:
                    for char in span['chars']:
                        if char['c'].strip() and (fitz.Rect(char['bbox'])&dest).get_area()>.01:
                            snippet.close();return None
        p.show_pdf_page(dest,source.parent,source.number,clip=box,keep_proportion=True)
        # Verify exact character sequence and relative two-dimensional geometry,
        # including superscripts/subscripts, after the PDF transform.
        before=[c for b in source.get_text('rawdict',clip=box)['blocks'] for l in b.get('lines',[]) for s in l['spans'] for c in s['chars'] if c['c'].strip()]
        after=[c for b in p.get_text('rawdict',clip=dest)['blocks'] for l in b.get('lines',[]) for s in l['spans'] for c in s['chars'] if c['c'].strip()]
        if len(before)!=len(after):snippet.close();return None
        ratio=dest.width/box.width
        for a,b in zip(before,after):
            expected=(dest.x0+(a['origin'][0]-box.x0)*ratio,dest.y0+(a['origin'][1]-box.y0)*ratio)
            if a['c']!=b['c'] or max(abs(expected[i]-b['origin'][i]) for i in (0,1))>.15:
                snippet.close();return None
    expected=prose_counter(MARKER.sub('',literal_text(text,objects)))
    actual=prose_counter(p.get_text())
    if expected-actual:snippet.close();return None
    return snippet
