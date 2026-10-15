"""Write SHA256SUMS (sha256sum format, LF endings) with a signed version header.

    python tools/write_sums.py --version 3.1.0 --out dist/SHA256SUMS dist/A.exe dist/B.exe

The first line "# ModpackServerBuilder 3.1.0" is covered by the signature, so an old signed list can't be
passed off as a newer release. `sha256sum -c SHA256SUMS` ignores it.
"""
import argparse
import hashlib
import re
from pathlib import Path


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sums(version: str, out: Path, files) -> bytes:
    version = version.lstrip("vV")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"bad version {version!r}")
    lines = [f"# ModpackServerBuilder {version}"]
    for f in files:
        f = Path(f)
        lines.append(f"{sha256_file(f)}  {f.name}")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    out.write_bytes(data)
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    print(write_sums(a.version, Path(a.out), a.files).decode(), end="")


if __name__ == "__main__":
    main()
