import test from 'node:test';
import assert from 'node:assert/strict';
import * as esbuild from 'esbuild';
import path from 'node:path';

async function readerFixture(){
  const mocks={
    readerPaneAdapter:`export class ReaderPane{constructor(item){this.item=item;this.ready=true;}async attach(){}async waitForScrollContainer(){return true}getPreciseState(){return {scale:1}}getScrollContainer(){return {id:'viewer'}}destroy(){if(globalThis.failReaderDestroy)throw new Error('destroy failed');}}export const createReaderBrowser=win=>win.document.createXULElement('browser');`,
    splitViewSync:'export const stateMap=new Map();export const makeSyncTrigger=()=>()=>{};export const installSync=()=>{};export const primeSyncBaselines=()=>{};',
    readerConfig:'export const flushAnnotationWrites=async()=>{};'};
  const bundle=await esbuild.build({entryPoints:[path.resolve('plugin/src/research-max/reader.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{},plugins:[{name:'host',setup(build){build.onResolve({filter:/\/(readerPaneAdapter|splitViewSync|readerConfig)$/},a=>({path:a.path.split('/').at(-1),namespace:'mock'}));build.onLoad({filter:/.*/,namespace:'mock'},a=>({contents:mocks[a.path],loader:'ts'}));}}]});
  const {openPair,closeAll}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
  class Node{
    constructor(tag){this.tag=tag;this.children=[];this.style={};this.isConnected=true;this.events={};}
    append(...items){for(const n of items){this.children.push(n);n.parentNode=this;}}
    appendChild(n){this.append(n);return n;}
    replaceChild(n,old){this.children[this.children.indexOf(old)]=n;n.parentNode=this;}
    cloneNode(){return new Node(this.tag);}
    setAttribute(){}addEventListener(k,f){this.events[k]=f;}removeEventListener(){}
    getBoundingClientRect(){return {width:500};}
  }
  const root=new Node('root');let old;
  const doc={createElementNS:(_,tag)=>new Node(tag),createXULElement:tag=>new Node(tag),getElementById:()=>old,head:new Node('head')};
  const closed=[];
  const win={document:doc,Zotero_Tabs:{add:()=>{old=new Node('tab');root.append(old);return {id:'pair'}},select(){},close(id){closed.push(id);}},MutationObserver:class{observe(){}disconnect(){}},removeEventListener(){}};
  globalThis.Zotero={Prefs:{get:()=>null},ResearchMaxBilingual:{watchTranslation:(_,f)=>{f({label:'翻译',disabled:false});return()=>{};}},Profile:{dir:'/test'},version:'10',logError(){}};
  globalThis.PathUtils={join:(...a)=>a.join('/')};globalThis.IOUtils={writeJSON:async()=>{}};
  const left={id:1,key:'original',getField:()=> 'paper'},right={id:2,key:'translated'};
  return {openPair,closeAll,win,doc,closed,left,right};
}
test('legacy and current archives mount exactly two PDF panes, never supplementary text',async()=>{
  const {openPair,closeAll,win,left,right}=await readerFixture();
  const reports=[undefined,{schema:2,units:[{page:7,status:'supplement',translation:'must stay in cache',segments:[{text:'must not render'}]}]},
    {schema:3,units:[{page:7,status:'missing',translation:'stored only'}]}]
    .map(reading=>({reading,issues:[{page:7,types:['paragraph_missing'],referenceTranslation:'never display'}]}));
  // Actual cloud_pipeline.report_for contract, not the old engine's `types` array.
  reports.push({schema:3,pipeline:'paddle-cloud-layout5-compose2',pages:19,
    reading:{schema:5,units:[{id:'region-a',page:7,region:[10,20,80,90],status:'missing',reason:'layout_pending',translation:'stored only'},
      {id:'formula-a',page:7,status:'preserved',reason:'formula'}]},
    issues:[{unitId:'region-a',page:7,region:[10,20,80,90],type:'layout_pending',fallback:'region'}]});
  for(const report of reports){
    const state=await openPair(win,left,right,report);
    const descendants=n=>[n,...n.children.flatMap(descendants)];
    const nodes=descendants(state.container);
    assert.equal(nodes.filter(n=>n.tag==='browser').length,2);
    assert.equal(nodes.filter(n=>['details','summary','pre','img'].includes(n.tag)).length,0);
    assert.deepEqual(state.rightBrowser.parentNode.children,[state.rightBrowser]);
    assert.equal(nodes.filter(n=>n.tag==='button'&&n.textContent==='翻译').length,1);
    await closeAll();
  }
});

test('toolbar construction and post-registration failures both close the empty tab and permit retry',async()=>{
  const {openPair,closeAll,win,doc,closed,left,right}=await readerFixture();
  const create=doc.createElementNS;
  doc.createElementNS=(ns,tag)=>{if(tag==='select')throw new Error('toolbar failed');return create(ns,tag);};
  await assert.rejects(openPair(win,left,right,{issues:[]}),/toolbar failed/);
  assert.deepEqual(closed,['pair']);
  doc.createElementNS=create;
  const watch=Zotero.ResearchMaxBilingual.watchTranslation;
  globalThis.failReaderDestroy=true;
  Zotero.ResearchMaxBilingual.watchTranslation=()=>{throw new Error('watch failed');};
  await assert.rejects(openPair(win,left,right,{issues:[]}),/watch failed/);
  assert.deepEqual(closed,['pair','pair']);
  delete globalThis.failReaderDestroy;
  Zotero.ResearchMaxBilingual.watchTranslation=watch;
  const state=await openPair(win,left,right,{issues:[]});
  assert.ok(state.leftAdapter.ready&&state.rightAdapter.ready);
  await closeAll();
});

test('cloud and legacy issues normalize once per region, without intentional formula preservation',async()=>{
  const bundle=await esbuild.build({entryPoints:['plugin/src/research-max/readingPanel.ts'],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
  const {reviewIssues}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
  const report={issues:[{page:3,region:[1,2,3,4],type:'layout_pending',unitId:'a'},
      {page:4,types:['math_geometry_changed','math_glyph_changed']},null],
    reading:{units:[{id:'a',page:3,region:[1,2,3,4],status:'missing',translation:'已缓存'},
      {id:'b',page:5,region:[1,2,3,4],status:'missing',reason:'translation_incomplete'},
      {id:'c',page:3,status:'pdf'},{id:'d',page:3,status:'preserved',reason:'formula'}]}};
  const issues=reviewIssues(report);
  assert.equal(issues.length,3);
  assert.deepEqual(issues[0].types,['layout_pending']);
  assert.deepEqual(issues[1].types,['math_geometry_changed','math_glyph_changed']);
  assert.deepEqual(issues[2].types,['translation_incomplete']);
  assert.deepEqual(reviewIssues({issues:[{page:1,types:[null,4],type:'future_reason'}],reading:{units:null}})[0].types,['future_reason']);
  assert.deepEqual(reviewIssues({issues:null}),[]);
});

test('native annotation save error or missing DB value keeps reader open',async()=>{
  const bundle=await esbuild.build({entryPoints:['plugin/src/research-max/reader.ts'],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
  const {closeForUpdate}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
  let closed=0;
  const manager={_readOnly:false,_annotations:[{id:'ANNO0001',comment:'new',text:'highlight'}],_unsavedAnnotations:new Map([['ANNO0001',{}]]),
    _triggerSaving:async()=>{manager._unsavedAnnotations.clear();manager._readOnly=true;}};
  const internal={_annotationManager:manager,_state:{readOnly:false}};
  const reader={itemID:2,_iframe:{contentWindow:{_reader:internal}},close:()=>closed++};
  const saved={parentID:2,annotationComment:'new',annotationText:'highlight'};
  globalThis.Zotero={Reader:{_readers:[reader]},Promise:{delay:async()=>{}},Items:{get:()=>({libraryID:1}),getByLibraryAndKey:()=>saved}};
  await assert.rejects(closeForUpdate(2),/批注保存失败/);assert.equal(closed,0);
  manager._readOnly=false;saved.annotationComment='old';
  await assert.rejects(closeForUpdate(2),/未完整写入/);assert.equal(closed,0);
  saved.annotationComment='new';await closeForUpdate(2);assert.equal(closed,1);
});
