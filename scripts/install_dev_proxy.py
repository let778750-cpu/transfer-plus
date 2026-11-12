"""Official extension proxy mechanism, restricted to an explicit test profile."""
import argparse
import json
from pathlib import Path
import shutil
import os
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser()
p.add_argument('--profile',type=Path,required=True)
p.add_argument('--pdf',type=Path,required=True)
p.add_argument('--translation',type=Path)
a=p.parse_args()
profile=a.profile.resolve()
if not (profile/'user.js').exists() or 'research-max-bilingual-dev' not in str(profile):
    raise SystemExit('Only the isolated development profile is accepted')
stage=profile.parent/'addon'
shutil.copytree(ROOT/'work'/'xpi',stage,dirs_exist_ok=True)
shutil.copy2(ROOT/'tests'/'zotero.integration.js',stage/'integration.js')
bootstrap=stage/'bootstrap.js'
bootstrap.write_text(bootstrap.read_text(encoding='utf-8')+'''
var productStartup = startup;
startup = async function(data) {
  try {
    await productStartup(data);
    await Zotero.uiReadyPromise;
    Services.scriptloader.loadSubScript(data.rootURI + 'integration.js', RMBContext);
  } catch(error) {
    await IOUtils.writeJSON(PathUtils.join(Zotero.Profile.dir,'startup-error.json'),{error:String(error),stack:error.stack});
  }
};
''',encoding='utf-8')
os.utime(stage,None)  # Gecko tracks unpacked extension-directory mtime.
(profile/'extensions').mkdir(exist_ok=True)
(profile/'extensions'/'zotero-bilingual@research-max.local').write_text(str(stage),encoding='utf-8')
prefs=profile/'prefs.js'
if prefs.exists():
    prefs.write_text('\n'.join(line for line in prefs.read_text(encoding='utf-8').splitlines()
                    if not any(k in line for k in ['extensions.lastAppBuildId','extensions.lastAppVersion'])),encoding='utf-8')
(profile/'rmb-test.json').write_text(json.dumps({'pdf':str(a.pdf),'translation':str(a.translation) if a.translation else None,
    'library':str(profile.parent/'library'),'report':str(profile.parent/'integration-report.json')}),encoding='utf-8')
print('Development extension proxy prepared')
