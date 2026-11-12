from pathlib import Path
import sys
import zipfile
source, output = map(Path, sys.argv[1:])
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
    for path in sorted(source.rglob("*")):
        if path.is_file():
            z.write(path, path.relative_to(source).as_posix())
print(output.name)
