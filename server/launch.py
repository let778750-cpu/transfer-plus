"""Executable entry point used by Zotero's nsIProcess."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rmb.service import main
if __name__ == "__main__":
    main()
