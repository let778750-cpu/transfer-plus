"""Re-render an existing local job with saved translations; makes NO API calls."""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from rmb.config import atomic_json

def main():
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--job',required=True)
    a=p.parse_args();job=a.data/'jobs'/a.job
    config=json.loads((job/'config.json').read_text(encoding='utf-8'))
    state=json.loads((job/'job.json').read_text(encoding='utf-8'))
    records=json.loads((job/'translation-records.json').read_text(encoding='utf-8'))
    import babeldoc.const as const
    const.CACHE_FOLDER=a.data/'cache'/'babeldoc';const.TIKTOKEN_CACHE_FOLDER=const.CACHE_FOLDER/'tiktoken'
    os.environ['TIKTOKEN_CACHE_DIR']=str(const.TIKTOKEN_CACHE_FOLDER)
    from rmb.translator import GuardedTranslator
    class Replay(GuardedTranslator):
        def __init__(self):
            super().__init__(config,'offline-replay-unused')
            self.saved={r['source']:r for r in records};self.missing=0
        def do_llm_translate(self,text,rate_limit_params=None):
            if text is None:return None
            def one(source):
                r=self.saved.get(source)
                if r is None:
                    self.missing+=1
                    self.records.append({'source':source,'translation':'','issue':'replay_missing'})
                    return source
                return source if r.get('issue') or r.get('protected') else r['translation']
            marker='## Here is the input:'
            if marker in text:
                batch=json.loads(text.rsplit(marker,1)[1].strip())
                return json.dumps([{'id':r['id'],'output':one(r['input'])} for r in batch],ensure_ascii=False)
            return one(text.rsplit('Now translate the following text:',1)[-1].strip())
    from rmb.engine_patch import install_clean_capture, FORMULA_FONT_PATTERN
    from rmb.quality import verify_and_compose
    from babeldoc.format.pdf import high_level
    from babeldoc.format.pdf.translation_config import TranslationConfig
    from babeldoc.progress_monitor import ProgressMonitor
    logging.disable(logging.CRITICAL);install_clean_capture();high_level.init()
    translator=Replay();work=job/'recheck'
    cfg=TranslationConfig(translator=translator,input_file=job/'source.pdf',lang_in='en',lang_out='zh-CN',
        doc_layout_model=None,pages=state.get('pages'),no_dual=True,qps=2,pool_max_workers=2,
        auto_extract_glossary=False,use_rich_pbar=False,save_auto_extracted_glossary=False,
        auto_enable_ocr_workaround=False,ocr_workaround=False,disable_same_text_fallback=True,
        formular_font_pattern=FORMULA_FONT_PATTERN,
        custom_system_prompt='Preserve all formula placeholders and identifiers exactly.',debug=False,
        output_dir=work/'output',working_dir=work/'ir')
    def notify(**e):
        if e.get('type')=='progress_start':print(e.get('stage'),flush=True)
    with ProgressMonitor(high_level.get_translation_stage(cfg),progress_change_callback=notify) as monitor:
        result=high_level.do_translate(monitor,cfg)
    verify_and_compose(job/'source.pdf',Path(result.mono_pdf_path),job/'engine-work'/'inspection-ir'/'source',
        work/'result.pdf',work/'report.json',config,records+translator.records,state,
        final_ir_path=work/'ir'/'source'/'final-layout.json')
    report=json.loads((work/'report.json').read_text(encoding='utf-8'))
    print(json.dumps({'status':report['status'],'issues':len(report['issues']),'chinesePages':report['pagesWithChinese'],
        'mathGlyphs':report['protectedMathGlyphsChecked'],'replayMissing':translator.missing}),flush=True)

if __name__=='__main__':main()
