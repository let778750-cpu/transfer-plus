import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import * as esbuild from 'esbuild';
const root=path.resolve(import.meta.dirname,'..');
test('Zotero requires an HTTPS update field even for manual releases',()=>{
  const manifest=JSON.parse(fs.readFileSync(path.join(root,'plugin/research-max/manifest.json'),'utf8'));
  const application=manifest.applications.zotero;
  for(const name of ['id','update_url','strict_max_version'])assert.ok(application[name]);
  assert.equal(application.id,'zotero-bilingual@research-max.local');
  const url=new URL(application.update_url);assert.equal(url.protocol,'https:');
  assert.equal(url.hostname,'research-max.invalid');
});

test('annotation writes snapshot values and serialize pending saves',async()=>{
  const saved=[];let release;
  const gate=new Promise(resolve=>{release=resolve;});
  globalThis.Zotero={locale:'en-US',File:{getContentsFromURL:()=>''},Prefs:{get:()=>14},API:{getLibraryPrefix:()=> 'library'},
    Items:{getByLibraryAndKey:()=>null},Annotations:{saveFromJSON:async(item,value)=>{if(saved.length===0){saved.push(value.comment);await gate;}else saved.push(value.comment);return{};}},
    logError:()=>{},getMainWindow:()=>({alert:()=>{}})};
  const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/core/pdf/splitview/readerConfig.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
  const module=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
  const config=await module.buildReaderConfig({id:7,key:'ABCDEFGH',libraryID:1,getAnnotations:()=>[],isEditable:()=>true});
  const first=config.onSaveAnnotations([{id:'AAAABBBB',comment:'first'}],()=>{});
  const value={id:'CCCCDDDD',comment:'second'};const second=config.onSaveAnnotations([value],()=>{});
  value.comment='mutated after frame close';
  await new Promise(resolve=>setTimeout(resolve,10));assert.deepEqual(saved,['first']);
  release();await Promise.all([first,second,module.flushAnnotationWrites()]);assert.deepEqual(saved,['first','second']);
});

async function archiveModule(){
  const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/research-max/archive.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
  return import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
}

test('old archived review text never exposes engine tokens as readable formulas',async()=>{
  const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/research-max/reviewText.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
  const {readableReference,issueLabel}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
  assert.equal(readableReference("Yi Di{v1}<style id='2'>2</style>"),'本区域保留原文，没有可用的中文参考文本。');
  assert.equal(readableReference("中文<style id='2'>说明</style>{v1}"),'中文说明〔公式或标记见原文〕');
  assert.equal(issueLabel('font_below_minimum'),'译文字号过小');
  assert.equal(issueLabel('unknown_internal_type'),'需要人工核对');
});
test('same PDF under a new attachment key reuses a verified local revision',async()=>{
  const {findArchiveRecord}=await archiveModule();
  const record={sourceHash:'same-pdf',epoch:7,attachmentKey:'TRAN0001',report:{outputHash:'verified'}};
  const data={'1:OLDKEY01':record};
  assert.equal(await findArchiveRecord(data,'1:NEWKEY02','same-pdf',7,async()=>true),record);
  assert.equal(await findArchiveRecord(data,'1:NEWKEY02','changed-pdf',7,async()=>true),null);
  assert.equal(await findArchiveRecord(data,'1:NEWKEY02','same-pdf',8,async()=>true),null);
  assert.equal(await findArchiveRecord(data,'1:NEWKEY02','same-pdf',7,async()=>false),null);
});
test('zoom settling cannot re-enable a user-disabled sync mode',async()=>{
  const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/core/pdf/splitview/splitViewSync.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
  const {pauseSyncForZoomSettle,doSyncCheck}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
  const timers=[];let reads=0;
  const adapter={getScrollFraction:()=>{reads++;return .2},getScale:()=>1};
  const state={win:{setTimeout:fn=>{timers.push(fn);return timers.length}},timeoutIds:[],syncPaused:false,leftAdapter:adapter,rightAdapter:adapter};
  pauseSyncForZoomSettle(state);state.syncPaused=true;
  timers[0]();assert.equal(state.syncPaused,true);assert.equal(state.zoomSettling,false);
  reads=0;doSyncCheck(state);assert.equal(reads,0);
  state.syncPaused=false;state.dragging=true;doSyncCheck(state);assert.equal(reads,0);
});


