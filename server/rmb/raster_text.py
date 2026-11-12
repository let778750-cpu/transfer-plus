"""Conservative separation of isolated OCR labels from chart artwork."""
from collections import deque
import pymupdf as fitz


def cover_is_clear(page,boxes,owned_rects):
    """White formula masks must not erase a background or unrelated stroke."""
    if any(box.intersects(fitz.Rect(info['bbox'])) for info in page.get_image_info() for box in boxes):return False
    keys={tuple(round(v,2) for v in r) for r in (owned_rects or [])}
    for drawing in page.get_drawings():
        if tuple(round(v,2) for v in drawing['rect']) in keys:continue
        color=drawing.get('color') or drawing.get('fill')
        if color and min(color)>.98:continue
        pad=max(.1,float(drawing.get('width',0) or 0)/2)
        for item in drawing['items']:
            if item[0]=='re':
                r=item[1]
                if 'f' in drawing.get('type','') and any(r.intersects(b) for b in boxes):return False
                points=[(r.tl,r.tr),(r.tr,r.br),(r.br,r.bl),(r.bl,r.tl)]
            elif item[0]=='l':points=[(item[1],item[2])]
            elif item[0]=='c':points=[(item[1],item[2],item[3],item[4])]
            else:continue
            for ps in points:
                r=fitz.Rect(min(p.x for p in ps),min(p.y for p in ps),max(p.x for p in ps),max(p.y for p in ps))+(-pad,-pad,pad,pad)
                if any(r.intersects(b) for b in boxes):return False
    return True


def crossing_path(page,box):
    # A PDF can paint hundreds of disjoint glyphs in ONE path. Its overall bbox
    # is not an obstacle. Examine primitives; an edge crossing the label is.
    for drawing in page.get_drawings():
        color=drawing.get('color') or drawing.get('fill')
        if color and min(color)>.9:continue
        for item in drawing['items']:
            if item[0]=='re':
                r=item[1];pairs=[(r.tl,r.tr),(r.tr,r.br),(r.br,r.bl),(r.bl,r.tl)]
            elif item[0]=='l':pairs=[(item[1],item[2])]
            elif item[0]=='c':pairs=[(item[1],item[2]),(item[2],item[3]),(item[3],item[4])]
            else:continue
            for a,b in pairs:
                r=fitz.Rect(min(a.x,b.x),min(a.y,b.y),max(a.x,b.x),max(a.y,b.y))+(-.1,-.1,.1,.1)
                if r.intersects(box) and not box.contains(r):return True
    return False


def has_solid_marker(image,background):
    """Reject filled legend dots/boxes that whole-line OCR may include as text.

    This is a local safeguard, not a general image segmentation guarantee.
    """
    pixels=image.load();w,h=image.size
    ink={(x,y) for y in range(h) for x in range(w)
         if max(abs(a-b) for a,b in zip(pixels[x,y],background))>100}
    components=[]
    while ink:
        first=ink.pop();queue=deque([first]);xs=[];ys=[]
        while queue:
            x,y=queue.popleft();xs.append(x);ys.append(y)
            for p in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                if p in ink:ink.remove(p);queue.append(p)
        if len(xs)>=8:components.append((len(xs),max(xs)-min(xs)+1,max(ys)-min(ys)+1))
    max_height=max((c[2] for c in components),default=0)
    return any(height>=max_height*.9 and .8<width/height<1.5 and count/(width*height)>.65
               for count,width,height in components)
