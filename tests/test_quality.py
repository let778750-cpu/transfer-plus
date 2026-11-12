import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.quality import paragraph_issues, formula_geometry_ok, split_word_paragraphs

def glyph(c, ident, x, y):
    return {'char_unicode':c,'pdf_character_id':ident,'pdf_style':{'font_id':'Math','font_size':10},'box':{'x':x,'y':y,'x2':x+5,'y2':y+10},'render_order':ident}

class QualityTests(unittest.TestCase):
    def test_word_split_across_translation_units_is_detected(self):
        first={'debug_id':'a','unicode':'ten r','box':dict(x=10,x2=40,y=10,y2=20)}
        second={'debug_id':'b','unicode':'uns of the experiment','box':dict(x=40.05,x2=90,y=10,y2=20)}
        self.assertEqual(split_word_paragraphs([first,second]),{'a','b'})
        first['unicode']='An LL';second['unicode']='M-based translation'
        self.assertEqual(split_word_paragraphs([first,second]),{'a','b'})
        second['box']['x']=43
        self.assertEqual(split_word_paragraphs([first,second]),set())
    def test_empty_prose_and_partially_missing_translated_glyphs(self):
        before={'unicode':'A paragraph which must not silently disappear.'}
        self.assertIn('paragraph_text_missing',paragraph_issues(before,{'unicode':'','pdf_paragraph_composition':[]}))
        self.assertIn('paragraph_not_drawn',paragraph_issues(before,{'unicode':'中文内容','pdf_paragraph_composition':[]}))
        after={'unicode':'中文内容','pdf_paragraph_composition':[{'pdf_character':glyph('中',1,0,0)}]}
        self.assertIn('translated_glyph_missing',paragraph_issues(before,after))
    def test_missing_math(self):
        a={'pdf_paragraph_composition':[{'pdf_formula':{'pdf_character':[glyph('x',1,0,0),glyph('2',2,5,4)]}}]}
        b=copy.deepcopy(a);b['pdf_paragraph_composition'][0]['pdf_formula']['pdf_character'].pop()
        self.assertIn('math_glyph_changed',paragraph_issues(a,b))
    def test_superscript_shift_rejected(self):
        a=[glyph('x',1,0,0),glyph('2',2,5,4)];b=copy.deepcopy(a);b[1]['box']['y']=0
        self.assertFalse(formula_geometry_ok(a,b))
    def test_uniform_translation_allowed(self):
        a=[glyph('x',1,0,0),glyph('2',2,5,4)];b=copy.deepcopy(a)
        for c in b:
            for axis in ('x','x2'):c['box'][axis]+=23
            for axis in ('y','y2'):c['box'][axis]+=71
        self.assertTrue(formula_geometry_ok(a,b))
    def test_shrink_and_missing(self):
        self.assertIn('font_below_minimum',paragraph_issues({}, {'scale':.7}))
        self.assertEqual(paragraph_issues({},None),['paragraph_missing'])
    def test_subscript_size_corruption(self):
        a=[glyph('x',1,0,0),glyph('i',2,5,-3)]
        a[1]['pdf_style']['font_size']=6
        b=copy.deepcopy(a);b[1]['pdf_style']['font_size']=60;b[1]['box']['x2']+=40
        self.assertFalse(formula_geometry_ok(a,b))
    def test_flattened_math_is_valid(self):
        chars=[glyph('x',1,0,0),glyph('2',2,5,4)]
        before={'pdf_paragraph_composition':[{'pdf_formula':{'pdf_character':chars}}]}
        after={'pdf_paragraph_composition':[{'pdf_character':copy.deepcopy(c)} for c in chars]}
        for part in after['pdf_paragraph_composition']:part['pdf_character']['render_order']=1
        self.assertEqual(paragraph_issues(before,after),[])

if __name__=='__main__':unittest.main()