async function controlsModule(){
  const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/research-max/taskControls.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
  return import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
}
test('cancel before submission finishes remains latched and blocks attachment delivery',async()=>{
  const {TaskControls,taskButtonState}=await controlsModule();const calls=[];let submitted;
  const wait=new Promise(resolve=>submitted=resolve);let delivered=0;
  const controls=new TaskControls(()=>{},async id=>calls.push(id));
  const pending=controls.start(42,async task=>{await wait;await controls.bindJob(task,'job');if(controls.deliver(task))delivered++;});
  assert.equal(taskButtonState(controls.tasks.get(42)).label,'取消翻译');
  await controls.cancel(42);assert.equal(taskButtonState(controls.tasks.get(42)).label,'正在取消…');
  submitted();await pending;assert.deepEqual(calls,['job']);assert.equal(delivered,0);
  assert.equal(taskButtonState(controls.tasks.get(42)).label,'翻译');
});
test('each paper has its own cancel state and completed delivery cannot be cancelled',async()=>{
  const {TaskControls,taskButtonState}=await controlsModule();const calls=[];let finish;
  const wait=new Promise(resolve=>finish=resolve);const controls=new TaskControls(()=>{},async id=>calls.push(id));
  let second;const one=controls.start(1,async task=>{await controls.bindJob(task,'one');await wait;});
  const two=controls.start(2,async task=>{second=task;await controls.bindJob(task,'two');await wait;});
  await new Promise(resolve=>setTimeout(resolve,0));await controls.cancel(1);
  assert.deepEqual(calls,['one']);assert.equal(second.cancelRequested,false);
  controls.deliver(second);assert.equal(taskButtonState(second).disabled,true);
  await controls.cancel(2);assert.deepEqual(calls,['one']);finish();await Promise.all([one,two]);
});
test('one pointer click runs once; the second click of a double-click cannot cancel it',async()=>{
  const {bindSingleClick,installButtonStyle}=await controlsModule();const handlers={};let actions=0,style;
  const button={disabled:false,addEventListener:(name,fn)=>handlers[name]=fn};
  bindSingleClick(button,()=>actions++);
  const event=detail=>({button:0,detail,preventDefault(){},stopPropagation(){}});
  assert.equal(handlers.pointerdown,undefined);handlers.click(event(1));assert.equal(actions,1);
  handlers.click(event(2));assert.equal(actions,1);
  handlers.click(event(0));assert.equal(actions,2); // keyboard activation
  installButtonStyle({getElementById:()=>null,createElementNS:()=>({}),head:{appendChild:value=>style=value}});
  assert.match(style.textContent,/:hover/);assert.match(style.textContent,/:active/);assert.match(style.textContent,/:focus-visible/);
  assert.match(style.textContent,/-moz-window-dragging:no-drag!important/);
  const previous={textContent:'old style'};
  installButtonStyle({getElementById:()=>previous,head:{appendChild:()=>assert.fail('must update existing style')}});
  assert.match(previous.textContent,/-moz-window-dragging:no-drag!important/);
});

test('old runtime is rejected before spawning; a new endpoint must advertise resume support',async()=>{
  const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/research-max/service.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
  const dataURL='data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64');
  let activeVersion='0.1.6',started=false,healthCalls=0,capabilities=[];
  globalThis.PathUtils={join:(...parts)=>parts.join('/'),parent:value=>value.slice(0,value.lastIndexOf('/'))};
  globalThis.Services={env:{get:()=>'/local'},appinfo:{processID:5}};
  globalThis.IOUtils={makeDirectory:async()=>{},readJSON:async file=>{
    if(file.endsWith('install.json'))return {root:'/runtime',data:'/runtime/data'};
    if(file.endsWith('active.json'))return {version:activeVersion};
    if(!started)throw new Error('not started');return {url:'http://127.0.0.1:1234',token:'test-token',pid:17};
  }};
  globalThis.Components={interfaces:{},classes:{'@mozilla.org/process/util;1':{createInstance:()=>({pid:17,isRunning:true,init(){},runwAsync(){started=true}})}}};
  globalThis.Zotero={Prefs:{get:()=>''},File:{pathToFile:value=>value},Promise:{delay:async()=>{}},HTTP:{request:async()=>{healthCalls++;return {response:{version:'0.4.0',capabilities}}}}};
  const service=await import(dataURL+'#runtime-check');
  await assert.rejects(service.ensureService(),/版本不匹配/);assert.equal(started,false);
  activeVersion='0.4.0';await assert.rejects(service.ensureService(),/版本不匹配/);assert.equal(healthCalls,1);
  capabilities=['resume-checkpoint-v1'];const endpoint=await service.ensureService();assert.equal(endpoint.pid,17);
});

