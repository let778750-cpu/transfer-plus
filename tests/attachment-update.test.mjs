import test from 'node:test';
import assert from 'node:assert/strict';
import * as esbuild from 'esbuild';
const bundle=await esbuild.build({entryPoints:['plugin/src/research-max/attachmentUpdate.ts'],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
const {replaceAttachment}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
function fixture(failure=false){
 const files=new Map([['old','oldhash'],['new','newhash'],['backup','oldhash']]);const events=[];
 globalThis.IOUtils={read:async p=>files.get(p),write:async(p,v,options)=>{assert.ok(options.tmpPath);files.set(p,v);events.push('write');}};
 const item={id:2,key:'TRAN0001',libraryID:1,getFilePathAsync:async()=> 'old',getAnnotations:()=>[{key:'ANNO0001',annotationPosition:'{"pageIndex":1}',annotationText:'text'}],saveTx:async()=>{if(failure)throw Error('sync failed');events.push('save');},relinkAttachmentFile:async p=>{assert.equal(p,'old');events.push('refresh');}};
 const deps={hash:p=>files.get(p),close:async()=>events.push('close'),api:async(p,v)=>{assert.equal(v.annotations[0].key,'ANNO0001');events.push('backup');return {backupPath:'backup'};}};
 return {files,events,item,deps,run:()=>replaceAttachment(item,'new',{sourceKey:'1:ORIG0001',report:{outputHash:'oldhash'}},{sourceHash:'source',outputHash:'newhash'},'job',deps)};
}
test('changed PDF updates same attachment and preserves annotation identity',async()=>{const h=fixture();assert.equal(await h.run(),h.item);assert.equal(h.files.get('old'),'newhash');assert.equal(h.item.attachmentSyncState,'to_upload');assert.deepEqual(h.events,['close','backup','write','save','refresh']);});
test('failed metadata commit restores verified previous PDF',async()=>{const h=fixture(true);await assert.rejects(h.run(),/sync failed/);assert.equal(h.files.get('old'),'oldhash');});
test('externally changed translation cannot be silently overwritten',async()=>{const h=fixture();h.files.set('old','external');await assert.rejects(h.run(),/未覆盖/);assert.deepEqual(h.events,[]);});
test('mapping write failure also rolls back PDF, allowing a later retry',async()=>{const h=fixture();h.deps.commit=async()=>{throw Error('mapping failed');};await assert.rejects(h.run(),/mapping failed/);assert.equal(h.files.get('old'),'oldhash');h.deps.commit=async()=>{};await h.run();assert.equal(h.files.get('old'),'newhash');});
