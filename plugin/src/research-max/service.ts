const PREF = "extensions.zotero.research-max-bilingual.";
const REQUIRED_RUNTIME="0.4.0";
function runtimeMismatch(){const error=new Error(`本机翻译服务版本不匹配。请先安装 ${REQUIRED_RUNTIME} 配套运行服务，再重启 Zotero。`);error.name='RMBRuntimeMismatch';return error;}
async function checkedEndpoint(value:any){
  const response=await Zotero.HTTP.request('GET',value.url+'/health',{headers:{'X-RMB-Token':value.token},responseType:'json',timeout:1500});
  const health=response.response;
  if(health?.version!==REQUIRED_RUNTIME||!health.capabilities?.includes('resume-checkpoint-v1'))throw runtimeMismatch();
  return value;
}
let endpoint: any, process: any, starting: any;
export let installation: any;
export async function locate() {
  if (installation) return installation;
  const override = Zotero.Prefs.get(PREF + "runtime", true);
  const file = override || PathUtils.join(Services.env.get("LOCALAPPDATA"), "ResearchMaxBilingual", "install.json");
  let candidate:any;
  try { candidate = await IOUtils.readJSON(file); }
  catch { throw new Error("尚未部署本机服务。请先运行安装包中的 install-runtime.ps1。"); }
  const active = await IOUtils.readJSON(PathUtils.join(candidate.root, "active.json"));
  if(active.version!==REQUIRED_RUNTIME)throw runtimeMismatch();
  candidate.versionRoot = PathUtils.join(candidate.root, "versions", active.version);
  candidate.data = candidate.data || PathUtils.join(candidate.root, "data");
  return installation=candidate;
}
export async function ensureService() {
  if (endpoint) return endpoint;
  if (starting) return starting;
  starting = (async () => {
    const install = await locate();
    const file = PathUtils.join(install.data, "config", `endpoint-${Services.appinfo.processID}.json`);
    await IOUtils.makeDirectory(PathUtils.parent(file), {ignoreExisting: true});
    // Only an endpoint for this Zotero PID may be reused.
    try {
      const old = await IOUtils.readJSON(file);
      return endpoint=await checkedEndpoint(old);
    } catch(error:any) {if(error?.name==='RMBRuntimeMismatch')throw error;}
    const executable = Zotero.File.pathToFile(PathUtils.join(install.versionRoot, "python", "pythonw.exe"));
    process = Components.classes["@mozilla.org/process/util;1"].createInstance(Components.interfaces.nsIProcess);
    process.init(executable);
    const args = ["-I", "-B", PathUtils.join(install.versionRoot, "server", "launch.py"),
                  "--data-dir", install.data, "--endpoint-file", file,
                  "--parent-pid", String(Services.appinfo.processID)];
    process.runwAsync(args, args.length, null, false);
    for (let i=0; i<100; i++) {
      await Zotero.Promise.delay(200);
      try {
        const value = await IOUtils.readJSON(file);
        if (value.pid === process.pid) return endpoint=await checkedEndpoint(value);
      } catch(error:any) {if(error?.name==='RMBRuntimeMismatch')throw error;}
      if (!process.isRunning) throw new Error("本机服务启动失败，请运行诊断脚本。");
    }
    throw new Error("本机服务启动超时");
  })();
  try { return await starting; } finally { starting = null; }
}
export async function api(path: string, data?: any) {
  const ep = await ensureService();
  let response;
  try { response = await Zotero.HTTP.request(data === undefined ? "GET" : "POST", ep.url + path, {
    headers: {"X-RMB-Token": ep.token, "Content-Type": "application/json"},
    body: data === undefined ? undefined : JSON.stringify(data), responseType: "json", timeout: path==='/translate'||path==='/api/config/test-paddle'?120000:15000,
    successCodes: false
  }); } catch (error) {
    endpoint = null; // Next operation checks/restarts the process; never replay a POST.
    throw error;
  }
  if (response.status >= 400) throw new Error(response.response?.error || `本机服务错误 ${response.status}`);
  return response.response;
}
export async function stopService() {
  if (endpoint) {
    const old=endpoint;
    try {
      await api("/shutdown", {});
      for(let i=0;i<10;i++) {
        await Zotero.Promise.delay(100);
        try {await Zotero.HTTP.request("GET",old.url+"/health",{headers:{"X-RMB-Token":old.token},timeout:200});}
        catch {break;}
      }
    } catch {}
  }
  endpoint = null;
  process = null;
}
export async function download(taskID: string) {
  // Read the validated permanent archive: another window may already have
  // finished importing and cleaned this shared task's temporary directory.
  if (!/^[0-9a-f]{32}$/.test(taskID)) throw new Error("任务编号无效");
  return (await api(`/api/tasks/${taskID}/result`)).path;
}
