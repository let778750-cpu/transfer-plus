"""Commit validated page artwork without replacing page identity or links."""
def copy_artwork(document, index, trial):
    # Scratch pages never need interactive links, annotations or form widgets.
    trial.insert_pdf(document, from_page=index, to_page=index,
                     links=False, annots=False, widgets=False)


def commit_artwork(document, index, trial):
    target = document.page_xref(index)
    appended = len(document)
    document.insert_pdf(trial, from_page=0, to_page=0, start_at=appended,
                        links=False, annots=False, widgets=False)
    imported = document.page_xref(appended)
    previous = {key:document.xref_get_key(target,key)[1] for key in ('Contents','Resources')}
    try:
        for key in previous:
            document.xref_set_key(target,key,document.xref_get_key(imported,key)[1])
    except Exception:
        for key,value in previous.items():document.xref_set_key(target,key,value)
        raise
    finally:
        document.delete_page(appended)
    # The original page xref, /Annots, outline targets and page labels survive.
    document.reload_page(document[index])
