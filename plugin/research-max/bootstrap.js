/* Research Max Bilingual; AGPL-3.0-or-later */
var RMBContext;
var RMBChromeHandle;
function install() {}
async function startup(data) {
  await Zotero.initializationPromise;
  const { AddonManager } = ChromeUtils.importESModule('resource://gre/modules/AddonManager.sys.mjs');
  const addon = await AddonManager.getAddonByID(data.id);
  if (addon) addon.applyBackgroundUpdates = AddonManager.AUTOUPDATE_DISABLE;
  // XUL windows must load as registered chrome resources, not jar:file URLs.
  const registry = Components.classes['@mozilla.org/addons/addon-manager-startup;1']
    .getService(Components.interfaces.amIAddonManagerStartup);
  RMBChromeHandle = registry.registerChrome(Services.io.newURI(data.rootURI + 'manifest.json'), [
    ['content', 'research-max-bilingual', data.rootURI + 'content/']
  ]);
  RMBContext = {Zotero, Services, Components, ChromeUtils, IOUtils, PathUtils,
                rootURI: data.rootURI};
  Services.scriptloader.loadSubScript(data.rootURI + "content/main.js", RMBContext);
  await RMBContext.RMB.start();
}
async function onMainWindowLoad({window}) { await RMBContext?.RMB.addWindow(window); }
async function onMainWindowUnload({window}) { await RMBContext?.RMB.removeWindow(window); }
async function shutdown() {
  try { await RMBContext?.RMB.stop(); }
  finally { RMBContext = null; RMBChromeHandle?.destruct(); RMBChromeHandle = null; }
}
function uninstall() {}
