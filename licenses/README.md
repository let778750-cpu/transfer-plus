# 来源与许可证

本组件采用 AGPL-3.0-or-later。修改后的插件、服务和引擎适配源码随源码包提供；复用文件保留原作者声明。

| 项目 | 固定版本 / 提交 | 用途 | 许可 |
|---|---|---|---|
| [Z-Transplit](https://github.com/Liozhang/z-transplit/tree/v1.20.0) | 1.20.0 / 490612cba42e39eb8167b595a19b22a0f16b4114 | ReaderPane 与分屏同步，已适配批注 | AGPL-3.0 |
| [Zotero PDF2zh](https://github.com/guaguastandup/zotero-pdf2zh/tree/v4.1.8) | 4.1.8 / 5dd2eaf019f71266764c269bbd2104b559ae95cf | TaskManager | AGPL-3.0 |
| [PDFMathTranslate Next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/tree/v2.10.2) | 2.10.2 | 0.2.x 历史依赖，0.3 默认运行包已移除 | AGPL-3.0 |
| [BabelDOC](https://github.com/funstory-ai/BabelDOC/tree/v0.7.1) | 0.7.1 / d922e2b0c5a353bde8731921eb442c07ae75849d | 保留历史适配源码和许可，0.3 不再调用全流程 | AGPL-3.0 |
| [Source Han Serif](https://github.com/adobe-fonts/source-han-serif) | 原引擎资源，文件哈希随运行包登记 | CJK 字体 | SIL OFL 1.1 |
| [Noto Serif](https://github.com/notofonts/noto-fonts) | 原资源中的 NotoSerif-Regular.ttf，SHA256 随运行包登记 | 拉丁字母后备字体 | SIL OFL 1.1，见 Noto-Serif-LICENSE.txt |

运行包保留 Python 自带 LICENSE、各依赖的 dist-info 许可证和原始元数据；构建时另生成 dependency-inventory.json，不将主项目 AGPL 视为覆盖所有第三方依赖。0.3 仅复制排版字体，不打包推理模型或翻译缓存。包清单记录每个文件 SHA256。

PaddleOCR 使用飞桨 AI Studio 的公开 HTTP 接口，不分发 Paddle 框架或模型。接口模型为 PaddleOCR-VL-1.6、PP-OCRv6、PP-StructureV3；依官方异步任务及参数文档实现客户端，服务使用条件和额度以服务商账户为准。PyMuPDF、OpenAI 客户端、Flask、Requests、Pillow 和 FileLock 的准确版本及许可见运行包 dependency-inventory.json 和各自 dist-info。

Zotero 插件清单要求 HTTPS 更新地址。此组件使用 IANA 保留的 `.invalid` 域名作为手动发布占位，并关闭自动更新；不向上游插件的更新地址发送请求，也没有可自动替换本组件的远端发布源。
