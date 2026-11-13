// Pair presentation state: which sources have a readable translation, whether
// the bilingual pair should follow the source reader automatically, and the
// guarded auto-pairing used when Zotero (re)opens a paper. Zotero cannot morph
// an existing reader tab in place, so "in place" is implemented as: open the
// pair tab, then close the plain reader that triggered it.
import {findViewableRecord} from './archive';

export type ViewInfo={record:any; stale:boolean};
export type ActivePair={leftID:number; tabID:string; select:()=>void};
export type PairDeps={
  maps:()=>Promise<{p:string; data:any}>;
  remember:(key:string, record:any)=>Promise<any>;
  sourceHash:(item:any)=>Promise<string|null>;      // stat-cached by caller
  attachmentHash:(item:any)=>Promise<string|null>;  // stat-cached by caller
  epoch:()=>number;
  items:{get:(id:number)=>any; getByLibraryAndKey:(libraryID:number, key:string)=>any};
  tasksHas:(itemID:number)=>boolean;
  openPair:(win:any, left:any, right:any, report:any)=>Promise<any>;
  activePairs:()=>ActivePair[];
  closeReader:(reader:any)=>void;
  mainWindow:()=>any;
  logError:(error:any)=>void;
  onViewChanged:(itemID:number)=>void;
};

export function createPairState(deps:PairDeps){
  const inFlight=new Set<number>();
  const failed=new Set<number>();
  const viewCache=new Map<number,ViewInfo|null>();
  const pending=new Map<number,Promise<ViewInfo|null>>();

  async function viewableFor(item:any):Promise<ViewInfo|null>{
    if(!item?.isPDFAttachment?.())return null;
    const key=`${item.libraryID}:${item.key}`;
    const hash=await deps.sourceHash(item);
    if(!hash)return null;
    const {data}=await deps.maps();
    const record=await findViewableRecord(data,key,hash,async(value:any)=>{
      const attachment=deps.items.getByLibraryAndKey(item.libraryID,value.attachmentKey);
      if(!attachment||attachment.deleted||!await attachment.fileExists?.())return false;
      return await deps.attachmentHash(attachment)===value.report.outputHash;
    });
    if(!record)return null;
    return {record,stale:record.epoch!==deps.epoch()};
  }

  async function refreshView(itemID:number):Promise<ViewInfo|null>{
    try{
      const view=await viewableFor(deps.items.get(itemID));
      viewCache.set(itemID,view);
      deps.onViewChanged(itemID);
      return view;
    }catch(error){deps.logError(error);return null;}
  }

  // Fire-and-forget cache warm-up; converges (a refresh never schedules itself).
  function ensureView(itemID:number){
    if(viewCache.has(itemID)||pending.has(itemID))return;
    const operation=refreshView(itemID).finally(()=>pending.delete(itemID));
    pending.set(itemID,operation);
  }

  async function viewNow(itemID:number):Promise<ViewInfo|null>{
    // Join an in-flight refresh so callers observe a deterministic result
    // instead of racing the background warm-up.
    const warming=pending.get(itemID);
    if(warming)return warming;
    const cached=viewCache.get(itemID);
    if(cached!==undefined)return cached;
    return refreshView(itemID);
  }

  function cachedView(itemID:number):ViewInfo|null|undefined{return viewCache.get(itemID);}

  function invalidate(itemID?:number){
    if(itemID!==undefined){viewCache.delete(itemID);failed.delete(itemID);ensureView(itemID);}
    else{viewCache.clear();failed.clear();}
  }

  // A companion attachment must never be translated again as a source: return
  // the mapping record whose attachmentKey matches, so callers can redirect to
  // the real source attachment instead of recursing.
  async function counterpartOf(item:any):Promise<{record:any; sourceKey:string}|null>{
    if(!item?.isPDFAttachment?.())return null;
    const {data}=await deps.maps();
    for(const [sourceKey,value] of Object.entries(data) as any[]){
      if(value?.attachmentKey===item.key&&value?.report?.outputHash)return {record:value,sourceKey};
    }
    return null;
  }

  function sourceItemFor(counterpart:{record:any; sourceKey:string}):any{
    const [libraryID,key]=counterpart.sourceKey.split(':');
    return deps.items.getByLibraryAndKey(Number(libraryID),key);
  }

  function isOpen(itemID:number):boolean{return deps.activePairs().some(p=>p.leftID===itemID);}
  function activePair(itemID:number):ActivePair|undefined{return deps.activePairs().find(p=>p.leftID===itemID);}

  async function setPairState(item:any,state:'enabled'|'disabled'){
    if(!item?.isPDFAttachment?.())return;
    const key=`${item.libraryID}:${item.key}`;
    const {data}=await deps.maps();
    const current=data[key];
    if(!current||current.pairState===state)return;
    await deps.remember(key,{...current,pairState:state});
    invalidate(item.id);
  }

  // Guard chain, in order: reentrancy, companion attachment, already-paired
  // (also breaks the loop for a native reader inside the pair tab), task in
  // flight, no viewable translation (session-scoped, no retry storm), and the
  // user's explicit opt-out. Only then does a plain reader become a pair.
  async function maybePair(reader:any){
    try{
      if(!reader||reader._type!=='pdf')return;
      const itemID=reader.itemID;
      if(inFlight.has(itemID)||failed.has(itemID)||deps.tasksHas(itemID))return;
      const item=deps.items.get(itemID);
      if(!item?.isPDFAttachment?.())return;
      if(await counterpartOf(item))return;
      const existing=activePair(itemID);
      if(existing){
        existing.select();
        // Only close a plain reader we can positively identify as a different
        // tab; never the reader hosted inside the pair tab itself.
        if(reader._tabID&&reader._tabID!==existing.tabID)deps.closeReader(reader);
        return;
      }
      const view=await viewNow(itemID);
      if(!view){failed.add(itemID);return;}
      if(view.record.pairState==='disabled')return;
      inFlight.add(itemID);
      try{
        const right=deps.items.getByLibraryAndKey(item.libraryID,view.record.attachmentKey);
        if(!right||right.deleted||!await right.fileExists?.())throw new Error('对照附件不可用，请重新翻译');
        const state=await deps.openPair(deps.mainWindow(),item,right,view.record.report);
        if(state&&reader._tabID&&reader._tabID!==state.tabID)deps.closeReader(reader);
      }finally{inFlight.delete(itemID);}
    }catch(error){deps.logError(error);}
  }

  return {viewableFor,ensureView,viewNow,cachedView,invalidate,counterpartOf,sourceItemFor,
    isOpen,activePair,setPairState,maybePair};
}