test('physical page anchors survive unequal zoom, fixed gaps and mixed page sizes',async()=>{
  const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/research-max/pageAnchor.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
  const {readPageAnchor,writePageAnchor}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
  const pane=(heights)=>{
    const offsets=[];let height=8;for(const h of heights){offsets.push(height);height+=h+12;}
    const container={scrollTop:0,scrollHeight:height,clientHeight:240,clientTop:2,getBoundingClientRect:()=>({top:100})};
    const viewer={pagesCount:heights.length,getPageView:i=>({div:{getBoundingClientRect:()=>({top:102+offsets[i]-container.scrollTop,bottom:102+offsets[i]-container.scrollTop+heights[i],height:heights[i]})}})};
    return {container,viewer,offsets};
  };
  const left=pane([1000,1200,1000]),right=pane([500,600,500]);
  left.container.scrollTop=left.offsets[1]+.4*1200;
  const anchor=readPageAnchor(left.viewer,left.container);assert.deepEqual(anchor,{pageIndex:1,fraction:.4});
  assert.equal(writePageAnchor(right.viewer,right.container,anchor),true);
  assert.deepEqual(readPageAnchor(right.viewer,right.container),anchor);
  assert.notEqual(left.container.scrollTop/(left.container.scrollHeight-240),right.container.scrollTop/(right.container.scrollHeight-240));
  right.container.scrollTop=right.offsets[2]+30;
  assert.equal(writePageAnchor(left.viewer,left.container,readPageAnchor(right.viewer,right.container)),true);
  assert.deepEqual(readPageAnchor(left.viewer,left.container),{pageIndex:2,fraction:.06});
  left.container.scrollTop=left.offsets[1]-5;
  assert.deepEqual(readPageAnchor(left.viewer,left.container),{pageIndex:1,fraction:0});
  assert.equal(writePageAnchor(right.viewer,right.container,{pageIndex:3,fraction:0}),false);
  assert.equal(writePageAnchor(right.viewer,right.container,{pageIndex:0,fraction:NaN}),false);
});

test('sync mirrors physical anchors and does not echo unequal document fractions',async()=>{
  const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/core/pdf/splitview/splitViewSync.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
  const {doSyncCheck,primeSyncBaselines}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
  let lf=.1,rf=.1,applied=0,fallbacks=0;const timers=[];const anchor={pageIndex:2,fraction:.3};
  const left={getScrollFraction:()=>lf,getPageAnchor:()=>anchor,getScale:()=>1};
  const right={getScrollFraction:()=>rf,getScale:()=>1,setPageAnchor:a=>{assert.equal(a,anchor);applied++;rf=.29;return true;},setScrollFraction:()=>fallbacks++};
  const state={win:{setTimeout:f=>{timers.push(f);return timers.length;}},timeoutIds:[],leftAdapter:left,rightAdapter:right};
  primeSyncBaselines(state);lf=.32;doSyncCheck(state);
  assert.equal(applied,1);assert.equal(fallbacks,0);assert.equal(state.lastRightFraction,.29);
  timers[0]();doSyncCheck(state);assert.equal(applied,1);
});

test('legacy supplementary words count as unfinished PDF, with no reading panel API',async()=>{
  const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/research-max/readingPanel.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
  const mod=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
  assert.equal(mod.createReadingPanel,undefined);
  const report={coverage:{total:100,pdf:60,supplement:30,missing:10},reading:{units:[{page:1,status:'pdf'},{page:2,status:'supplement',translation:'中文',region:[1,2,3,4]}]}};
  assert.equal(mod.coverageText(report),'正文已排入 PDF 60% · 未完成 40%');
  assert.equal(mod.reviewIssues(report)[0].types[0],'layout_pending');
});
