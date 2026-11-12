// Lookup by file content, independently of the attachment name and Zotero key.
// A changed translation configuration starts a new revision; old files remain.
export async function findArchiveRecord(data:any, key:string, hash:string, epoch:number,
  usable:(record:any)=>Promise<boolean>) {
  const records = [data[key], ...Object.values(data).filter(value=>value!==data[key])];
  for (const record of records as any[]) {
    if (record?.sourceHash!==hash || record.epoch!==epoch || !record.report?.outputHash) continue;
    if (await usable(record)) return record;
  }
  return null;
}
