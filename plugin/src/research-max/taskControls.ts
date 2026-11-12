export type Task = {itemID:number; jobID?:string; cancelRequested:boolean;
  phase:'running'|'cancelling'|'delivering'; promise?:Promise<any>};
export class TaskControls {
  tasks=new Map<number,Task>();
  constructor(private changed:()=>void, private sendCancel:(id:string)=>Promise<any>) {}
  start(itemID:number, work:(task:Task)=>Promise<any>) {
    const existing=this.tasks.get(itemID);if(existing)return existing.promise!;
    const task:Task={itemID,cancelRequested:false,phase:'running'};
    this.tasks.set(itemID,task);this.changed();
    task.promise=Promise.resolve().then(()=>work(task)).finally(()=>{
      if(this.tasks.get(itemID)===task)this.tasks.delete(itemID);this.changed();
    });
    return task.promise;
  }
  async bindJob(task:Task, jobID:string) {
    task.jobID=jobID;
    if(task.cancelRequested)await this.sendCancel(jobID);
  }
  async cancel(itemID:number) {
    const task=this.tasks.get(itemID);
    if(!task||task.phase==='delivering'||task.phase==='cancelling')return;
    task.cancelRequested=true;task.phase='cancelling';this.changed();
    try {if(task.jobID)await this.sendCancel(task.jobID);}
    catch(error){task.phase='running';this.changed();throw error;}
  }
  deliver(task:Task) {
    if(task.cancelRequested)return false;
    task.phase='delivering';this.changed();return true;
  }
}
export function taskButtonState(task?:Task) {
  if(!task)return {label:'翻译',disabled:false,title:'检查本机存档，复用已有译文并补充缺漏'};
  if(task.phase==='delivering')return {label:'正在打开…',disabled:true,title:'完整译文已生成，正在导入并打开'};
  if(task.phase==='cancelling')return {label:'正在取消…',disabled:true,title:'正在停止任务并保留已完成的进度'};
  return {label:'取消翻译',disabled:false,title:'停止当前论文的翻译，保留进度供下次继续'};
}
export function bindSingleClick(button:any, action:()=>void) {
  // The toolbar is a native window drag region. CSS no-drag, not interception
  // of pointerdown, makes this a normal clickable HTML button in Gecko.
  button.addEventListener('click',(e:any)=>{
    e.preventDefault();e.stopPropagation();
    // A physical double-click must not start and immediately cancel the task.
    if(button.disabled||e.detail>1||(e.button!==undefined&&e.button!==0))return;
    action();
  },true);
}
export function installButtonStyle(doc:any) {
  const existing=doc.getElementById('research-max-translate-style');
  const style=existing||doc.createElementNS('http://www.w3.org/1999/xhtml','style');style.id='research-max-translate-style';
  style.textContent=`.research-max-bilingual-button {-moz-window-dragging:no-drag!important;appearance:none!important;border:0!important;border-radius:5px!important;background:transparent!important;color:inherit!important;cursor:pointer!important;padding:5px 8px!important;margin:0 4px!important;font:inherit!important;width:auto!important;min-width:54px;transition:background-color .12s ease;}
  .research-max-bilingual-button:hover:not(:disabled) {background:rgba(128,128,128,.18)!important;}
  .research-max-bilingual-button:active:not(:disabled) {background:rgba(128,128,128,.32)!important;}
  .research-max-bilingual-button:focus-visible {outline:2px solid #4285d4!important;outline-offset:1px;}
  .research-max-bilingual-button:disabled {opacity:.65;cursor:default!important;}`;
  if(!existing)(doc.head||doc.documentElement).appendChild(style);
}
