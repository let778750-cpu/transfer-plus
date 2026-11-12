/* Runs only from the separate developer extension, never packaged for release. */
(async function () {
  const file = PathUtils.join(Zotero.Profile.dir, "rmb-test.json");
  const config = await IOUtils.readJSON(file);
  const result = {version: Zotero.version, profile: Zotero.Profile.dir, checks: []};
  const save = () => IOUtils.writeJSON(config.report, result);
  const check = (name, value) => { result.checks.push({name, passed: !!value}); if(!value)throw new Error(name); };
  try {
    await Zotero.uiReadyPromise;
    check("separate library", Zotero.DataDirectory.dir === config.library);
    const parent = new Zotero.Item("journalArticle");
    parent.setField("title", "Research Max isolated integration fixture");
    await parent.saveTx();
    const left = await Zotero.Attachments.importFromFile({file: config.pdf, parentItemID: parent.id});
    const right = await Zotero.Attachments.importFromFile({file: config.translation || config.pdf, parentItemID: parent.id});
    result.attachments = [left.key, right.key]; await save();
    const state = await Zotero.ResearchMaxBilingual.openPair(Zotero.getMainWindow(), left, right, {issues: []});
    check("left reader ready", state.leftAdapter.ready);
    check("right reader ready", state.rightAdapter.ready);
    check("left scroll container", state.leftAdapter.getScrollContainer());
    check("right scroll container", state.rightAdapter.getScrollContainer());
    result.dimensions = [state.leftBrowser.getBoundingClientRect().width, state.rightBrowser.getBoundingClientRect().width];
    check("two visible panes", result.dimensions.every(x=>x>=200));
    state.leftAdapter.setScrollFraction(.32);
    await Zotero.Promise.delay(900);
    const fraction = state.rightAdapter.getScrollFraction();
    result.scrollFraction = fraction;
    check("scroll synchronization", Math.abs(fraction-.32)<.025);
    state.syncPaused = true;
    state.leftAdapter.setScrollFraction(.6);
    await Zotero.Promise.delay(900);
    check("independent reading", Math.abs(state.rightAdapter.getScrollFraction()-fraction)<.025);
    state.syncPaused = false;
    for (const [side, item] of [["left",left],["right",right]]) {
      const reader=state[side+"Browser"].contentWindow.wrappedJSObject._reader;
      result[side+"ReaderKeys"]=Object.keys(reader);
      result[side+"AnnotationManagerKeys"]=Object.keys(reader._annotationManager||{});
      const annotation={id:Zotero.DataObjectUtilities.generateKey(),type:"highlight",color:"#ffd400",text:"integration annotation",comment:side,
        pageLabel:"1",sortIndex:"00000|000001|00000",position:{pageIndex:0,rects:[[72,700,180,712]]},tags:[]};
      if(reader._annotationManager?.addAnnotation){
        reader._annotationManager.addAnnotation(annotation);
        await Zotero.Promise.delay(2000);
        const persisted=Zotero.Items.getByLibraryAndKey(item.libraryID,annotation.id);
        check(side+" annotation saved to correct attachment",persisted?.parentID===item.id);
      }
      else check(side+" annotation API available",false);
    }
    result.completed=true;
  } catch (error) { result.error=String(error);result.stack=error.stack; }
  await save();
})();
