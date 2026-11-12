// Report summaries only. Reading panes never mount supplemental text panels.
export function coverageText(report:any){
  const c=report.coverage;if(!c?.total)return '待译内容覆盖尚未统计';
  if(c.byKind){
    const percent=(v:any)=>v?.total?(v.missing?Math.floor(100*v.pdf/v.total):100):100;
    return `正文已写入 PDF ${percent(c.byKind.native)}% · 图中文字 ${percent(c.byKind.raster)}%`;
  }
  const done=Math.max(0,Math.min(100,Math.round(100*(c.pdf||0)/c.total)));
  return `${report.pipeline?.startsWith('paddle-cloud')?'待译内容':'正文'}已排入 PDF ${done}% · 未完成 ${100-done}%`;
}
export function reviewIssues(report:any){
  // Cloud reports use `type`, old engine reports use `types`. Normalize at the
  // reader boundary; report issues are authoritative over inferred unit states.
  const result:any[]=[];const byUnit=new Map<string,any>();const byRegion=new Map<string,any>();
  const add=(value:any,inferred=false)=>{
    if(!value||typeof value!=='object')return;
    const page=Number.isInteger(value.page)&&value.page>0?value.page:1;
    const region=Array.isArray(value.region)&&value.region.length===4&&value.region.every(Number.isFinite)
      &&value.region[2]>=value.region[0]&&value.region[3]>=value.region[1]?value.region:undefined;
    const types=[...new Set([...(Array.isArray(value.types)?value.types:[]),value.type,value.reason]
      .filter((t:any)=>typeof t==='string'&&t.trim()))];
    const unitId=typeof value.unitId==='string'?value.unitId:undefined;
    const key=JSON.stringify([page,region||null]);
    const existing=(unitId&&byUnit.get(unitId))||byRegion.get(key);
    if(existing){
      if(!inferred)existing.types=[...new Set([...existing.types,...types])];
      if(unitId)byUnit.set(unitId,existing);
      return;
    }
    const issue={page,region,unitId,types:types.length?types:['missing_content'],fallback:value.fallback||'region'};
    result.push(issue);byRegion.set(key,issue);if(unitId)byUnit.set(unitId,issue);
  };
  for(const issue of Array.isArray(report?.issues)?report.issues:[])add(issue);
  for(const u of Array.isArray(report?.reading?.units)?report.reading.units:[]){
    // Formula/identifier preservation is intentional, not a missing translation.
    if(!u||!['missing','supplement'].includes(u.status))continue;
    add({page:u.page,region:u.region,unitId:u.id,
      type:u.reason||(u.translation?'layout_pending':'no_chinese_translation'),fallback:'region'},true);
  }
  return result;
}
