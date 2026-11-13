import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import * as esbuild from 'esbuild';

const root=path.resolve(import.meta.dirname,'..');
const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/research-max/pairState.ts')],
  bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
const {createPairState}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));

function attachment(id,key,{libraryID=1,hash='O1',annotations=0}={}){
  return {id,libraryID,key,deleted:false,
    isPDFAttachment:()=>true,fileExists:async()=>true,getFilePathAsync:async()=>'x',
    fileHash:hash,getAnnotations:()=>new Array(annotations)};
}
function fixture(records,overrides={}){
  const errors=[],closed=[],opened=[],selected=[];let writes=0;
  let data={...records};
  const items=new Map();
  const deps={
    maps:async()=>({p:'maps.json',data}),
    remember:async(key,record)=>{data={...data,[key]:record};writes++;},
    sourceHash:async item=>item.sourceHash??null,
    attachmentHash:async item=>item.fileHash,
    epoch:()=>7,
    items:{get:id=>items.get(id),getByLibraryAndKey:(lib,key)=>items.get(`${lib}:${key}`)},
    tasksHas:()=>false,
    openPair:async(win,left,right,report)=>{opened.push({left,right,report});return {tabID:'pair-new'};},
    activePairs:()=>[],
    closeReader:reader=>closed.push(reader),
    mainWindow:()=>({}),
    logError:e=>errors.push(e),
    onViewChanged:()=>{},
    ...overrides,
  };
  return {deps,items,errors,closed,opened,selected,get writes(){return writes;},get data(){return data;}};
}
const record=(over={})=>({sourceHash:'H1',attachmentKey:'C1',libraryID:1,epoch:7,
  report:{outputHash:'O1'},pairState:'enabled',...over});

test('viewable lookup ignores the config epoch, so old translations stay readable after settings change',async()=>{
  const source=attachment(5,'SRC',{hash:'H1'});source.sourceHash='H1';
  const companion=attachment(6,'C1');
  const f=fixture({'1:SRC':record({epoch:3})});
  f.items.set('1:SRC',source);f.items.set(5,source);f.items.set('1:C1',companion);
  const pair=createPairState(f.deps);
  const view=await pair.viewableFor(source);
  assert.equal(view.record.attachmentKey,'C1');assert.equal(view.stale,true);
});

test('maybePair opens the pair from the archive and closes the plain reader tab',async()=>{
  const source=attachment(5,'SRC',{hash:'H1'});source.sourceHash='H1';
  const f=fixture({'1:SRC':record()});
  f.items.set('1:SRC',source);f.items.set(5,source);f.items.set('1:C1',attachment(6,'C1'));
  const pair=createPairState(f.deps);
  await pair.maybePair({itemID:5,_type:'pdf',_tabID:'plain-1'});
  assert.equal(f.opened.length,1);assert.equal(f.opened[0].right.key,'C1');
  assert.deepEqual(f.closed.map(r=>r._tabID),['plain-1']);assert.equal(f.errors.length,0);
});

test('a user-disabled pair is never reopened automatically',async()=>{
  const source=attachment(5,'SRC',{hash:'H1'});source.sourceHash='H1';
  const f=fixture({'1:SRC':record({pairState:'disabled'})});
  f.items.set('1:SRC',source);f.items.set(5,source);f.items.set('1:C1',attachment(6,'C1'));
  const pair=createPairState(f.deps);
  await pair.maybePair({itemID:5,_type:'pdf',_tabID:'plain-1'});
  assert.equal(f.opened.length,0);
});

test('a companion attachment never pairs or translates as its own source (recursion guard)',async()=>{
  const companion=attachment(6,'C1',{hash:'X'});companion.sourceHash='X';
  const f=fixture({'1:SRC':record()});
  f.items.set('1:C1',companion);f.items.set(6,companion);
  const pair=createPairState(f.deps);
  const counterpart=await pair.counterpartOf(companion);
  assert.equal(counterpart.sourceKey,'1:SRC');
  await pair.maybePair({itemID:6,_type:'pdf',_tabID:'plain-1'});
  assert.equal(f.opened.length,0);
});

test('an already open pair is focused and the plain duplicate closed, not paired twice',async()=>{
  const source=attachment(5,'SRC',{hash:'H1'});source.sourceHash='H1';
  const f=fixture({'1:SRC':record()},{activePairs:()=>[{leftID:5,tabID:'pair-A',select:()=>f.selected.push('pair-A')}]});
  f.items.set('1:SRC',source);f.items.set(5,source);f.items.set('1:C1',attachment(6,'C1'));
  const pair=createPairState(f.deps);
  await pair.maybePair({itemID:5,_type:'pdf',_tabID:'plain-2'});
  assert.equal(f.opened.length,0);assert.deepEqual(f.selected,['pair-A']);
  assert.deepEqual(f.closed.map(r=>r._tabID),['plain-2']);
  // A reader hosted inside the pair tab itself (same tab id) is never closed.
  f.closed.length=0;
  await pair.maybePair({itemID:5,_type:'pdf',_tabID:'pair-A'});
  assert.equal(f.closed.length,0);
});

test('an in-flight translation task suspends auto-pairing',async()=>{
  const source=attachment(5,'SRC',{hash:'H1'});source.sourceHash='H1';
  const f=fixture({'1:SRC':record()},{tasksHas:()=>true});
  f.items.set('1:SRC',source);f.items.set(5,source);f.items.set('1:C1',attachment(6,'C1'));
  const pair=createPairState(f.deps);
  await pair.maybePair({itemID:5,_type:'pdf',_tabID:'plain-1'});
  assert.equal(f.opened.length,0);
});

test('a source without a usable translation is not retried every sweep',async()=>{
  const source=attachment(5,'SRC',{hash:'H1'});source.sourceHash='H1';
  const f=fixture({}); // no records at all
  f.items.set('1:SRC',source);f.items.set(5,source);
  const pair=createPairState(f.deps);
  const reader={itemID:5,_type:'pdf',_tabID:'plain-1'};
  await pair.maybePair(reader);await pair.maybePair(reader);
  assert.equal(f.opened.length,0);
  // After a translation lands, invalidate() re-enables pairing for it.
  f.deps.maps=async()=>({p:'maps.json',data:{'1:SRC':record()}});
  f.items.set('1:C1',attachment(6,'C1'));
  pair.invalidate(5);
  await pair.maybePair(reader);
  assert.equal(f.opened.length,1);
});

test('setPairState persists the opt-out through remember and skips redundant writes',async()=>{
  const source=attachment(5,'SRC',{hash:'H1'});
  const f=fixture({'1:SRC':record()});
  const pair=createPairState(f.deps);
  await pair.setPairState(source,'disabled');
  assert.equal(f.data['1:SRC'].pairState,'disabled');assert.equal(f.writes,1);
  await pair.setPairState(source,'disabled');
  assert.equal(f.writes,1); // unchanged state is not written again
});

test('viewableFor rejects a companion whose file no longer matches the record',async()=>{
  const source=attachment(5,'SRC',{hash:'H1'});source.sourceHash='H1';
  const f=fixture({'1:SRC':record()}); // record expects outputHash O1
  f.items.set('1:SRC',source);f.items.set(5,source);
  f.items.set('1:C1',attachment(6,'C1',{hash:'TAMPERED'}));
  const pair=createPairState(f.deps);
  assert.equal(await pair.viewableFor(source),null);
});
