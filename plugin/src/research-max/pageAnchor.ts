// Read live page geometry rather than mixing pdf.js's debounced location with
// its current page number. Page indices are physical, independent of labels.
export type PageAnchor = {pageIndex: number; fraction: number};
export function readPageAnchor(viewer: any, container: any): PageAnchor | null {
  if (!viewer?.pagesCount || !container) return null;
  const top = container.getBoundingClientRect().top + (container.clientTop || 0);
  let lo = 0, hi = viewer.pagesCount - 1;
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    const box = viewer.getPageView(mid)?.div?.getBoundingClientRect();
    if (!box || box.height <= 0) return null;
    if (box.bottom <= top) lo = mid + 1; else hi = mid;
  }
  const box = viewer.getPageView(lo)?.div?.getBoundingClientRect();
  if (!box || box.height <= 0) return null;
  return {pageIndex: lo, fraction: Math.max(0, Math.min(1, (top - box.top) / box.height))};
}
export function writePageAnchor(viewer: any, container: any, anchor: PageAnchor): boolean {
  if (!container || !Number.isInteger(anchor.pageIndex) || !Number.isFinite(anchor.fraction)
      || anchor.pageIndex < 0 || anchor.pageIndex >= (viewer?.pagesCount || 0)) return false;
  const box = viewer.getPageView(anchor.pageIndex)?.div?.getBoundingClientRect();
  if (!box || box.height <= 0) return false;
  const top = container.getBoundingClientRect().top + (container.clientTop || 0);
  const target = container.scrollTop + box.top - top + Math.max(0, Math.min(1, anchor.fraction)) * box.height;
  container.scrollTop = Math.max(0, Math.min(container.scrollHeight - container.clientHeight, target));
  return true;
}
