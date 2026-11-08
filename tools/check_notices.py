"""Checks that THIRD-PARTY-NOTICES has an entry for every package the last build bundled.

Run it after scripts/build.sh (package.sh runs that too): it reads PyInstaller's record of
what went into the app. Python itself, and the OpenSSL it brings, aren't from site-packages,
so they aren't listed here; their entry is checked by eye.

    python3 tools/check_notices.py
"""

import ast
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOC = Path.home() / "Library/Caches/MacEngineRoom-build/build/MacEngineRoom/Analysis-00.toc"


def entries(toc):
    """Every (name, source, kind) in PyInstaller's nested table of contents."""
    if isinstance(toc, (list, tuple)):
        if len(toc) == 3 and all(isinstance(item, str) for item in toc):
            yield toc
        else:
            for item in toc:
                yield from entries(item)


def project(name, source, distributions):
    """The name the package's entry goes under: PyObjC's modules share one, as do
    PyInstaller's run-time hooks."""
    if "/PyInstaller/" in source:
        return "PyInstaller"
    top = name.replace("/", ".").split(".")[0]
    dist = distributions.get(top, [top])[0]
    return "PyObjC" if dist.lower().startswith("pyobjc") else dist


def key(text):
    return text.strip().lower().replace("-", "_")


def main():
    if not TOC.exists():
        sys.exit(f"No build found at {TOC.parent}: run scripts/build.sh first.")
    distributions = packages_distributions()
    bundled = {project(name, source, distributions)
               for name, source, _ in entries(ast.literal_eval(TOC.read_text(encoding="utf-8")))
               if "/site-packages/" in source}
    headings = {key(line) for line in (ROOT / "THIRD-PARTY-NOTICES").read_text(encoding="utf-8").splitlines()}
    missing = sorted(p for p in bundled if key(p) not in headings)
    print("Bundled:", ", ".join(sorted(bundled)))
    if missing:
        sys.exit("No entry in THIRD-PARTY-NOTICES for: " + ", ".join(missing)
                 + ". Add its notice, or exclude it in the spec if the app doesn't need it.")
    print("THIRD-PARTY-NOTICES covers them all.")


if __name__ == "__main__":
    main()
