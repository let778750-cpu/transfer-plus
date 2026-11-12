// A registered chrome document gets the XUL principal and native window sizing.
export const SETTINGS_URL='chrome://research-max-bilingual/content/settings.xhtml';
export const SETTINGS_NAME='research-max-bilingual-settings';
export function openSettingsWindow(host:any,render:(win:any)=>Promise<void>,onError:(error:any)=>void){
  const win=host.openDialog(SETTINGS_URL,SETTINGS_NAME,
    'chrome,dialog=no,resizable,centerscreen,width=760,height=760');
  let initialized=false;
  const initialize=()=>{
    if(initialized||win.closed||win.document?.documentURI!==SETTINGS_URL)return;
    initialized=true;win.removeEventListener('load',initialize);
    win.document.title='中文对照设置';
    Promise.resolve().then(()=>render(win)).catch(error=>{
      onError(error);
      const message=win.document.getElementById('message');
      if(message)message.textContent='设置加载失败：'+(error?.message||String(error));
    });
  };
  win.addEventListener('load',initialize);
  if(win.document?.readyState==='complete')initialize();
  return win;
}
