// Split-pane mechanics reused from Z-Transplit v1.20.0 (AGPL-3.0-or-later).
import {ReaderPane, createReaderBrowser} from "../core/pdf/splitview/readerPaneAdapter";
import {installSync, makeSyncTrigger, stateMap, primeSyncBaselines} from "../core/pdf/splitview/splitViewSync";
import {flushAnnotationWrites} from "../core/pdf/splitview/readerConfig";
import {version} from "../../research-max/manifest.json";
import {issueLabel} from './reviewText';
import {coverageText,reviewIssues} from './readingPanel';
import {bindSingleClick,installButtonStyle} from './taskControls';
const HTML = "http://www.w3.org/1999/xhtml";
const active = new Map<string, any>();
// Programmatic closes (restore, update, shutdown, stale replace) must not be
// mistaken for the user closing the tab: only a user close disables the pair.
const suppressed = new Set<string>();
export function suppressPairClose(id: string) { suppressed.add(id); }
export function activePairs() {
  return [...active.values()].map(s => ({leftID: s.leftAdapter.item.id, tabID: s.tabID,
    select: () => s.win.Zotero_Tabs.select(s.tabID)}));
}
export async function closePairAndRestore(id: string) {
  const s = active.get(id); if (!s) return;
  const left = s.leftAdapter.item, win = s.win;
  suppressPairClose(id); closePair(id);
  try { win.Zotero_Tabs.close(id); } catch (error:any) { Zotero.logError(error); }
  // Focus the original attachment; Zotero.Reader.open reuses an existing tab.
  try { await Zotero.Reader.open(left.id); } catch (error:any) { Zotero.logError(error); }
}
function element(doc: any, tag: string, text = "") { const el = doc.createElementNS(HTML, tag); el.textContent = text; return el; }
export async function openPair(win: any, left: any, right: any, report: any) {
  const found = [...active.values()].find(s => s.leftAdapter.item.id === left.id && s.rightAdapter.item.id === right.id);
  if (found && JSON.stringify(found.report)===JSON.stringify(report)) { win.Zotero_Tabs.select(found.tabID); return found; }
  if(found){suppressPairClose(found.tabID);closePair(found.tabID);await flushAnnotationWrites();win.Zotero_Tabs.close(found.tabID);}
  const {id} = win.Zotero_Tabs.add({type: "reader", title: left.parentItem?.getField("title") || left.getField("title"),
    data: {itemID: left.id, isSplitView: true}, select: true,
    // The tab itself is the paper's reading view; the two panes are the marker.
    onClose: () => {const wasSuppressed=suppressed.delete(id);const s=active.get(id);
      const closedLeft=s?.leftAdapter?.item;closePair(id);
      if(!wasSuppressed&&closedLeft)(Zotero as any).ResearchMaxBilingual?.onPairUserClosed?.(closedLeft.id);}});
  try {
  let old;
  for (let i=0; i<40 && !old; i++) { old = win.document.getElementById(id); if (!old) await Zotero.Promise.delay(50); }
  if (!old) throw new Error("未能创建中文对照标签页");
  const doc = win.document, container = old.cloneNode(false);
  old.parentNode.replaceChild(container, old);
  container.setContextPaneOpen = (open: boolean) => container.dispatchEvent(new win.CustomEvent("tab-context-pane-toggle", {detail: {open}}));
  container.setBottomPlaceholderHeight = () => {};
  container.onTabSelectionChanged = () => {};
  container.style.cssText = "display:flex;flex-direction:column;height:100%;overflow:hidden;";
  const toolbar = element(doc, "div"); toolbar.style.cssText = "display:flex;gap:12px;align-items:center;padding:7px 12px;background:var(--material-toolbar,#eee);font-size:13px;";
  const label = element(doc, "span", `原文 ← → 中文 · ${version}`); label.style.flex = "1";
  const sync = element(doc, "button", "同步：开");
  const improve=element(doc,'button','翻译');
  installButtonStyle(doc);improve.className='research-max-bilingual-button';
  improve.title='检查本机存档，复用已有译文并补充缺漏';
  improve.style.cssText='cursor:pointer;-moz-window-dragging:no-drag;';
  bindSingleClick(improve,()=>{
    // One primary action for every entry point: cancel while running, open or
    // close the pair otherwise; translating a companion redirects to source.
    (Zotero as any).ResearchMaxBilingual?.primaryAction?.(left).catch((e:any)=>win.alert(e.message));
  });
  const coverage=element(doc,'span',coverageText(report));coverage.title=report.pipeline?.startsWith('paddle-cloud')?'按识别清单中的待译词数估算，包含识别到的图表文字；公式与保留的身份信息不计入，不代表识别召回率或语义正确率。':'旧版正文统计，不包含图表内部文字。';
  coverage.style.cssText='font-size:12px;';
  const review = element(doc, "select"); review.setAttribute("aria-label", "待核对区域");
  const issues=reviewIssues(report);
  review.appendChild(element(doc, "option", `待核对 ${issues.length} 处`));
  for (const [i, issue] of issues.entries()) {
    const option = element(doc, "option", `第 ${issue.page} 页 · ${issue.types.map(issueLabel).join(" / ")}${report.reading?'':` · 保留${issue.fallback === "page" ? "整页" : "原段"}`}`);
    option.value = String(i); review.appendChild(option);
  }
  toolbar.style.flexWrap='wrap';toolbar.append(label,coverage,sync,improve,review);
  container.append(toolbar);
  const row = doc.createXULElement("hbox"); row.style.cssText = "display:flex;flex:1;min-height:0;overflow:hidden;";
  const leftBrowser = createReaderBrowser(win), rightBrowser = createReaderBrowser(win);
  const rightColumn=doc.createXULElement('vbox');rightColumn.style.cssText='display:flex;flex-direction:column;flex:1 1 0;min-width:0;min-height:0;overflow:hidden;';
  const divider = doc.createXULElement("box"); divider.style.cssText = "width:6px;min-width:6px;cursor:col-resize;background:#c1c9d2;";
  leftBrowser.style.flex = rightBrowser.style.flex = "1 1 0";
  rightColumn.append(rightBrowser);row.append(leftBrowser, divider, rightColumn); container.appendChild(row);
  const state: any = {tabID:id, win, report,container, mainHbox:row, leftBrowser, rightBrowser, resizer:divider,
    leftAdapter:new ReaderPane(left), rightAdapter:new ReaderPane(right), syncPaused:false, mirrorGuard:false,
    syncScheduled:false, lastLeftFraction:null, lastRightFraction:null, lastLeftHFraction:null,lastRightHFraction:null,
    lastLeftScale:null,lastRightScale:null,lastLeftScaleValue:null,lastRightScaleValue:null,
    syncIntervalId:null,syncRAFId:null,leftTriggerCleanup:null,rightTriggerCleanup:null,
    eventListeners:[],timeoutIds:[],isCleaningUp:false};
  active.set(id,state); stateMap.set(id,state);
  state.translationCleanup=Zotero.ResearchMaxBilingual.watchTranslation(left.id,(value:any)=>{
    improve.textContent=value.label;improve.disabled=value.disabled;
  });
  const trigger = makeSyncTrigger(state);
  const prefs = `extensions.zotero.research-max-bilingual.view.${left.key}.${right.key}.`;
  const saved = (side: string) => { try { return JSON.parse(Zotero.Prefs.get(prefs+side,true) || "null"); } catch { return null; } };
  const menu = (browser: any) => (params: any) => {
    // The native reader stores callbacks on the unwrapped content object.
    params=browser.contentWindow.wrappedJSObject.contextMenuParams||params;
    if (!params) return;
    const popup = doc.createXULElement("menupopup");
    const add = (parent: any, groups: any[]) => {
      for (const group of groups || []) {
        if (parent.children.length) parent.appendChild(doc.createXULElement("menuseparator"));
        for (const item of group) {
          if (item.groups) {
            const sub = doc.createXULElement("menu"); sub.setAttribute("label",item.label);
            const inner = doc.createXULElement("menupopup"); sub.appendChild(inner); add(inner,item.groups); parent.appendChild(sub);
          } else {
            const node = doc.createXULElement("menuitem"); node.setAttribute("label", item.label || "");
            if (item.disabled) node.setAttribute("disabled","true");
            if (item.checked !== undefined) {node.setAttribute("type","checkbox");node.setAttribute("checked",String(item.checked));}
            node.addEventListener("command", () => item.onCommand?.()); parent.appendChild(node);
          }
        }
      }
    };
    add(popup,params.itemGroups || params.groups || []);
    doc.documentElement.appendChild(popup);
    popup.addEventListener("popuphidden",()=>popup.remove(),{once:true});
    const bounds=browser.getBoundingClientRect();
    const screen=win.windowUtils.toScreenRectInCSSUnits(bounds.x+params.x,bounds.y+params.y,0,0);
    win.setTimeout(()=>{if(popup.isConnected)popup.openPopupAtScreen(screen.x,screen.y,true);},0);
  };
    await Promise.all((["left","right"] as const).map(side => state[side+"Adapter"].attach(state[side+"Browser"],{
      tabID:id,side,initialState:saved(side),onOpenContextMenu:menu(state[side+"Browser"]),
      onChangeViewState:(v:any)=>{if(v) Zotero.Prefs.set(prefs+side,JSON.stringify(v),true);trigger();}
    })));
    const ready = await Promise.all([state.leftAdapter.waitForScrollContainer(),state.rightAdapter.waitForScrollContainer()]);
    if (ready.some(v=>!v)) throw new Error("PDF 阅读器加载超时");
    installSync(state);
  let enabled=true;
  sync.addEventListener("click",()=>{enabled=!enabled;state.syncPaused=!enabled;sync.textContent=`同步：${enabled?"开":"关"}`;primeSyncBaselines(state);});
  const drag=(event:any)=>{const box=row.getBoundingClientRect();const width=Math.max(200,Math.min(box.width-206,event.clientX-box.left));
    leftBrowser.style.flexGrow=String(width);rightColumn.style.flexGrow=String(box.width-width-6);};
  let resizeAnchor:any=null;
  const up=()=>{win.removeEventListener("mousemove",drag);win.removeEventListener("mouseup",up);state.dragging=false;
    if(resizeAnchor&&!state.syncPaused&&!state.isCleaningUp){state.leftAdapter.setPageAnchor(resizeAnchor);state.rightAdapter.setPageAnchor(resizeAnchor);}
    resizeAnchor=null;primeSyncBaselines(state);};
  divider.addEventListener("mousedown",(e:any)=>{if(e.button!==0)return;e.preventDefault();resizeAnchor=state.leftAdapter.getPageAnchor();state.dragging=true;win.addEventListener("mousemove",drag);win.addEventListener("mouseup",up);});
  state.dragCleanup=up;
  review.addEventListener("change",async()=>{
    if(review.selectedIndex<1)return;
    const issue=issues[Number(review.value)];
    const position={pageIndex:issue.page-1,scale:state.leftAdapter.getPreciseState()?.scale||1,top:issue.region?.[1]||0,left:0};
    await Promise.all([state.leftAdapter.applyPreciseState(position),state.rightAdapter.applyPreciseState(position)]);
  });
  const observer=new win.MutationObserver(()=>{if(!container.isConnected)closePair(id);});
  observer.observe(container.parentNode,{childList:true});state.observer=observer;
  try {
    await IOUtils.writeJSON(PathUtils.join(Zotero.Profile.dir,"research-max-bilingual-reader.json"),{
      checkedAt:new Date().toISOString(),pluginVersion:version,zoteroVersion:Zotero.version,
      leftReady:state.leftAdapter.ready,rightReady:state.rightAdapter.ready,
      widths:[leftBrowser.getBoundingClientRect().width,rightBrowser.getBoundingClientRect().width],
      scrollContainers:[state.leftAdapter.getScrollContainer()?.id,state.rightAdapter.getScrollContainer()?.id],
      attachmentKeys:[left.key,right.key],warnings:report.issues?.length||0
    });
  } catch(error) {Zotero.logError(error);}
  return state;
  } catch (error) {
    // This covers toolbar/report construction too, before `active` is populated.
    // A failed attempt must never leave a selected, permanently empty reader tab.
    suppressPairClose(id);
    try {closePair(id);} catch(cleanupError) {Zotero.logError(cleanupError);}
    try {win.Zotero_Tabs.close(id);} catch(cleanupError) {Zotero.logError(cleanupError);}
    throw error;
  }
}
export function closePair(id: string) {
  const s=active.get(id);if(!s)return;
  s.isCleaningUp=true;active.delete(id);stateMap.delete(id);
  const clean=(fn:()=>void)=>{try{fn();}catch(error){Zotero.logError(error);}};
  clean(()=>s.observer?.disconnect());clean(()=>s.dragCleanup?.());clean(()=>s.translationCleanup?.());
  if(s.syncIntervalId!=null)clean(()=>s.win.clearInterval(s.syncIntervalId));
  if(s.syncRAFId!=null)clean(()=>s.win.cancelAnimationFrame(s.syncRAFId));
  clean(()=>s.leftTriggerCleanup?.());clean(()=>s.rightTriggerCleanup?.());
  for(const e of s.eventListeners)clean(()=>e.target.removeEventListener(e.type,e.listener,e.options));
  for(const t of s.timeoutIds)clean(()=>s.win.clearTimeout(t));
  clean(()=>s.leftAdapter.destroy());clean(()=>s.rightAdapter.destroy());
}
export async function closeAll() { for(const [id,s] of active){suppressPairClose(id);closePair(id);s.win.Zotero_Tabs.close(id);} await flushAnnotationWrites(); }
export async function closeForUpdate(itemID:number) {
  const matching=[...active].filter(([id,s])=>s.rightAdapter.item.id===itemID);
  try {for(const [id,s] of matching){await s.leftAdapter.flushForUpdate();await s.rightAdapter.flushForUpdate();}}
  catch(error){for(const [id,s] of matching){s.leftAdapter.resumeAfterUpdate();s.rightAdapter.resumeAfterUpdate();}throw error;}
  for(const [id,s] of matching){suppressPairClose(id);closePair(id);s.win.Zotero_Tabs.close(id);}
  await flushAnnotationWrites();
  for(const reader of [...(Zotero.Reader._readers||[])]) {
    if(reader.itemID!==itemID)continue;
    const win=reader._iframe?.contentWindow;
    const internal=(win?.wrappedJSObject||win)?._reader;
    const manager=internal?._annotationManager;
    if(!manager)throw new Error('中文附件正在加载，请稍后重试更新');
    if(manager._readOnly||internal._state?.readOnly)throw new Error('中文附件处于只读或批注保存失败状态，未更新文件');
    const pendingDeletes=[...manager._unsavedAnnotations].filter(([id,value]:any)=>!value).map(([id]:any)=>id);
    manager._skipAnnotationSavingDebounce=true;
    try {
      for(let i=0;i<100&&(manager._unsavedAnnotations.size||manager._savingInProgress);i++){
        await manager._triggerSaving();await Zotero.Promise.delay(50);
      }
      if(manager._unsavedAnnotations.size||manager._savingInProgress)throw new Error('批注尚未保存，请稍后重试更新');
      if(manager._readOnly||internal._state?.readOnly)throw new Error('批注保存失败，未更新中文附件');
      const owner=Zotero.Items.get(itemID);
      // Native save callbacks can finish even after the host reports an error.
      // Confirm visible annotations reached the DB before closing that window.
      for(const a of manager._annotations||[]) {
        if(a.isExternal)continue;
        const saved=Zotero.Items.getByLibraryAndKey(owner.libraryID,a.id);
        if(!saved||saved.deleted||saved.parentID!==itemID||String(saved.annotationComment||'')!==String(a.comment||'')||
          String(saved.annotationText||'')!==String(a.text||''))throw new Error('批注尚未完整写入文库，未更新中文附件');
      }
      if(pendingDeletes.some((key:string)=>{const a=Zotero.Items.getByLibraryAndKey(owner.libraryID,key);return a&&!a.deleted;}))
        throw new Error('批注删除尚未保存，未更新中文附件');
      reader.close();
    } finally {manager._skipAnnotationSavingDebounce=false;}
  }
}
