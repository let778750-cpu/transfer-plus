import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import * as esbuild from 'esbuild';
const root=path.resolve(import.meta.dirname,'..');
const built=await esbuild.build({entryPoints:[path.join(root,'plugin/src/research-max/settingsWindow.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
const {openSettingsWindow,SETTINGS_URL}=await import('data:text/javascript;base64,'+Buffer.from(built.outputFiles[0].text).toString('base64'));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function host(readyState='loading',documentURI=SETTINGS_URL){
  const listeners=new Map(),message={textContent:'正在加载设置…'},calls=[];
  const win={closed:false,document:{documentURI,readyState,getElementById:()=>message},
    addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)};
  return {win,message,calls,load:()=>listeners.get('load')?.(),openDialog:(...args)=>{calls.push(args);return win;}};
}
test('opens registered chrome page with explicit native size and initializes once',async()=>{
  const h=host();let count=0;
  openSettingsWindow(h,async()=>{count++;},assert.fail);
  assert.equal(h.calls[0][0],SETTINGS_URL);assert.match(h.calls[0][2],/width=760,height=760/);
  assert.equal(count,0);h.load();h.load();await tick();assert.equal(count,1);
  assert.equal(h.win.document.title,'中文对照设置');
});
test('handles load completed synchronously and ignores initial about:blank',async()=>{
  let count=0;const loaded=host('complete');openSettingsWindow(loaded,async()=>{count++;},assert.fail);await tick();assert.equal(count,1);
  const blank=host('complete','about:blank');openSettingsWindow(blank,async()=>{count++;},assert.fail);await tick();assert.equal(count,1);
  blank.win.document.documentURI=SETTINGS_URL;blank.load();await tick();assert.equal(count,2);
});
test('closed windows do not initialize and render failure has a visible message',async()=>{
  const closed=host();openSettingsWindow(closed,()=>assert.fail('closed window'),assert.fail);closed.win.closed=true;closed.load();await tick();
  const failed=host();const errors=[];openSettingsWindow(failed,async()=>{throw Error('fixture failed');},e=>errors.push(e));failed.load();await tick();
  assert.equal(errors.length,1);assert.match(failed.message.textContent,/设置加载失败：fixture failed/);
});
test('bootstrap registers chrome before plugin loading and unregisters on shutdown failure',async()=>{
  const calls=[];const ctx={Zotero:{initializationPromise:Promise.resolve()},
    ChromeUtils:{importESModule:()=>({AddonManager:{AUTOUPDATE_DISABLE:0,getAddonByID:async()=>({})}})},
    Services:{io:{newURI:value=>value},scriptloader:{loadSubScript:(url,context)=>{calls.push(['load',url]);context.RMB={start:async()=>{},stop:async()=>{throw Error('stop fixture');}};}}},
    Components:{interfaces:{},classes:{'@mozilla.org/addons/addon-manager-startup;1':{getService:()=>({registerChrome:(uri,paths)=>{calls.push(['register',uri,paths]);return{destruct:()=>calls.push(['destruct'])};}})}}},IOUtils:{},PathUtils:{}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(root,'plugin/research-max/bootstrap.js'),'utf8'),ctx);
  await ctx.startup({id:'test',rootURI:'jar:file:///plugin.xpi!/'});
  assert.equal(calls[0][0],'register');assert.equal(calls[0][2][0][1],'research-max-bilingual');
  assert.equal(calls[0][2][0][2],'jar:file:///plugin.xpi!/content/');assert.equal(calls[1][0],'load');
  await assert.rejects(ctx.shutdown(),/stop fixture/);assert.equal(calls.at(-1)[0],'destruct');
});
