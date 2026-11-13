import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import * as esbuild from 'esbuild';
let serial=0;
async function host(failAt=''){
  const originalHash='11'.repeat(32),outputHash='22'.repeat(32),opened=[],requests=[],notices=[];
  const original={id:1,key:'ORIG0001',libraryID:1,isPDFAttachment:()=>true,getFilePathAsync:async()=>'/source.pdf',getField:()=> 'paper'};
  const translated={id:2,key:'TRAN0001',fileExists:async()=>true,getFilePathAsync:async()=>'/translation.pdf'};
  const oldReport={sourceHash:originalHash,outputHash,issues:[],reading:{schema:1}};
  const report={...oldReport,reading:{schema:2},coverage:{total:10,pdf:10,supplement:0,missing:0}};
  let mapping={'1:ORIG0001':{sourceHash:originalHash,epoch:0,libraryID:1,attachmentKey:'TRAN0001',report:oldReport}},imports=0;
  globalThis.__rmbTest={openPair:async(win,left,right,r)=>{opened.push({left,right,report:r});},
    show:()=>({update(){},close(){},finish:(...v)=>notices.push(v)}),
    api:async(p,data)=>{requests.push({p,data});if(p===failAt)throw Error('offline');
      if(p==='/api/config')return {hasApiKey:true,hasPaddleApiKey:true,model:'test-model'};
      if(p==='/translate')return {taskId:'job',status:'completed',active:false,message:'checked'};
      if(p==='/api/tasks/job/report')return report;
      if(p==='/api/tasks/job/imported')return {ok:true};
      throw Error('unexpected endpoint '+p);}};
  globalThis.Components={interfaces:{},classes:{
    '@mozilla.org/network/file-input-stream;1':{createInstance:()=>({init(p){this.path=p;},close(){}})},
    '@mozilla.org/security/hash;1':{createInstance:()=>({init(){},updateFromStream(s){this.value=s.path==='/source.pdf'?'\x11':'\x22';},finish(){return this.value.repeat(32);}})}}};
  globalThis.PathUtils={join:(...v)=>v.join('/')};
  globalThis.IOUtils={exists:async()=>false,readJSON:async()=>structuredClone(mapping),writeJSON:async(p,v)=>{mapping=structuredClone(v);},stat:async()=>({lastModified:1,size:1})};
  globalThis.ChromeUtils={importESModule:()=>({})};globalThis.Services={wm:{getEnumerator:()=>[]}};
  globalThis.Zotero={Profile:{dir:'/profile'},Prefs:{get:()=>0},File:{pathToFile:p=>p},
    Items:{get:id=>id===1?original:undefined,getByLibraryAndKey:()=>translated},Attachments:{importFromFile:async()=>{imports++;return translated;}},
    addShutdownListener(){},getMainWindows:()=>[],getMainWindow:()=>({alert(){}}),logError(){},Reader:{registerEventListener(){},unregisterEventListener(){}}};
  const mocks={
    service:'export const api=(...a)=>globalThis.__rmbTest.api(...a);export const download=async()=>"/new.pdf";export const locate=async()=>{};export const stopService=async()=>{};',
    reader:'export const openPair=(...a)=>globalThis.__rmbTest.openPair(...a);export const closeAll=async()=>{};export const closeForUpdate=async()=>{};export const activePairs=()=>[];export const closePairAndRestore=async()=>{};',
    toolbar:'export class ReaderToolbar{start(){} stop(){} refresh(){}}',
    progress:'export class TranslationNotices{close(){} closeAll(){} show(){return globalThis.__rmbTest.show();}}export const closeLegacyCancelledNotices=()=>{};'};
  const bundle=await esbuild.build({entryPoints:[path.resolve('plugin/src/research-max/main.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{},
    plugins:[{name:'host',setup(build){build.onResolve({filter:/^\.\/(service|reader|toolbar|progress)$/},a=>({path:a.path.slice(2),namespace:'mock'}));build.onLoad({filter:/.*/,namespace:'mock'},a=>({contents:mocks[a.path],loader:'ts'}));}}]});
  const mod=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64')+'#'+serial++);await mod.start();
  return {mod,original,translated,opened,requests,notices,mapping:()=>mapping,imports:()=>imports,report};
}
test('one Translate checks cached paper and updates its report without duplicate attachment',async()=>{
  const h=await host();const labels=[];const unsubscribe=Zotero.ResearchMaxBilingual.watchTranslation(1,s=>labels.push(s.label));
  await Zotero.ResearchMaxBilingual.translate(h.original);
  assert.equal(h.requests.find(r=>r.p==='/translate').data.automatic,true);
  assert.equal(h.opened.length,2);assert.equal(h.opened[0].right,h.translated);assert.deepEqual(h.opened[1].report,h.report);
  assert.equal(h.imports(),0);assert.equal(h.mapping()['1:ORIG0001'].report.reading.schema,2);
  // The mock hosts no live pair, so the idle label lands on the archive action.
  assert.ok(labels.includes('取消翻译'));assert.equal(labels.at(-1),'打开对照');unsubscribe();await h.mod.stop();
});
test('provider submission failure leaves existing verified local translation readable',async()=>{
  const h=await host('/translate');await assert.rejects(Zotero.ResearchMaxBilingual.translate(h.original),/offline/);
  assert.equal(h.opened.length,1);assert.equal(h.opened[0].right,h.translated);assert.equal(h.imports(),0);await h.mod.stop();
});
test('local service failure opens archive and explicitly reports audit was not performed',async()=>{
  const h=await host('/api/config');await Zotero.ResearchMaxBilingual.translate(h.original);
  assert.ok(h.opened.length>=1);assert.equal(h.requests.some(r=>r.p==='/translate'),false);
  assert.match(h.notices[0][1],/未执行查漏补缺/);assert.equal(h.imports(),0);await h.mod.stop();
});
