/**
 * jarErrorKind — Classify raw error text that mentions the bundled JAR files.
 *
 * Two distinct failure shapes used to hide behind "any message containing
 * the substring jar" (the old blanket rule in friendlyOdlError):
 *
 *   - "missing": the component file does not exist on disk at all — a packed
 *     XPI install whose extraction failed, a file deleted by antivirus, or a
 *     broken build. Reinstalling the plugin is the right advice.
 *   - "unreadable": the file exists but Java or the OS refused to open it —
 *     the JVM launcher prints "Error: Unable to access jarfile <path>"
 *     (permissions, AV lock, invalid characters in the path). Reinstalling
 *     usually does NOT help; checking quarantine and file permissions does.
 *
 * Anything else that merely contains "jar" — a java.lang.* stack trace from a
 * RUNNING JVM, a URL, a model name — is NOT a component error. Callers must
 * let it fall through to their other classifiers, mirroring the lesson of
 * splitViewFactory's JAVA_MISSING_RE: never match a bare "java" substring.
 *
 * Zero dependencies; importable from Node tests as-is.
 */

export type JarErrorKind = "download-failed" | "missing" | "unreadable";

/**
 * 下载兜底失败：精简版安装里组件缺失的最常见原因。它的错误文本内嵌在
 * 「JAR file not found …」外壳里，必须先于 missing 判定，否则会被吞掉。
 */
const DOWNLOAD_FAILED_RE = /odl jar download failed|解析组件下载失败/i;

/**
 * Missing component: our own "JAR file not found at <path>" report from
 * runOpenDataLoader, PdfParseError's "OpenDataLoader JAR is missing." hint,
 * and the zh-CN odl-error-jar-missing wording (a friendly message can be
 * re-wrapped by an outer error and re-classified).
 */
const MISSING_RE =
  /jar\s*file\s*not\s*found|jar\s+is\s+missing|(?:缺失|未找到)[^\n]{0,10}jar|jar[^\n]{0,10}(?:缺失|未找到)/i;

/**
 * Unreadable component: the JVM launcher's "Error: Unable to access jarfile"
 * and the err-jar-load wording (nsIFile.initWithPath refused the path) in
 * both locales.
 */
const UNREADABLE_RE =
  /unable\s+to\s+access\s+jarfile|could\s+not\s+(?:read|load|open)[^\n]{0,60}\bjar(?:\s*文件)?\b|无法读取解析组件|无法访问\s*jar\s*文件/i;

/**
 * Classify a raw error message. Returns null when the text does not describe
 * a missing/unreadable bundled component — the caller should keep applying
 * its other classifiers (HTTP status, Java launch failure, network, …).
 */
export function classifyJarError(message: string): JarErrorKind | null {
  const msg = message || "";
  if (DOWNLOAD_FAILED_RE.test(msg)) return "download-failed";
  if (MISSING_RE.test(msg)) return "missing";
  if (UNREADABLE_RE.test(msg)) return "unreadable";
  return null;
}
