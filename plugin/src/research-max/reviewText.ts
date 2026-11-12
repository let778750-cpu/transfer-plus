// Old archives can contain BabelDOC's internal math/style tokens. They are not
// readable formulas; do not expose them or reconstruct unverified mathematics.
export function readableReference(value:any){
  const text=String(value||'').replace(/<\s*\/?\s*style\b[^>]*>/g,'');
  if(!/[\u4e00-\u9fff]/.test(text))return '本区域保留原文，没有可用的中文参考文本。';
  return text.replace(/\{\s*v\s*\d+\s*\}/g,'〔公式或标记见原文〕').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g,'');
}
const labels:Record<string,string>={
  ocr_needs_review:'图片文字识别或方向待核对',ocr_math_alignment:'图片中的公式文字无法可靠分离',
  recognition_empty:'本页尚未得到可靠文字清单',translation_incomplete:'译文尚未完整返回',
  annotation_anchor:'中文批注所在区域保留旧版本',
  layout_pending:'本段尚未安全排入中文 PDF',
  font_below_minimum:'译文字号过小',math_geometry_changed:'数学对象位置变化',math_glyph_changed:'数学符号变化',
  source_word_split_between_paragraphs:'原文单词被拆分',partial_math_recognition_uncertain:'行内符号识别存疑',
  multiline_math_recognition_uncertain:'跨行符号识别存疑',restoration_clips_text_glyph:'局部恢复影响相邻文字',
  paragraph_overlap:'段落重叠',text_outside_page:'文字超出页面',protected_region_overlap:'正文进入保留区域',
  paragraph_missing:'段落缺失',paragraph_text_missing:'段落内容缺失',paragraph_not_drawn:'段落未显示',
  translated_glyph_missing:'译文字符缺失',math_placeholder_changed:'数学对象标记变化',
  source_paragraph_crosses_graphic_rules:'原文段落跨越分隔线',translated_paragraph_crosses_graphic_rules:'译文跨越分隔线',
  no_chinese_translation:'未生成中文',audit_page_missing:'缺少页面核验数据',scan_or_damaged_text_layer:'扫描页或文字层异常',
  pdf_render_failed:'页面显示异常',paragraph_structure_changed:'译文结构异常',empty_translation:'译文为空',unexpected_expansion:'译文长度异常'
};
export function issueLabel(type:string){return labels[type]||(type.startsWith('model_request_failed')?'翻译接口未完成':'需要人工核对');}
