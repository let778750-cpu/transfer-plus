import * as esbuild from "esbuild";
import fs from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {execFileSync} from "node:child_process";
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),"..");
const {version}=JSON.parse(await fs.readFile(path.join(root,"plugin","research-max","manifest.json"),"utf8"));
const stage=path.join(root,"work","xpi");
await fs.mkdir(path.join(stage,"content"),{recursive:true});
for(const name of ["bootstrap.js","manifest.json"])await fs.copyFile(path.join(root,"plugin","research-max",name),path.join(stage,name));
await fs.copyFile(path.join(root,"plugin","research-max","settings.xhtml"),path.join(stage,"content","settings.xhtml"));
await fs.cp(path.join(root,"licenses"),path.join(stage,"licenses"),{recursive:true});
await esbuild.build({entryPoints:[path.join(root,"plugin","src","research-max","main.ts")],
  outfile:path.join(stage,"content","main.js"),bundle:true,format:"iife",globalName:"RMB",target:"firefox128",tsconfigRaw:{compilerOptions:{target:"ES2022"}},
  plugins:[{name:"rmb-localization",setup(build){build.onResolve({filter:/utils\/locale$/},()=>({path:path.join(root,"plugin","src","research-max","locale.ts")}));}}]});
await fs.mkdir(path.join(root,"dist"),{recursive:true});
execFileSync(process.env.RMB_BUILD_PYTHON||"python",[path.join(root,"scripts","package_xpi.py"),stage,path.join(root,"dist",`research-max-bilingual-${version}.xpi`)],{stdio:"inherit"});
