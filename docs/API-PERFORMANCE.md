# 0.3.4 调用方式与性能排查

## 本次证据

用户的 19 页任务在本机排版首次提交时触发 PyMuPDF Link.next / get_links / _do_links 的 IndexError。已用保存的识别清单和译文离线复现；故障不来自 PaddleOCR 的 OCR 输出或 GLM 请求。反复删除并插回页对象会让链接目标处理脆弱，改为试排页不复制交互对象、只提交 Contents 与 Resources，保留页面身份及其批注/链接。

旧任务文件时间显示识别清单建立约 4 分钟，随后串行翻译约 32 分 38 秒；这些是文件时间估算。清单中存在大量很短的原生/图片文本片段，逐段串行请求及逐图创建 OCR 作业扩大了等待开销。0.3.4 引入分段校验的批量翻译、四请求并发、统一节流、批量图片 OCR，不以移除公式保护换速度。

## 已测数据与限制

| 测试 | 结果 | 解释边界 |
|---|---|---|
| 同一 GLM、相同 8 段合成学术文本 | 串行 8 请求 11.86 秒；批量 1 请求 4.58 秒，8 段通过校验 | 单次小样本，不是整个文献或不同翻译模型对比 |
| PP-OCRv6 合成图片两区域 | 1 个真实云端作业，8.28 秒，返回两页文字与尺寸正确 | 验证裁剪批次及映射，不证明所有图表均可识别 |
| 19 页失败任务恢复 | 识别缓存 0.031 秒，翻译 139.484 秒，排版 184.610 秒，核验 0.406 秒，总计 324.7 秒 | 使用已有缓存；不是从零冷启动测速 |
| 恢复时翻译请求 | 151 请求、618 次段落缓存命中；网络请求累计 526.359 秒 | 累计耗时包含并行，不等于墙钟耗时；失败段可能重试 |
| 恢复 PDF | 19 页保持，正文清单 9919 原文词中 7116 写入 PDF、2803 未完成 | 约 71.7% 是清单覆盖估算，不是译文正确率，也不是全文所有字形的比例 |
| 最终资源去重 | 525983438 字节降到 6878911 字节，去重 1.48 秒 | 19 页逐页渲染及链接比较相同；避免重复字体/图片流随试排累积 |

当前仍有复杂公式相邻文本、背景复杂的图中文字及少量未通过译文校验的区域待核对。它们保留原文且计为未完成。批量传输不能修复所有识别或段落分割错误。未用整个文库测试，未做 Google 与 GLM 的同文同机冷启动比较。

## API、MCP、网页的选择

- **API：继续作为插件主路径。**可持久化远端任务、严格校验分页、复用缓存、批量提交并控制取消后的行为；已经核对官方 multipart、Bearer、模型和 optionalPayload 字段。
- **MCP：适合让智能体调用 OCR。**官方 AI Studio 模式内部使用 AsyncPaddleOCRClient 的 parse_document / ocr，仍走 API；不会因此自动更换识别模型，也不会解决本机 PDF 链接错误。没有为本插件增加这一层调用链。
- **网页：适合人工预览和对照识别效果。**本轮未对网页做同文同参数测速。无法断言其队列优先级或预处理与 API 完全相同；没有证据证明仅换入口就更快、更准确。

官方来源：[MCP 实现](https://github.com/PaddlePaddle/PaddleOCR/blob/main/mcp_server/paddleocr_mcp/inference/paddleocr_vl/aistudio.py)、[MCP 使用说明](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/integrations/mcp_server.en.md)、[异步 API](https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5)。

## 开源项目借鉴

PDFMathTranslate Next 明确区分 QPS 与工作线程数并支持缓存；本次采用同类的“限速与并发分开、缓存优先”组织方式，没有重新引入本机推理全套依赖。pdf-craft 的持久化提取结果与独立翻译阶段也支持这种分层思路；其面向书籍重排的整套输出方式不直接替换当前固定页数双栏工具。

沉浸式翻译的 PDF 帮助将基础阅读器描述为实时对照阅读，并说明纯文本识别对公式和表格有局限。用户体验表明快速可读的文字交付值得借鉴，但不能据此推算带原位公式核验 PDF 的耗时；此前串行等待是本插件可证实的设计问题。本版仍使用用户已配置的 GLM，没有宣称接入免费 Google，也没有把其他软件的宣传速度当作实测。

来源：[PDFMathTranslate Next 参数](https://github.com/PDFMathTranslate/PDFMathTranslate-next/blob/main/docs/en/advanced/advanced.md)、[pdf-craft 翻译](https://github.com/oomol-lab/pdf-craft/blob/main/docs/en/PDF_TRANSLATION.md)、[沉浸式翻译 PDF 帮助](https://immersivetranslate.com/en/docs/pdf-helper/)。
