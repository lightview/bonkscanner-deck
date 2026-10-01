"""Build out/bonkscanner-deck.zip in the layout Decky Loader installs from.

Run after `npm run build` (or just `npm run package`). The zip holds one
top-level folder, which becomes ~/homebrew/plugins/<folder> on the Deck.
"""

import json
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDER = "bonkscanner-deck"
FILES = ["plugin.json", "package.json", "main.py", "LICENSE", "README.md", "dist/index.js"]
DIRS = ["py_modules"]


def main() -> None:
    if not os.path.exists(os.path.join(ROOT, "dist", "index.js")):
        raise SystemExit("dist/index.js is missing -- run `npm run build` first.")
    version = json.load(open(os.path.join(ROOT, "package.json"), encoding="utf-8"))["version"]
    out_dir = os.path.join(ROOT, "out")
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, f"{FOLDER}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in FILES:
            zf.write(os.path.join(ROOT, rel), f"{FOLDER}/{rel}")
        for directory in DIRS:
            for base, dirnames, filenames in os.walk(os.path.join(ROOT, directory)):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for name in filenames:
                    path = os.path.join(base, name)
                    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                    zf.write(path, f"{FOLDER}/{rel}")
    print(f"Packaged {FOLDER} v{version} -> {target}")


if __name__ == "__main__":
    main()
