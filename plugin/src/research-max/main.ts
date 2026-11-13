import {api, locate, download, stopService} from "./service";
import {openPair, closeAll, closeForUpdate, activePairs, closePairAndRestore} from "./reader";
import {replaceAttachment} from './attachmentUpdate';
import {findArchiveRecord} from "./archive";
import {createPairState} from "./pairState";
import {TaskControls, taskButtonState, type Task} from "./taskControls";
import {ReaderToolbar} from "./toolbar";
import {TranslationNotices, closeLegacyCancelledNotices, type Outcome} from "./progress";
import {version} from "../../research-max/manifest.json";
import {openSettingsWindow,SETTINGS_NAME} from './settingsWindow';
const ID="zotero-bilingual@research-max.local";
const windows=new Set<any>();
let toolbar:ReaderToolbar|undefined;
let notices:TranslationNotices|undefined;
const stateListeners=new Set<()=>void>();
const controls=new TaskControls(()=>{toolbar?.refresh();for(const fn of stateListeners)fn();},job=>api(`/api/tasks/${job}/cancel`,{}));
// Hash with a stat stamp so the 1s sweep and view lookups never re-read big
// PDFs unless the file actually changed on disk.
const hashCache=new Map<string,{stamp:string;hash:string}>();
async function statHash(path:string|null|undefined){
  if(!path)return null;
  let stamp='';
  try{const stat=await IOUtils.stat(path);stamp=`${stat.lastModified}:${stat.size}`;}catch{return null;}
  const cached=hashCache.get(path);
  if(cached&&cached.stamp===stamp)return cached.hash;
  const hash=fileHash(path);hashCache.set(path,{stamp,hash});return hash;
}
const pair=createPairState({
  maps,remember,
  sourceHash:async(item:any)=>statHash(await item.getFilePathAsync()),
  attachmentHash:async(item:any)=>statHash(await item.getFilePathAsync()),
  epoch:()=>Zotero.Prefs?.get("extensions.zotero.research-max-bilingual.configEpoch",true)||0,
  items:{get:(id:number)=>Zotero.Items?.get?.(id),getByLibraryAndKey:(l:number,k:string)=>Zotero.Items?.getByLibraryAndKey?.(l,k)},
  tasksHas:(id:number)=>controls.tasks.has(id),
  openPair,activePairs,
  closeReader:(reader:any)=>{try{reader.close();}catch(error){Zotero.logError?.(error);}},
  mainWindow:()=>Zotero.getMainWindow?.(),
  logError:(e:any)=>Zotero.logError?.(e),
  onViewChanged:()=>{toolbar?.refresh();for(const fn of stateListeners)fn();},
});
function viewState(itemID:number){
  const view=pair.cachedView(itemID);
  return view?{available:true,open:pair.isOpen(itemID),stale:view.stale}:undefined;
}
function updateButton(button:any,itemID:number,reader?:any){
  const state=taskButtonState(controls.tasks.get(itemID),viewState(itemID));
  button.textContent=state.label;button.disabled=state.disabled;button.title=state.title;
  button.setAttribute('aria-busy',String(controls.tasks.has(itemID)));
  if(reader)pair.maybePair(reader);
  if(!controls.tasks.has(itemID))pair.ensureView(itemID);
}
let archiveWrites:Promise<any>=Promise.resolve();
let stopping=false;
let batchCancelled=false;
let settingsWindow:any;
const HTML="http://www.w3.org/1999/xhtml";
function fail(error:any){if(!stopping)Zotero.getMainWindow().alert("中文对照："+(error?.message||String(error)));}
const run=(fn:()=>Promise<any>)=>{fn().catch(fail);};
function fileHash(path:string){
  const input=Components.classes["@mozilla.org/network/file-input-stream;1"].createInstance(Components.interfaces.nsIFileInputStream);
  input.init(Zotero.File.pathToFile(path),1,0,0);
  try{const hash=Components.classes["@mozilla.org/security/hash;1"].createInstance(Components.interfaces.nsICryptoHash);
    hash.init(hash.SHA256);hash.updateFromStream(input,0xffffffff);
    return Array.from(hash.finish(false),(c:any)=>c.charCodeAt(0).toString(16).padStart(2,"0")).join("");
  }finally{input.close();}
}
async function maps(){const p=PathUtils.join(Zotero.Profile.dir,"research-max-bilingual.json");let data:any={};try{data=await IOUtils.readJSON(p);}catch{}return{p,data};}
function remember(key:string,record:any){
  const operation=archiveWrites.then(async()=>{const fresh=await maps();fresh.data[key]=record;await IOUtils.writeJSON(fresh.p,fresh.data,{tmpPath:fresh.p+'.tmp',flush:true});});
  archiveWrites=operation.catch(()=>{});return operation;
}
async function pdfs(items:any[]){const out=[];for(const item of items){if(item.isPDFAttachment?.())out.push(item);else{const best=await item.getBestAttachment?.();if(best?.isPDFAttachment())out.push(best);}}return out;}
async function translate(item:any,force=false){
  if(!item?.isPDFAttachment?.())throw new Error("请选择 PDF 附件");
  return controls.start(item.id,task=>{
    // Clear the previous terminal notice even when the next click hits archive
    // or fails before a new translation job can be submitted.
    notices?.close(item.id);return translateItem(item,task,force);
  });
}
async function translateItem(item:any,task:Task,force=false){
  // Offline re-opening uses locally imported attachments. Settings updates clear
  // only the lookup fingerprint; existing PDFs and annotations remain intact.
  const key=`${item.libraryID}:${item.key}`;
  const journalPath=PathUtils.join(Zotero.Profile.dir,`research-max-bilingual-update-${item.libraryID}-${item.key}.json`);
  if(await IOUtils.exists(journalPath)) {
    const journal=await IOUtils.readJSON(journalPath),current=fileHash(journal.path);
    if(current===journal.newHash)await remember(key,journal.record);
    else if(current!==journal.oldHash)throw new Error('上次更新尚未恢复，译文文件已在外部改变');
    await IOUtils.remove(journalPath,{ignoreAbsent:true});
  }
  const saved=await maps();
  const sourcePath=await item.getFilePathAsync();
  if(!sourcePath)throw new Error("PDF 尚未下载到本机");
  const hash=fileHash(sourcePath);
  const epoch=Zotero.Prefs.get("extensions.zotero.research-max-bilingual.configEpoch",true)||0;
  // View lookup is epoch-free: a settings change never hides an existing
  // translation, and never triggers a surprise full re-translation.
  const view=await pair.viewableFor(item);
  const record=view?.record??null;
  if(task.cancelRequested||stopping)return;
  // Local reading remains immediate even when the later repair needs a model
  // and that provider is offline or out of quota. This is an existing complete
  // attachment, not delivery of the in-progress repair.
  if(record)await openPair(Zotero.getMainWindow(),item,Zotero.Items.getByLibraryAndKey(item.libraryID,record.attachmentKey),record.report);
  if(record&&view?.stale&&!force){
    if(task.cancelRequested||stopping)return;
    pair.invalidate(item.id);
    const notice=notices!.show(item.id,'中文对照');
    notice.finish('completed','已打开现有译文。当前翻译设置已变更，如需按新设置重译，请从右键菜单选择“更新译文”。');
    return;
  }
  if(task.cancelRequested||stopping)return;
  let config;
  try{config=await api("/api/config");}
  catch(error){
    if(!record)throw error;
    if(task.cancelRequested||stopping)return;
    const attachment=Zotero.Items.getByLibraryAndKey(item.libraryID,record.attachmentKey);
    if(controls.deliver(task))await openPair(Zotero.getMainWindow(),item,attachment,record.report);
    const notice=notices!.show(item.id,'中文对照');notice.finish('failed','本机检查暂不可用，已打开现有存档；此次未执行查漏补缺。');return;
  }
  if(task.cancelRequested||stopping)return;
  // The service checks its permanent archive before requiring an API key.
  const progress=notices!.show(item.id,item.parentItem?.getField("title")||item.getField("title"));
  progress.update(0,'检查本机存档并查找缺漏…');
  let job;try{job=await api("/translate",{sourcePath,sourceKey:key,automatic:true});}
  catch(error:any){progress.finish('failed',error.message||'本次检查未完成');if(!config.hasApiKey||!config.hasPaddleApiKey)await settings();throw error;}
  try{await controls.bindJob(task,job.taskId);}
  catch(error:any){progress.finish('failed',error.message||'任务状态更新失败');throw error;}
  if(task.cancelRequested||stopping){progress.finish('cancelled','已取消，进度已保留');return;}
  let outcome:Outcome='failed',message='翻译未完成，请重试';
  try{
    let state=job;
    while(state.active&&!stopping){progress.update(state.progress||0,state.message||"处理中");await Zotero.Promise.delay(1000);state=await api(`/api/tasks/${job.taskId}`);}
    if(stopping||task.cancelRequested){outcome='cancelled';message='已取消，进度已保留；再次点击翻译即可继续';return;}
    if(state.status==="skipped"||state.status==="cancelled"){outcome=state.status;message=state.message;return;}
    if(!["completed","completed_with_warnings"].includes(state.status))throw new Error((record?'已有译文仍可阅读，本次查漏补缺未完成：':'')+state.message);
    const report=await api(`/api/tasks/${job.taskId}/report`);
    const file=await download(job.taskId);
    const actual=fileHash(file);
    if(actual!==report.outputHash||hash!==report.sourceHash)throw new Error("译文校验失败，未导入附件");
    if(stopping||!controls.deliver(task)){outcome='cancelled';message='已取消，进度已保留；再次点击翻译即可继续';return;}
    // A new audit may leave the PDF bytes unchanged. Reuse that
    // attachment and its annotations instead of importing a duplicate PDF.
    const reusable=await findArchiveRecord(saved.data,key,hash,epoch,async(value:any)=>{
      if(value.report.outputHash!==report.outputHash || (value.libraryID!==undefined&&value.libraryID!==item.libraryID))return false;
      const candidate=Zotero.Items.getByLibraryAndKey(item.libraryID,value.attachmentKey);
      return candidate&&!candidate.deleted&&await candidate.fileExists()&&fileHash(await candidate.getFilePathAsync())===report.outputHash;
    });
    const owned=saved.data[key];
    const existing=owned?.sourceHash===hash&&owned.attachmentKey!==item.key?
      Zotero.Items.getByLibraryAndKey(item.libraryID,owned.attachmentKey):null;
    const newMapping=(a:any)=>({sourceHash:hash,configFingerprint:report.configFingerprint,
      attachmentKey:a.key,libraryID:item.libraryID,epoch,report,pairState:'enabled'});
    const saveMapping=(a:any)=>remember(key,newMapping(a));
    let recorded=false;
    const attachment=reusable?Zotero.Items.getByLibraryAndKey(item.libraryID,reusable.attachmentKey):
      existing&&!existing.deleted&&await existing.fileExists()?
      await replaceAttachment(existing,file,{...owned,sourceKey:key},report,job.taskId,{hash:fileHash,api,close:closeForUpdate,
        journal:{path:journalPath,record:newMapping(existing)},
        commit:async()=>{await saveMapping(existing);recorded=true;}}):
      await Zotero.Attachments.importFromFile({file,parentItemID:item.parentItemID||undefined,libraryID:item.libraryID,
      title:`中文对照${report.issues.length?"（待核对）":""}`});
    if(fileHash(await attachment.getFilePathAsync())!==report.outputHash)throw new Error("导入后的译文校验失败，本机存档仍保留");
    if(!recorded)await saveMapping(attachment);
    await api(`/api/tasks/${job.taskId}/imported`,{attachmentKey:attachment.key,libraryID:item.libraryID,sourceKey:key,outputHash:report.outputHash,deliveryId:report.deliveryId});
    const pairTab=await openPair(Zotero.getMainWindow(),item,attachment,report);
    await focusPairAndClosePlain(item.id,pairTab);
    pair.invalidate(item.id);await pair.viewNow(item.id);
    await trashSuperseded(item,attachment,saved.data);
    outcome='completed';message=report.message||state.message||'中文对照已打开';
  }catch(error:any){message=error?.message||String(error);throw error;}
  finally{if(stopping)progress.close();else progress.finish(outcome,message);}
}
async function focusPairAndClosePlain(itemID:number,pairTab:any){
  if(!pairTab?.tabID)return;
  for(const reader of [...(Zotero.Reader._readers||[])]){
    if(reader._type!=='pdf'||reader.itemID!==itemID)continue;
    // Close only positively identified plain-reader tabs, never the pair tab's
    // own hosted reader (matched by tab id).
    const tabID=(reader as any)._tabID??reader.tabID;
    if(tabID&&tabID!==pairTab.tabID){
      try{reader.close();}catch(error:any){Zotero.logError(error);}
    }
  }
}
async function trashSuperseded(source:any,current:any,snapshot:any){
  const kept:string[]=[];
  for(const value of Object.values(snapshot||{})){
    const record=value as any;
    if(!record?.attachmentKey)continue;
    const candidate=Zotero.Items.getByLibraryAndKey(record.libraryID??source.libraryID,record.attachmentKey);
    if(!candidate||candidate.deleted||candidate.id===current.id)continue;
    if(candidate.parentItemID!==source.parentItemID)continue;
    if(!String(candidate.getField('title')||'').startsWith('中文对照'))continue;
    if(candidate.getAnnotations().length){kept.push(String(candidate.getField('title')));continue;}
    try{candidate.deleted=true;await candidate.saveTx();}catch(error:any){Zotero.logError(error);}
  }
  if(kept.length){
    const notice=notices?.show(source.id,'中文对照');
    notice?.finish('completed',`已交付新译文；${kept.length} 个带批注的旧对照附件已保留，请自行决定是否删除。`);
  }
}
// One primary action for every entry point (toolbar, context menu, pair pane).
// Cancel keeps its in-task semantics; a companion attachment redirects to its
// source instead of translating itself recursively.
async function primaryAction(itemRaw:any){
  let item=itemRaw;
  const counterpart=await pair.counterpartOf(item);
  if(counterpart){
    const source=pair.sourceItemFor(counterpart);
    if(!source)throw new Error("找不到该译文对应的原文附件");
    item=source;
  }
  if(controls.tasks.has(item.id))return controls.cancel(item.id);
  const view=await pair.viewNow(item.id);
  if(!view)return translate(item);
  if(pair.isOpen(item.id)){
    const current=pair.activePair(item.id);
    await pair.setPairState(item,'disabled');
    if(current)await closePairAndRestore(current.tabID);
    return;
  }
  await pair.setPairState(item,'enabled');
  const right=Zotero.Items.getByLibraryAndKey(item.libraryID,view.record.attachmentKey);
  if(!right||right.deleted||!await right.fileExists())throw new Error("对照附件不可用，请重新翻译");
  const pairTab=await openPair(Zotero.getMainWindow(),item,right,view.record.report);
  await focusPairAndClosePlain(item.id,pairTab);
}
async function selected(){const list=await pdfs(Zotero.getActiveZoteroPane().getSelectedItems());if(!list.length)throw new Error("请选择一篇或多篇带 PDF 的论文");batchCancelled=false;let next=0;
  const consume=async()=>{while(next<list.length&&!stopping&&!batchCancelled){const item=list[next++];try{await translate(item);}catch(e){Zotero.logError(e);}}};
  await Promise.all([consume(),consume()]);}
