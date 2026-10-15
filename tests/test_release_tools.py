"""Tests for the release tools (set_version, write_sums, release_sign, release_notes).

Run:  MSB_SOURCE=path/to/modpack_server_builder.py python -I tests/test_release_tools.py
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
import _msb  # noqa: E402
import release_notes  # noqa: E402
import release_sign  # noqa: E402
import set_version  # noqa: E402
import write_sums  # noqa: E402

APP = _msb.load_app()
SEED = hashlib.sha256(b"release tools test key").digest()


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_version(self):
        src = self.tmp / "app.py"
        shutil.copy(_msb.source_path(), src)
        vf = self.tmp / "build" / "version_info.txt"
        set_version.main(["v3.12.4", "--source", str(src), "--version-file", str(vf)])
        text = src.read_text(encoding="utf-8")
        self.assertIn('\nAPP_VERSION = "3.12.4"\n', text)
        self.assertEqual(text.count("\nAPP_VERSION = "), 1)
        info = vf.read_text(encoding="utf-8")
        self.assertIn("filevers=(3, 12, 4, 0)", info)
        self.assertIn("StringStruct('ProductVersion', '3.12.4')", info)
        try:
            from PyInstaller.utils.win32.versioninfo import load_version_info_from_text_file
        except Exception:  # noqa: BLE001  (PyInstaller is installed in CI, maybe not locally)
            load_version_info_from_text_file = None
        if load_version_info_from_text_file:
            self.assertIn("3.12.4", str(load_version_info_from_text_file(str(vf))))
        crlf = self.tmp / "crlf.py"
        crlf.write_bytes(b'X = 1\r\nAPP_VERSION = "1.0.0"\r\nY = 2\r\n')
        set_version.set_source_version(crlf, "2.0.0")
        self.assertEqual(crlf.read_bytes(), b'X = 1\r\nAPP_VERSION = "2.0.0"\r\nY = 2\r\n')
        for bad in ("3.1", "v3.1.0-rc1", "latest", "v1.2.70000"):
            with self.assertRaises(SystemExit):
                set_version.parse_version(bad)

    def test_sums_and_sign_agree_with_app(self):
        a, b = self.tmp / "ModpackServerBuilder-Setup.exe", self.tmp / "ModpackServerBuilder.exe"
        a.write_bytes(b"MZ" * 999)
        b.write_bytes(b"MZ" * 999)
        sums = self.tmp / "SHA256SUMS"
        data = write_sums.write_sums("v3.1.0", sums, [a, b])
        self.assertTrue(data.startswith(b"# ModpackServerBuilder 3.1.0\n"))
        self.assertNotIn(b"\r", data)
        real = APP.UPDATE_PUBKEY
        APP.UPDATE_PUBKEY = APP.ed25519_public_key(SEED).hex()
        try:
            sig = release_sign.sign_file(APP, sums, SEED)
            signed = APP.verify_signed_sums(sums.read_bytes(), sig.read_bytes().strip())
        finally:
            APP.UPDATE_PUBKEY = real
        self.assertEqual(signed["version"], "3.1.0")
        self.assertEqual(signed["files"]["ModpackServerBuilder-Setup.exe"], hashlib.sha256(b"MZ" * 999).hexdigest())
        if shutil.which("sha256sum"):
            r = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=self.tmp, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_sign_refuses_wrong_or_missing_key(self):
        sums = self.tmp / "SHA256SUMS"
        sums.write_bytes(b"# ModpackServerBuilder 3.1.0\n")
        env = {**os.environ, "MSB_SOURCE": str(_msb.source_path())}
        for key in ("", SEED.hex(), "zz"):                 # missing, not the release key, not hex
            env["UPDATE_SIGNING_KEY"] = key
            r = subprocess.run([sys.executable, "-I", str(TOOLS / "release_sign.py"), str(sums)], env=env,
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, key)
            self.assertFalse((self.tmp / "SHA256SUMS.sig").exists())

    def test_release_notes_changelog(self):
        cl = self.tmp / "CHANGELOG.md"
        cl.write_text("# Changelog\n\n## [3.1.0] - 2026-10-01\n- Auto updates\n- Faster\n\n## 3.0.0\n- First\n",
                      encoding="utf-8")
        self.assertEqual(release_notes.changelog_section("3.1.0", cl), "- Auto updates\n- Faster")
        self.assertEqual(release_notes.changelog_section("3.0.0", cl), "- First")
        self.assertEqual(release_notes.changelog_section("3.0.1", cl), "")
        self.assertEqual(release_notes.notes_for("v9.9.9-nope", cl), "Modpack Server Builder 9.9.9-nope")


if __name__ == "__main__":
    unittest.main(verbosity=2)
