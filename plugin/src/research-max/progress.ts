export type Outcome='completed'|'cancelled'|'skipped'|'failed';
type Timers={setTimeout:(fn:()=>void,ms:number)=>any;clearTimeout:(id:any)=>void};
export type Notice={update:(percent:number,message:string)=>void;
  finish:(outcome:Outcome,message:string)=>void;close:()=>void};

/** One-time migration: older releases did not retain the cancelled popup.
 * Match its exact window URL, headline and message; leave all other UI alone.
 */
export function closeLegacyCancelledNotices(windows:Iterable<any>,remove:(win:any)=>void,log:(e:any)=>void){
  for(const win of windows){try{
    const doc=win.document;
    if(doc.documentURI!=='chrome://zotero/content/progressWindow.xhtml')continue;
    const headline=doc.getElementById('zotero-progress-text-headline');
    const title=Array.from(headline?.children||[],(el:any)=>el.getAttribute('value')||el.textContent||'').join('');
    if(title!=='生成中文对照')continue;
    const text=doc.getElementById('zotero-progress-text-box')?.textContent?.replace(/\s+/g,'');
    if(text!=='已取消，进度已保留；再次点击翻译即可继续')continue;
    remove(win);win.close();
  }catch(e){log(e);}}
}

/** Own only this plugin's notices, independently of the popup's hover timer. */
export class TranslationNotices {
  private notices=new Map<number,Notice>();
  constructor(private create:()=>any,private timers:Timers,private log:(e:any)=>void) {}

  close(itemID:number){this.notices.get(itemID)?.close();}
  closeAll(){for(const notice of [...this.notices.values()])notice.close();}

  show(itemID:number,title:string):Notice {
    this.close(itemID);
    const popup=this.create();let line:any,timer:any,closed=false,finished=false;
    const nativeClose=popup.close.bind(popup);
    const safe=(work:()=>void)=>{try{work();}catch(e){this.log(e);}};
    const notice:Notice={
      update:(percent,message)=>{if(!closed&&!finished)safe(()=>{line.setProgress(percent);line.setText(message);});},
      finish:(outcome,message)=>{
        if(closed||finished)return;finished=true;
        // Gecko's ProgressWindow.startCloseTimer is cancelled by mouseover.
        // Use a module timer, so hover and missing mouseout cannot pin a notice.
        timer=this.timers.setTimeout(()=>notice.close(),3500);
        safe(()=>{
          popup.changeHeadline({completed:'翻译完成',cancelled:'已取消翻译',skipped:'已跳过翻译',failed:'翻译未完成'}[outcome]);
          line.setProgress(100); // remove the partial-progress indicator
          if(outcome==='failed')line.setError();
          line.setText(message);
        });
      },
      close:()=>{
        if(closed)return;closed=true;
        if(timer!==undefined)this.timers.clearTimeout(timer);
        if(this.notices.get(itemID)===notice)this.notices.delete(itemID);
        safe(nativeClose);
      }
    };
    // Native click/parent-window closure must release our ownership too.
    // This wraps only our instance, never Zotero's prototype or global set.
    popup.close=notice.close;this.notices.set(itemID,notice);
    try{
      popup.changeHeadline('生成中文对照');
      line=new popup.ItemProgress('',title);popup.show();
    }catch(e){notice.close();throw e;}
    return notice;
  }
}
