"""Release notes for a tag: the annotated tag's message, else the CHANGELOG.md section, else a default.

    python tools/release_notes.py v3.1.0 --out build/release_notes.md
"""
import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*args) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        return r.stdout if r.returncode == 0 else ""
    except OSError:
        return ""


def tag_message(tag: str) -> str:
    if git("cat-file", "-t", f"refs/tags/{tag}").strip() != "tag":     # lightweight tag: no message
        return ""
    msg = git("tag", "-l", "--format=%(contents)", tag)
    msg = re.split(r"^-----BEGIN (?:PGP|SSH) SIGNATURE-----", msg, flags=re.M)[0]
    return msg.strip()


def changelog_section(version: str, path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    head = re.compile(r"^##[ \t]+\[?v?" + re.escape(version) + r"\]?(?=[ \t\r]|$)[^\n]*$", re.M)
    m = head.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    return (rest[:nxt.start()] if nxt else rest).strip()


def notes_for(tag: str, changelog: Path) -> str:
    version = tag.lstrip("vV")
    return (tag_message(tag) or changelog_section(version, changelog)
            or f"Minecraft Server Manager {version}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--out", required=True)
    ap.add_argument("--changelog", default=str(ROOT / "CHANGELOG.md"))
    a = ap.parse_args()
    notes = notes_for(a.tag, Path(a.changelog))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(notes + "\n", encoding="utf-8")
    print(notes)


if __name__ == "__main__":
    main()
