export function getString(key: string) { return ({
  "splitview-error-reader-timeout": "PDF 阅读器初始化超时",
  "splitview-error-scroll-timeout": "PDF 视图尚未准备好"
} as any)[key] || key; }
