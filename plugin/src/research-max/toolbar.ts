import {bindSingleClick, installButtonStyle} from './taskControls';

const BUTTONS='.research-max-bilingual-button,button[title="生成或打开中文对照 PDF"],button[title="优先打开本机存档；没有匹配译文时开始翻译"]';
// Zotero 10's Toolbar places CustomSections immediately before Appearance.
const SLOT='.toolbar .end .custom-sections';
type Entry={doc:any; button:any};
type Host={readers:()=>any[]; register:(handler:any)=>void; unregister:(handler:any)=>void;
  repeat:(callback:()=>void,ms:number)=>any; clear:(timer:any)=>void; error:(error:any)=>void};

/** One owned button per live native PDF reader; never depends on a paper key.
 * renderToolbar is the immediate path. A cheap document/slot check also handles
 * hot installs, restored tabs, late iframes and missed/replaced toolbar mounts.
 * It does no file IO, translation or network work.
 */
export class ReaderToolbar {
  private entries=new Map<any,Entry>();
  private errors=new Set<any>();
  private active=false;
  private timer:any;
  constructor(private host:Host, private action:(itemID:number)=>void,
    private update:(button:any,itemID:number)=>void) {}

  readonly render=(event:any)=>{
    if(!this.active||event.reader?._type!=='pdf')return;
    this.attempt(event.reader,()=>this.ensure(event.reader,event.doc,event.append));
  };

  start() {
    if(this.active)return;
    this.active=true;
    this.host.register(this.render);
    this.sweep();
    this.timer=this.host.repeat(()=>this.sweep(),1000);
  }

  private attempt(reader:any,work:()=>void) {
    try {work();this.errors.delete(reader);}
    catch(error) {if(!this.errors.has(reader)){this.errors.add(reader);this.host.error(error);}}
  }

  private ensure(reader:any,doc:any,append?:(node:any)=>void) {
    if(!doc||reader._isUninitialized||reader._isTabClosed)return;
    const slot=doc.querySelector(SLOT);
    if(!slot&&!append)return; // iframe or toolbar is still loading
    let entry=this.entries.get(reader);
    if(entry&&entry.doc!==doc){
      const old=entry;this.entries.delete(reader);entry=undefined;
      // A destroyed Gecko document can leave a dead wrapper. Its cleanup must
      // not prevent the replacement document from acquiring its own button.
      this.attempt(reader,()=>old.button.remove());
    }
    if(!entry){
      // Remove only our stale nodes, including instances from pre-0.1.2.
      for(const old of doc.querySelectorAll(BUTTONS))old.remove();
      installButtonStyle(doc);
      const button=doc.createElementNS('http://www.w3.org/1999/xhtml','button');
      button.type='button';button.className='toolbar-button research-max-bilingual-button';
      const itemID=reader.itemID;
      bindSingleClick(button,()=>{if(this.active)this.action(itemID);});
      entry={doc,button};this.entries.set(reader,entry);
      this.update(button,itemID);
    }
    // Reuse our handler after a React remount; no duplicate buttons/listeners.
    if(!entry.button.isConnected||!slot?.contains(entry.button)){
      if(append)append(entry.button);
      else slot.appendChild(entry.button);
    }
  }

  sweep() {
    if(!this.active)return;
    const readers=new Set(this.host.readers());
    for(const [reader,entry] of this.entries){
      this.attempt(reader,()=>{
        if(!readers.has(reader)||reader._isUninitialized||reader._isTabClosed){
          this.entries.delete(reader);this.errors.delete(reader);entry.button.remove();
        }
      });
    }
    for(const reader of readers){
      this.attempt(reader,()=>{
        if(reader._type==='pdf')this.ensure(reader,reader._iframeWindow?.document);
      });
    }
    for(const reader of this.errors)if(!readers.has(reader))this.errors.delete(reader);
  }

  refresh() {
    for(const [reader,entry] of this.entries)
      this.attempt(reader,()=>this.update(entry.button,reader.itemID));
  }

  stop() {
    this.active=false;
    this.host.unregister(this.render);
    if(this.timer!==undefined)this.host.clear(this.timer);
    this.timer=undefined;
    for(const [reader,entry] of this.entries)this.attempt(reader,()=>{
      entry.button.remove();entry.doc.getElementById('research-max-translate-style')?.remove();
    });
    this.entries.clear();this.errors.clear();
  }
}
