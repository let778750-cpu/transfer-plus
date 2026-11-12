"""Render local acceptance pages into an explicitly selected, private directory."""
import argparse
import json
from pathlib import Path
import pymupdf

p=argparse.ArgumentParser();p.add_argument('pdf',type=Path);p.add_argument('output',type=Path)
p.add_argument('--pages');a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
with pymupdf.open(a.pdf) as pdf:
    pages=[int(x)-1 for x in a.pages.split(',')] if a.pages else range(len(pdf))
    for i in pages:pdf[i].get_pixmap(matrix=pymupdf.Matrix(1.5,1.5),alpha=False).save(a.output/f'page-{i+1:02}.png')
    print(json.dumps({'pages':len(pdf),'rendered':list(i+1 for i in pages)}))
