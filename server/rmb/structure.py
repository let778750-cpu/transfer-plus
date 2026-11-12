"""Bound reference sections by actual headings; appendices are body content."""
import re
import statistics
import pymupdf as fitz

REFERENCE = re.compile(r'^(?:(?:[IVX]+|\d+)[. ]+)?(?:REFERENCES|BIBLIOGRAPHY)\s*$',re.I)
APPENDIX = re.compile(r'^(?:(?:[A-Z]|\d+)[. ]+)?(?:APPENDI(?:X|CES)|SUPPLEMENT(?:ARY|AL)(?:\s+MATERIALS?|\s+INFORMATION)?|SUPPORTING INFORMATION)\b',re.I)

def reference_regions(document):
    result={};inside=False
    for index,page in enumerate(document):
        lines=[]
        for block in page.get_text('dict')['blocks']:
            for line in block.get('lines',[]):
                spans=line.get('spans',[]);text=''.join(s.get('text','') for s in spans).strip()
                if text:lines.append((fitz.Rect(line['bbox']),text,spans))
        sizes=[float(s['size']) for _,_,spans in lines for s in spans if len(s.get('text','').strip())>10]
        body=statistics.median(sizes) if sizes else 10
        lines.sort(key=lambda v:(int(v[0].x0>=page.rect.width/2),v[0].y0,v[0].x0))
        boxes=[];last_column=None;last_was_reference=False
        for box,text,spans in lines:
            named_end=bool(APPENDIX.match(text))
            # A lettered bold/large section heading can follow the last citation.
            lettered=bool(re.match(r'^[A-Z][. ]+\s*[A-Z][A-Za-z -]{4,80}$',text))
            heading=any(re.search(r'bold',s.get('font',''),re.I) or s.get('size',0)>body*1.15 for s in spans)
            if inside and (named_end or (lettered and heading)):inside=False
            if REFERENCE.fullmatch(text):inside=True
            if inside:
                column=int(box.x0>=page.rect.width/2)
                if boxes and last_was_reference and column==last_column:
                    boxes[-1]=boxes[-1] | (box+(-.5,-.5,.5,.5))
                else:boxes.append(box+(-.5,-.5,.5,.5))
                last_column=column
            last_was_reference=inside
        result[index]=boxes
    return result