async function cancel(){batchCancelled=true;await Promise.all([...controls.tasks.keys()].map(id=>controls.cancel(id)));}
async function settings(){
  if(settingsWindow&&!settingsWindow.closed){settingsWindow.focus();return settingsWindow;}
  settingsWindow=openSettingsWindow(Zotero.getMainWindow(),async(win:any)=>{
      const doc=win.document,body=doc.getElementById("body");body.replaceChildren();
      const make=(tag:string,text="")=>{const el=doc.createElementNS(HTML,tag);el.textContent=text;return el;};
      body.append(make("h2",`中文对照设置 · ${version}`),make("p","版面解析、公式保护与中文排版全部在本机引擎完成，不上传论文；仅将待译文本发送至下方你配置的模型接口，任意 OpenAI 兼容服务均可（GPT、Claude、Kimi、GLM、DeepSeek、Qwen 等）。密钥使用当前 Windows 账户加密保存。"));
      const status=make("p","正在连接本机服务…");status.id="message";body.append(status);
      try{
        let config=await api("/api/config");const inputs:any={};
        for(const [key,label] of [["provider","翻译服务商"],["baseUrl","翻译标准 API 地址"],["model","翻译模型"],["apiKey",`翻译 API 密钥${config.hasApiKey?"（已保存，留空保持）":""}`],["glossary","术语表（每行：英文 = 中文）"]]){
          const holder=make("label",label);const input=make(key==="glossary"?"textarea":key==="provider"?"select":"input");
          if(key==="provider")for(const [value,label] of [["zhipu","智谱标准 API（预设）"],["deepseek","DeepSeek（预设）"],["custom","自定义（任意 OpenAI 兼容服务）"]]){const option=make("option",label);option.value=value;input.append(option);}
          const secret=key==="apiKey";input.value=secret?"":config[key]||"";if(secret){input.type="password";input.autocomplete="off";}
          if(key==="glossary")input.rows=5;inputs[key]=input;holder.append(input);body.append(holder);
        }
        const providerPresets:any={zhipu:{baseUrl:"https://open.bigmodel.cn/api/paas/v4",model:"glm-5.3-flash"},deepseek:{baseUrl:"https://api.deepseek.com",model:"deepseek-chat"}};
        inputs.provider.addEventListener("change",()=>{const preset=providerPresets[inputs.provider.value];
          if(preset){inputs.baseUrl.value=preset.baseUrl;inputs.model.value=preset.model;inputs.apiKey.value="";status.textContent="已套用预设接口与模型，请填入对应 API 密钥。";}
          else status.textContent="自定义模式：API 地址与模型名称保持当前值，可填写任意 OpenAI 兼容服务，能稳定遵循翻译指令的模型即可使用。";});
        const save=make("button","保存"),close=make("button","关闭");body.append(save,close);
        save.addEventListener("click",()=>run(async()=>{const value:any={};for(const key of Object.keys(inputs))value[key]=inputs[key].value;
          if(String(value.baseUrl).replace(/\/+$/,"")!==config.baseUrl&&!value.apiKey)throw new Error("更换接口地址时请填写对应 API 密钥");
          const old=JSON.stringify(config);const updated=await api("/api/config",value);inputs.apiKey.value="";
          if(old!==JSON.stringify(updated)){Zotero.Prefs.set("extensions.zotero.research-max-bilingual.configEpoch",Date.now(),true);pair.invalidate();}
          config=updated;
          status.textContent="已保存。关闭设置后，在 PDF 工具栏点击翻译即可。";}));
        close.addEventListener("click",()=>win.close());status.textContent="本机服务已连接；本地引擎：BabelDOC 0.7.1";
      }catch(e:any){status.textContent=e.message;}
    },e=>Zotero.logError(e));
  return settingsWindow;
}
const context=(event:any)=>{if(event.reader?._type!=="pdf")return;const id=event.reader.itemID;
  event.append({label:taskButtonState(controls.tasks.get(id),viewState(id)).label,
    onCommand:()=>run(()=>primaryAction(Zotero.Items.get(id)))});
  if(!controls.tasks.has(id))event.append({label:"更新译文（按当前设置重译）",onCommand:()=>run(()=>translate(Zotero.Items.get(id),true))});};
