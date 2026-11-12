"""Prepare a separate Zotero profile; never open the daily database directly."""
import argparse
import json
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument("--root", type=Path, required=True)
p.add_argument("--runtime", type=Path, required=True)
args=p.parse_args()
profile=args.root / "profile"
profile.mkdir(parents=True, exist_ok=True)
(args.root / "library").mkdir(exist_ok=True)
registry=args.root / "install.json"
registry.write_text(json.dumps({"root":str(args.runtime),"data":str(args.root / "data")}),encoding="utf-8")
prefs={"extensions.zotero.dataDir":str(args.root / "library"),
       "extensions.zotero.useDataDir":True,"extensions.zotero.firstRun":False,
       "extensions.zotero.research-max-bilingual.runtime":str(registry),
       "extensions.zotero.debug.store":True,
       "extensions.zotero.automaticScraperUpdates":False,
       "extensions.zotero.sync.autoSync":False}
(profile / "user.js").write_text("\n".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k,v in prefs.items()),encoding="utf-8")
print(profile)
