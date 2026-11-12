// Commit new bytes to the same Zotero attachment, with a verified local backup.
export async function replaceAttachment(item:any,file:string,record:any,report:any,job:string,
  deps:{hash:(path:string)=>string;api:(path:string,data:any)=>Promise<any>;close:(id:number)=>Promise<void>;commit?:()=>Promise<void>;journal?:{path:string;record:any}}) {
  const path=await item.getFilePathAsync();
  if(item.key===record.sourceKey?.split(':').pop() || deps.hash(path)!==record.report.outputHash)
    throw new Error('已有译文已改变，未覆盖文件');
  await deps.close(item.id);
  const annotations=item.getAnnotations().map((a:any)=>({key:a.key,position:a.annotationPosition,
    text:a.annotationText,comment:a.annotationComment,color:a.annotationColor,type:a.annotationType}));
  const prepared=await deps.api(`/api/tasks/${job}/prepare-update`,{path,outputHash:record.report.outputHash,
    sourceHash:report.sourceHash,attachmentKey:item.key,libraryID:item.libraryID,annotations,report:record.report});
  if(deps.hash(path)!==record.report.outputHash || deps.hash(prepared.backupPath)!==record.report.outputHash)
    throw new Error('替换前文件校验失败');
  if(prepared.report){Object.assign(report,prepared.report);if(deps.journal)deps.journal.record.report=report;}
  file=prepared.resultPath||file;
  let changed=false;
  if(deps.journal)await IOUtils.writeJSON(deps.journal.path,{path,oldHash:record.report.outputHash,newHash:report.outputHash,record:deps.journal.record},
    {tmpPath:deps.journal.path+'.tmp',flush:true});
  try {
    const bytes=await IOUtils.read(file);
    await IOUtils.write(path,bytes,{tmpPath:path+'.rmb-update.tmp',flush:true});changed=true;
    if(deps.hash(path)!==report.outputHash)throw new Error('更新后的译文校验失败');
    // Zotero owns item identity, sync state, timestamps and notifications.
    item.attachmentSyncState='to_upload';await item.saveTx();
    await item.relinkAttachmentFile(path);
    await deps.commit?.();
  } catch(error) {
    if(changed)await IOUtils.write(path,await IOUtils.read(prepared.backupPath),{tmpPath:path+'.rmb-rollback.tmp',flush:true});
    // A journal whose target has the old hash is safely discarded on retry.
    throw error;
  }
  // Cleanup failure must not roll back a committed PDF while its mapping is new.
  if(deps.journal)try{await IOUtils.remove(deps.journal.path,{ignoreAbsent:true});}catch(error){Zotero.logError(error);}
  return item;
}