export async function addWindow(win:any){if(windows.has(win))return;windows.add(win);for(const [target,label,action] of [["menu_ToolsPopup","中文对照设置",settings],["menu_ToolsPopup","取消中文翻译任务",cancel],["zotero-itemmenu","中文对照（所选论文）",selected]] as const){
  const parent=win.document.getElementById(target);if(!parent)continue;const node=win.document.createXULElement("menuitem");node.className="research-max-bilingual-menu";node.setAttribute("label",label);node.addEventListener("command",()=>run(action));parent.append(node);}}
export async function removeWindow(win:any){for(const el of win.document.querySelectorAll(".research-max-bilingual-menu"))el.remove();windows.delete(win);}
export async function start(){stopping=false;Zotero.addShutdownListener(async()=>{notices?.closeAll();if(!stopping)await closeAll();});
  // Close only our previous empty settings window left behind by a hot update.
  for(const win of Services.wm.getEnumerator(null))if(win.name===SETTINGS_NAME)win.close();
  const timers=ChromeUtils.importESModule('resource://gre/modules/Timer.sys.mjs');
  closeLegacyCancelledNotices(Services.wm.getEnumerator(null),win=>Zotero.ProgressWindowSet.remove(win),e=>Zotero.logError(e));
  notices=new TranslationNotices(()=>new Zotero.ProgressWindow({closeOnClick:true}),timers,e=>Zotero.logError(e));
  toolbar=new ReaderToolbar({readers:()=>Zotero.Reader._readers||[],
    register:handler=>Zotero.Reader.registerEventListener('renderToolbar',handler,ID),
    unregister:handler=>Zotero.Reader.unregisterEventListener('renderToolbar',handler),
    repeat:(fn,ms)=>timers.setInterval(fn,ms),clear:id=>timers.clearInterval(id),error:e=>Zotero.logError(e)},
    id=>run(()=>primaryAction(Zotero.Items.get(id))),updateButton);
  toolbar.start();
  Zotero.Reader.registerEventListener("createViewContextMenu",context,ID);for(const win of Zotero.getMainWindows())await addWindow(win);
  Zotero.ResearchMaxBilingual={translate,settings,selected,cancel,openPair,isTranslating:(id:number)=>controls.tasks.has(id),cancelItem:(id:number)=>controls.cancel(id),
    primaryAction,
    onPairUserClosed:(leftID:number)=>{const item=Zotero.Items.get(leftID);
      if(item)pair.setPairState(item,'disabled').catch((e:any)=>Zotero.logError(e));},
    watchTranslation:(id:number,callback:any)=>{const fn=()=>callback(taskButtonState(controls.tasks.get(id),viewState(id)));stateListeners.add(fn);fn();return()=>stateListeners.delete(fn);}};}
export async function stop(){stopping=true;if(settingsWindow&&!settingsWindow.closed)settingsWindow.close();settingsWindow=undefined;notices?.closeAll();notices=undefined;toolbar?.stop();toolbar=undefined;Zotero.Reader.unregisterEventListener("createViewContextMenu",context);await closeAll();for(const win of windows)await removeWindow(win);await stopService();delete Zotero.ResearchMaxBilingual;}
