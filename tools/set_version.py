"""Stamp a release version into the source and the exe's version resource.

    python tools/set_version.py v3.1.0 [--source modpack_server_builder.py] [--version-file build/version_info.txt]

Rewrites the single `APP_VERSION = "..."` line and writes a PyInstaller VSVersionInfo file.
Prints the bare version (3.1.0).
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "Minecraft Server Manager"
EXE_NAME = "ModpackServerBuilder"

VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=({t}), prodvers=({t}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{name}'),
      StringStruct('FileDescription', '{name}'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', '{exe}'),
      StringStruct('LegalCopyright', ''),
      StringStruct('OriginalFilename', '{exe}.exe'),
      StringStruct('ProductName', '{name}'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""

RX_APP_VERSION = re.compile(r'^APP_VERSION = "[^"\r\n]*"(?=[ \t]*\r?$)', re.M)


def parse_version(tag: str) -> str:
    m = re.fullmatch(r"(?:refs/tags/)?[vV]?(\d+)\.(\d+)\.(\d+)", tag.strip())
    if not m:
        raise SystemExit(f"Not a release version: {tag!r} (expected vX.Y.Z)")
    if any(int(x) > 65535 for x in m.groups()):
        raise SystemExit("version parts must be <= 65535 (Windows version resource limit)")
    return ".".join(str(int(x)) for x in m.groups())


def set_source_version(src: Path, version: str):
    text = src.read_bytes().decode("utf-8")              # no newline translation: keep CRLF/LF as is
    hits = RX_APP_VERSION.findall(text)
    if len(hits) != 1:
        raise SystemExit(f"{src}: expected exactly one APP_VERSION line, found {len(hits)}")
    new = RX_APP_VERSION.sub(f'APP_VERSION = "{version}"', text)
    with open(src, "w", encoding="utf-8", newline="") as f:      # keep the file's own line endings
        f.write(new)


def version_info_text(version: str) -> str:
    t = ", ".join(version.split(".") + ["0"])
    return VERSION_INFO.format(t=t, v=version, name=APP_NAME, exe=EXE_NAME)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--source", default=str(ROOT / "modpack_server_builder.py"))
    ap.add_argument("--version-file", default=str(ROOT / "build" / "version_info.txt"))
    a = ap.parse_args(argv)
    version = parse_version(a.tag)
    set_source_version(Path(a.source), version)
    vf = Path(a.version_file)
    vf.parent.mkdir(parents=True, exist_ok=True)
    vf.write_text(version_info_text(version), encoding="utf-8")
    print(version)


if __name__ == "__main__":
    main(sys.argv[1:])
