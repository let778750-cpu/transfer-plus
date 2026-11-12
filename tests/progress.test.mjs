import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import * as esbuild from 'esbuild';
const root=path.resolve(import.meta.dirname,'..');
const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/research-max/progress.ts')],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{}});
const {TranslationNotices,closeLegacyCancelledNotices}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
function setup(){
  let now=0,counter=0;const timers=new Map(),popups=[],errors=[];
  const clock={setTimeout:(fn,ms)=>{const id=++counter;timers.set(id,{fn,at:now+ms});return id;},clearTimeout:id=>timers.delete(id)};
  const manager=new TranslationNotices(()=>{
    const popup={closed:false,closes:0,shown:false,headline:'',nativeTimer:false,
      close(){this.closed=true;this.closes++;},changeHeadline(text){this.headline=text;},show(){this.shown=true;},
      startCloseTimer(){this.nativeTimer=true;},hover(){this.nativeTimer=false;},click(){this.close();}};
    popup.ItemProgress=function(_icon,title){this.text=title;this.progress=0;this.setProgress=n=>this.progress=n;this.setText=s=>this.text=s;this.setError=()=>this.error=true;popup.line=this;};
    popups.push(popup);return popup;
  },clock,e=>errors.push(e));
  return {manager,popups,timers,errors,advance(ms){now+=ms;for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.fn();}}};
}

test('cancelled/completed/skipped/failed notices expire even when the mouse cancels native timers',()=>{
  for(const outcome of ['cancelled','completed','skipped','failed']){
    const h=setup(),n=h.manager.show(1,'paper');n.update(38,'working');h.advance(10000);
    assert.equal(h.popups[0].closed,false); // running tasks do not time out
    n.finish(outcome,'terminal message');h.popups[0].hover();h.advance(3499);assert.equal(h.popups[0].closed,false);
    assert.equal(h.popups[0].line.progress,100);assert.equal(h.popups[0].line.text,'terminal message');
    if(outcome==='failed')assert.equal(h.popups[0].line.error,true);
    h.advance(1);assert.equal(h.popups[0].closed,true);assert.equal(h.timers.size,0);
  }
});

test('restarting one paper closes only its old notice; an old timeout cannot close the new task',()=>{
  const h=setup(),old=h.manager.show(1,'one'),other=h.manager.show(2,'two');old.finish('cancelled','saved');
  const queued=[...h.timers.values()][0].fn;h.manager.close(1); // before archive lookup or job submission
  assert.equal(h.popups[0].closed,true);assert.equal(h.popups[1].closed,false);
  const next=h.manager.show(1,'one resumed');queued();old.finish('failed','stale');old.update(99,'stale');
  assert.equal(h.popups[2].closed,false);assert.equal(h.popups[2].line.text,'one resumed');
  next.finish('completed','done');other.finish('cancelled','saved');h.advance(3500);
  assert.ok(h.popups.every(x=>x.closed));assert.equal(h.popups[0].closes,1);
});

test('click dismisses only the UI; later progress and completion cannot reopen the popup',()=>{
  const h=setup(),notice=h.manager.show(3,'paper');notice.update(15,'running');h.popups[0].click();
  notice.update(90,'still running');notice.finish('completed','done');
  assert.equal(h.popups[0].line.text,'running');assert.equal(h.popups[0].closes,1);assert.equal(h.timers.size,0);
});

test('disable closes running and terminal notices and cancels all owned timers',()=>{
  const h=setup(),running=h.manager.show(1,'running'),done=h.manager.show(2,'done');done.finish('cancelled','saved');
  h.manager.closeAll();assert.ok(h.popups.every(x=>x.closed));assert.equal(h.timers.size,0);
  running.finish('completed','late result');h.advance(3500);assert.ok(h.popups.every(x=>x.closes===1));
});

test('a broken terminal display still expires and cannot retain subsequent tasks',()=>{
  const h=setup(),n=h.manager.show(4,'paper');h.popups[0].changeHeadline=()=>{throw Error('dead document');};
  n.finish('cancelled','saved');assert.equal(h.errors.length,1);h.advance(3500);assert.equal(h.popups[0].closed,true);
  h.manager.show(4,'retry');assert.equal(h.popups[1].shown,true);
});

test('upgrade cleans only the exact legacy cancelled translation popup',()=>{
  const make=(uri,title,text)=>({closed:false,close(){this.closed=true;},document:{documentURI:uri,
    getElementById:id=>id.endsWith('headline')?{children:[{getAttribute:()=>title}]}:{textContent:text}}});
  const uri='chrome://zotero/content/progressWindow.xhtml',cancelled='已取消，进度已保留；再次点击翻译即可继续';
  const old=make(uri,'生成中文对照',cancelled),running=make(uri,'生成中文对照','处理中'),other=make(uri,'正在保存',cancelled),normal=make('normal.xhtml','生成中文对照',cancelled),removed=[];
  closeLegacyCancelledNotices([old,running,other,normal],win=>removed.push(win),e=>{throw e;});
  assert.deepEqual(removed,[old]);assert.equal(old.closed,true);assert.ok([running,other,normal].every(x=>!x.closed));
});
