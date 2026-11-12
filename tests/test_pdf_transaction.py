import sys,unittest
from pathlib import Path
import pymupdf as fitz
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.pdf_transaction import copy_artwork,commit_artwork

class PageTransactionTests(unittest.TestCase):
    def test_repeated_artwork_updates_preserve_xrefs_cross_page_links_and_annotations(self):
        with fitz.open() as doc:
            for i in range(3):doc.new_page().insert_text((40,50),'Original text')
            doc[0].insert_link({'kind':fitz.LINK_GOTO,'from':fitz.Rect(40,40,100,60),'page':2,'to':fitz.Point(0,0)})
            doc[1].insert_link({'kind':fitz.LINK_GOTO,'from':fitz.Rect(40,40,100,60),'page':0,'to':fitz.Point(0,0)})
            doc[0].add_text_annot((100,100),'Keep note')
            doc.set_toc([[1,'First',1],[1,'Third',3]])
            doc=doc # keep this context's document identity
            xrefs=[p.xref for p in doc];annots=doc.xref_get_key(xrefs[0],'Annots')
            for i in range(4):
                with fitz.open() as scratch:
                    copy_artwork(doc,0,scratch)
                    self.assertFalse(scratch[0].get_links());self.assertIsNone(scratch[0].first_annot)
                    scratch[0].insert_text((40,80+i*15),'Inserted '+str(i))
                    commit_artwork(doc,0,scratch)
            self.assertEqual([p.xref for p in doc],xrefs)
            self.assertEqual(doc.xref_get_key(xrefs[0],'Annots'),annots)
            self.assertEqual(doc[0].get_links()[0]['page'],2)
            self.assertEqual(doc[1].get_links()[0]['page'],0)
            self.assertEqual(doc.get_toc(),[[1,'First',1],[1,'Third',3]])
            self.assertIn('Inserted 3',doc[0].get_text())
            pixels=doc[0].get_pixmap().samples
            with fitz.open(stream=doc.tobytes(garbage=4,deflate=True),filetype='pdf') as saved:
                self.assertEqual(saved[0].get_links()[0]['page'],2)
                self.assertEqual(saved[0].first_annot.info['content'],'Keep note')
                self.assertEqual(saved[0].get_pixmap().samples,pixels)

if __name__=='__main__':unittest.main()
