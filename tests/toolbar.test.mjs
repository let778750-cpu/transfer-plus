import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import * as esbuild from 'esbuild';

const root=path.resolve(import.meta.dirname,'..');
async function loadModule(file){
  const bundle=await esbuild.build({entryPoints:[path.join(root,'plugin/src/research-max',file)],bundle:true,format:'esm',platform:'node',write:false,tsconfigRaw:{},
    plugins:[{name:'localization',setup(build){build.onResolve({filter:/utils\/locale$/},()=>({path:path.join(root,'plugin/src/research-max/locale.ts')}));}}]});
  return import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
}

// Minimal tree with real connectivity/containment semantics. The host mirrors
// Zotero 10 CustomSections: clear its contents, synchronously dispatch render.
function documentTree(ready=true){
  const nodes=[];
  const doc={};
  function node(tag){
    const n={tag,children:[],parentNode:null,handlers:{},attributes:{},
      get isConnected(){return this===doc.documentElement||!!this.parentNode?.isConnected;},
      contains(child){return this===child||this.children.some(x=>x.contains(child));},
      appendChild(child){child.remove();this.children.push(child);child.parentNode=this;return child;},
      remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(x=>x!==this);this.parentNode=null;},
      replaceChildren(...children){for(const child of [...this.children])child.remove();for(const child of children)this.appendChild(child);},
      setAttribute(k,v){this.attributes[k]=v;},
      addEventListener(k,fn){(this.handlers[k]??=[]).push(fn);},
      click(){for(const fn of this.handlers.click||[])fn({button:0,detail:1,preventDefault(){},stopPropagation(){}});}
    };nodes.push(n);return n;
  }
  doc.documentElement=node('html');doc.head=node('head');doc.documentElement.appendChild(doc.head);
  doc.body=node('body');doc.documentElement.appendChild(doc.body);
  doc.createElementNS=(_ns,tag)=>node(tag);
  doc.getElementById=id=>nodes.find(n=>n.isConnected&&n.id===id);
  doc.querySelector=selector=>{
    assert.equal(selector,'.toolbar .end .custom-sections');
    return doc.slot?.isConnected?doc.slot:null;
  };
  doc.buttons=()=>nodes.filter(n=>n.isConnected&&n.className?.includes('research-max-bilingual-button'));
  doc.querySelectorAll=()=>nodes.filter(n=>n.isConnected&&(n.className?.includes('research-max-bilingual-button')||n.title==='生成或打开中文对照 PDF'));
  doc.mount=()=>{doc.slot?.remove();doc.slot=node('div');doc.body.appendChild(doc.slot);};
  if(ready)doc.mount();
  return doc;
}
function harness(ReaderToolbar,readers=[]){
  let tick,handler;const errors=[],actions=[],cleared=[],states=new Map();
  const host={readers:()=>readers,register:fn=>handler=fn,unregister:fn=>{if(handler===fn)handler=undefined;},
    repeat:(fn,ms)=>{assert.equal(ms,1000);tick=fn;return 42;},clear:id=>cleared.push(id),error:e=>errors.push(e)};
  const toolbar=new ReaderToolbar(host,id=>actions.push(id),(button,id)=>{button.textContent=states.get(id)||'翻译';});
  return {toolbar,readers,errors,actions,states,cleared,tick:()=>tick(),get handler(){return handler;},
    render(reader){const doc=reader._iframeWindow.document;doc.slot.replaceChildren();handler({reader,doc,append:n=>doc.slot.appendChild(n)});}};
}
const pdf=(id,doc=documentTree())=>({_type:'pdf',itemID:id,_iframeWindow:doc?{document:doc}:null});

test('hot install supplies absent buttons and replaces old handlers without translation',async()=>{
  const {ReaderToolbar}=await loadModule('toolbar.ts');
  const first=pdf(101),second=pdf(202);
  const old=second._iframeWindow.document.createElementNS('','button');old.title='生成或打开中文对照 PDF';
  old.addEventListener('click',()=>assert.fail('stale handler'));second._iframeWindow.document.slot.appendChild(old);
  const h=harness(ReaderToolbar,[first,second]);h.toolbar.start();
  assert.deepEqual(h.actions,[]);assert.equal(old.isConnected,false);
  for(const r of h.readers){const [b]=r._iframeWindow.document.buttons();assert.equal(b.textContent,'翻译');assert.match(b.className,/toolbar-button/);b.click();}
  assert.deepEqual(h.actions,[101,202]);h.toolbar.stop();
});

test('new PDF events, missed events and delayed iframes all get their own clickable button',async()=>{
  const {ReaderToolbar}=await loadModule('toolbar.ts');const h=harness(ReaderToolbar);h.toolbar.start();
  const fresh=pdf(303);h.readers.push(fresh);h.render(fresh);
  assert.equal(fresh._iframeWindow.document.buttons().length,1);
  const missed=pdf(404);h.readers.push(missed);h.tick();
  const delayed=pdf(505,null);h.readers.push(delayed);h.tick();
  delayed._iframeWindow={document:documentTree(false)};h.tick();assert.equal(delayed._iframeWindow.document.buttons().length,0);
  delayed._iframeWindow.document.mount();h.tick();
  for(const r of h.readers)r._iframeWindow.document.buttons()[0].click();
  assert.deepEqual(h.actions,[303,404,505]);assert.deepEqual(h.errors,[]);h.toolbar.stop();
});

