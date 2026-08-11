"""
install_pending.py — generic step installer. Download this ONCE, keep it in
the project root, and reuse it for every future step's zip.

Usage (from the project root, with the zip in the same folder):

    python install_pending.py                     -> looks for pending_files.zip
    python install_pending.py step3_files.zip      -> or name any zip explicitly

What it does:
    - creates any folder the zip needs, however deep, automatically
    - writes every file from the zip into the project, preserving its
      internal path (src/db/models.py in the zip -> src/db/models.py here)
    - tells you which files were newly created vs overwritten
    - refuses to run if the zip would write outside the project (zip-slip
      protection — harmless here since I control the zip, but it's the
      correct default for any script that extracts an archive)
    - prints one clear success or failure statement at the end

No project imports, no venv required — this runs with a bare python3.11
before anything else is installed.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

DEFAULT_ZIP_NAME = "pending_files.zip"


def install(zip_path: Path, project_root: Path) -> int:
    if not zip_path.exists():
        print(f"FAILED: zip file not found: {zip_path}")
        print("        Place the zip in the same folder as this script, "
              "or pass its path as an argument.")
        return 1

    root = project_root.resolve()
    created: list[str] = []
    overwritten: list[str] = []
    dirs_touched: set[Path] = set()

    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad_entry = zf.testzip()
            if bad_entry is not None:
                print(f"FAILED: zip is corrupt at entry: {bad_entry}")
                print("        Re-download the zip and try again.")
                return 1

            for info in zf.infolist():
                if info.is_dir():
                    continue

                target = (root / info.filename).resolve()
                if root != target and root not in target.parents:
                    print(f"FAILED: unsafe path in zip, refusing to extract: {info.filename}")
                    return 1

                existed = target.exists()
                if not target.parent.exists():
                    dirs_touched.add(target.parent)
                target.parent.mkdir(parents=True, exist_ok=True)

                with zf.open(info) as src, open(target, "wb") as dst:
                    dst.write(src.read())

                rel = str(target.relative_to(root)).replace("\\", "/")
                (overwritten if existed else created).append(rel)

    except zipfile.BadZipFile:
        print(f"FAILED: {zip_path.name} is not a valid zip file.")
        return 1

    print()
    print("=" * 64)
    if dirs_touched:
        print(f"Folders created ({len(dirs_touched)}):")
        for d in sorted(dirs_touched):
            print(f"  + {d.relative_to(root)}")
        print()

    if created:
        print(f"Files created ({len(created)}):")
        for f in sorted(created):
            print(f"  + {f}")
    if overwritten:
        print(f"\nFiles overwritten ({len(overwritten)}):")
        for f in sorted(overwritten):
            print(f"  ~ {f}")

    total = len(created) + len(overwritten)
    print("=" * 64)
    print(f"SUCCESS: {total} file(s) installed into {root}")
    print("=" * 64)
    return 0


def main() -> int:
    project_root = Path(__file__).resolve().parent
    zip_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ZIP_NAME
    zip_path = Path(zip_name)
    if not zip_path.is_absolute():
        zip_path = project_root / zip_path
    return install(zip_path, project_root)


if __name__ == "__main__":
    raise SystemExit(main())