test('re-render, toolbar replacement and iframe navigation neither duplicate nor lose state',async()=>{
  const {ReaderToolbar}=await loadModule('toolbar.ts');const r=pdf(606),h=harness(ReaderToolbar,[r]);h.toolbar.start();
  let doc=r._iframeWindow.document;const initial=doc.buttons()[0];
  h.states.set(606,'取消翻译');h.toolbar.refresh();
  for(let i=0;i<4;i++){h.render(r);h.tick();assert.equal(doc.buttons().length,1);}
  assert.equal(doc.buttons()[0],initial);assert.equal(initial.handlers.click.length,1);
  doc.mount();h.tick();assert.equal(doc.buttons()[0],initial);assert.equal(initial.textContent,'取消翻译');
  r._iframeWindow.document=documentTree();h.tick();assert.equal(initial.isConnected,false);
  doc=r._iframeWindow.document;assert.equal(doc.buttons().length,1);assert.equal(doc.buttons()[0].textContent,'取消翻译');
  doc.buttons()[0].click();assert.deepEqual(h.actions,[606]);h.toolbar.stop();
});

test('tab closure, reopening and disable clean up; queued callbacks cannot revive a button',async()=>{
  const {ReaderToolbar}=await loadModule('toolbar.ts');const first=pdf(707),h=harness(ReaderToolbar,[first]);h.toolbar.start();
  const previous=first._iframeWindow.document.buttons()[0];h.readers.length=0;h.tick();assert.equal(previous.isConnected,false);
  const reopened=pdf(707);h.readers.push(reopened);h.tick();const button=reopened._iframeWindow.document.buttons()[0];
  const queued=h.handler;h.toolbar.stop();assert.deepEqual(h.cleared,[42]);assert.equal(h.handler,undefined);
  h.tick();queued({reader:reopened,doc:reopened._iframeWindow.document,append:()=>assert.fail('disabled')});
  button.click();assert.deepEqual(h.actions,[]);assert.equal(button.isConnected,false);
});

test('a failing reader cannot prevent another PDF; EPUBs get no PDF translation button',async()=>{
  const {ReaderToolbar}=await loadModule('toolbar.ts');const broken={_type:'pdf',get _iframeWindow(){throw Error('closed window');}};
  const valid=pdf(808),epub={...pdf(909),_type:'epub'},h=harness(ReaderToolbar,[broken,valid,epub]);h.toolbar.start();h.tick();
  assert.equal(h.errors.length,1);assert.equal(valid._iframeWindow.document.buttons().length,1);
  assert.equal(epub._iframeWindow.document.buttons().length,0);h.toolbar.stop();
});

test('a dead wrapper from a destroyed iframe cannot block its replacement or stay retained',async()=>{
  const {ReaderToolbar}=await loadModule('toolbar.ts');const r=pdf(910),h=harness(ReaderToolbar,[r]);h.toolbar.start();
  let removals=0;
  r._iframeWindow.document.buttons()[0].remove=()=>{removals++;throw Error('dead object');};
  r._iframeWindow.document=documentTree();h.tick();
  assert.equal(r._iframeWindow.document.buttons().length,1);assert.equal(removals,1);
  h.tick();assert.equal(removals,1);
  r._iframeWindow.document.buttons()[0].remove=()=>{removals++;throw Error('dead object');};
  h.readers.length=0;h.tick();h.tick();assert.equal(removals,2);h.toolbar.stop();assert.equal(removals,2);
});

test('plugin start and stop wire lifecycle timer, events and menu-independent PDF mounting',async()=>{
  const reader=pdf(1010),registered=[],timers=new Map();let counter=0;
  globalThis.ChromeUtils={importESModule:name=>{assert.equal(name,'resource://gre/modules/Timer.sys.mjs');return{
    setInterval:(fn,ms)=>{const id=++counter;timers.set(id,fn);return id;},clearInterval:id=>timers.delete(id)};}};
  globalThis.Services={wm:{getEnumerator:()=>[]}};
  globalThis.Zotero={addShutdownListener(){},getMainWindows:()=>[],logError:e=>{throw e;},Reader:{_readers:[reader],
    registerEventListener:(...a)=>registered.push(a),unregisterEventListener:(kind,handler)=>{
      const index=registered.findIndex(x=>x[0]===kind&&x[1]===handler);if(index>=0)registered.splice(index,1);
    }}};
  const mod=await loadModule('main.ts');await mod.start();
  assert.equal(reader._iframeWindow.document.buttons()[0].textContent,'翻译');assert.equal(registered.length,2);assert.equal(timers.size,1);
  const late=pdf(1111);Zotero.Reader._readers.push(late);for(const tick of timers.values())tick();
  assert.equal(late._iframeWindow.document.buttons().length,1);
  await mod.stop();assert.equal(registered.length,0);assert.equal(timers.size,0);
  assert.equal(reader._iframeWindow.document.buttons().length,0);assert.equal(late._iframeWindow.document.buttons().length,0);
});
