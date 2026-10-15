"""Tests for the self-updater (Ed25519, GitHub check, download/verify, AutoUpdater, finish-update).

Run:  MSB_SOURCE=path/to/modpack_server_builder.py python -I test_updater.py
If the module doesn't contain the updater yet, updater_part.py (MSB_UPDATER_PART, or the file next to this
test) is exec'd into its namespace, the same way it will be pasted in. Never touches the real network.
"""
import hashlib
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="msb_upd_test_"))
os.environ["MSB_HOME"] = str(TMP / "home")
os.environ["LOCALAPPDATA"] = str(TMP / "localappdata")
os.environ["MSB_UPDATE_TEST"] = "1"
for k in ("NO_PROXY", "no_proxy"):
    os.environ[k] = ",".join(x for x in (os.environ.get(k), "127.0.0.1", "localhost") if x)


def load_module():
    cands = [os.environ.get("MSB_SOURCE"), HERE.parent / "modpack_server_builder.py", "/tmp/outputs/modpack_server_builder.py"]
    src = next(Path(c) for c in cands if c and Path(c).is_file())
    spec = importlib.util.spec_from_file_location("msb_under_test", src)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    if not hasattr(m, "UPDATE_PUBKEY"):
        part = Path(os.environ.get("MSB_UPDATER_PART") or HERE / "updater_part.py")
        exec(compile(part.read_text(encoding="utf-8"), str(part), "exec"), m.__dict__)
    return m


M = load_module()
M._UPDATE_RETRY_DELAY = 0.01
M._FINISH_RETRY_DELAY = 0.01
REAL_PUBKEY = M.UPDATE_PUBKEY

# RFC 8032 section 7.1 (TEST 1, 2, 3, 1024): seed, public key, message, signature (hex)
RFC8032_VECTORS = [
    ('TEST 1',
     '9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60',
     'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a',
     '',
     'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b'),
    ('TEST 2',
     '4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb',
     '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c',
     '72',
     '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00'),
    ('TEST 3',
     'c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7',
     'fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025',
     'af82',
     '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a'),
    ('TEST 1024',
     'f5e5767cf153319517630f226876b86c8160cc583bc013744c6bf255f5cc0ee5',
     '278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e',
     (
      '08b8b2b733424243760fe426a4b54908632110a66c2f6591eabd3345e3e4eb98fa6e264bf09efe12ee50f8f54e9f77b1'
      'e355f6c50544e23fb1433ddf73be84d879de7c0046dc4996d9e773f4bc9efe5738829adb26c81b37c93a1b270b20329d'
      '658675fc6ea534e0810a4432826bf58c941efb65d57a338bbd2e26640f89ffbc1a858efcb8550ee3a5e1998bd177e93a'
      '7363c344fe6b199ee5d02e82d522c4feba15452f80288a821a579116ec6dad2b3b310da903401aa62100ab5d1a36553e'
      '06203b33890cc9b832f79ef80560ccb9a39ce767967ed628c6ad573cb116dbefefd75499da96bd68a8a97b928a8bbc10'
      '3b6621fcde2beca1231d206be6cd9ec7aff6f6c94fcd7204ed3455c68c83f4a41da4af2b74ef5c53f1d8ac70bdcb7ed1'
      '85ce81bd84359d44254d95629e9855a94a7c1958d1f8ada5d0532ed8a5aa3fb2d17ba70eb6248e594e1a2297acbbb39d'
      '502f1a8c6eb6f1ce22b3de1a1f40cc24554119a831a9aad6079cad88425de6bde1a9187ebb6092cf67bf2b13fd65f270'
      '88d78b7e883c8759d2c4f5c65adb7553878ad575f9fad878e80a0c9ba63bcbcc2732e69485bbc9c90bfbd62481d9089b'
      'eccf80cfe2df16a2cf65bd92dd597b0707e0917af48bbb75fed413d238f5555a7a569d80c3414a8d0859dc65a46128ba'
      'b27af87a71314f318c782b23ebfe808b82b0ce26401d2e22f04d83d1255dc51addd3b75a2b1ae0784504df543af8969b'
      'e3ea7082ff7fc9888c144da2af58429ec96031dbcad3dad9af0dcbaaaf268cb8fcffead94f3c7ca495e056a9b47acdb7'
      '51fb73e666c6c655ade8297297d07ad1ba5e43f1bca32301651339e22904cc8c42f58c30c04aafdb038dda0847dd988d'
      'cda6f3bfd15c4b4c4525004aa06eeff8ca61783aacec57fb3d1f92b0fe2fd1a85f6724517b65e614ad6808d6f6ee34df'
      'f7310fdc82aebfd904b01e1dc54b2927094b2db68d6f903b68401adebf5a7e08d78ff4ef5d63653a65040cf9bfd4aca7'
      '984a74d37145986780fc0b16ac451649de6188a7dbdf191f64b5fc5e2ab47b57f7f7276cd419c17a3ca8e1b939ae49e4'
      '88acba6b965610b5480109c8b17b80e1b7b750dfc7598d5d5011fd2dcc5600a32ef5b52a1ecc820e308aa342721aac09'
      '43bf6686b64b2579376504ccc493d97e6aed3fb0f9cd71a43dd497f01f17c0e2cb3797aa2a2f256656168e6c496afc5f'
      'b93246f6b1116398a346f1a641f3b041e989f7914f90cc2c7fff357876e506b50d334ba77c225bc307ba537152f3f161'
      '0e4eafe595f6d9d90d11faa933a15ef1369546868a7f3a45a96768d40fd9d03412c091c6315cf4fde7cb68606937380d'
      'b2eaaa707b4c4185c32eddcdd306705e4dc1ffc872eeee475a64dfac86aba41c0618983f8741c5ef68d3a101e8a3b8ca'
      'c60c905c15fc910840b94c00a0b9d0'
     ),
     '0aab4c900501b3e24d7cdf4663326a3a87df5e4843b2cbdb67cbf6e460fec350aa5371b1508f9f4528ecea23c436d94b5e8fcd4f681e30a6ac00a9704a188a03'),
]

SEED = hashlib.sha256(b"msb updater test key").digest()      # throwaway test key, not the release key
PUB = M.ed25519_public_key(SEED)
OTHER_SEED = hashlib.sha256(b"someone else").digest()


def make_sums(version, files: dict) -> bytes:
    lines = [f"# ModpackServerBuilder {version}"]
    lines += [f"{hashlib.sha256(data).hexdigest()}  {name}" for name, data in files.items()]
    return ("\n".join(lines) + "\n").encode()


def sign_hex(data, seed=SEED) -> bytes:
    return M.ed25519_sign(seed, data).hex().encode() + b"\n"


# ---- fake GitHub ---------------------------------------------------------------------------

class FakeGitHub:
    def __init__(self):
        self.routes = {}
        self.hits = []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                fake.hits.append((self.path, dict(self.headers)))
                status, body, headers = fake.routes.get(self.path, (404, b'{"message":"Not Found"}', {}))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def hit_count(self, suffix):
        return sum(1 for p, _ in self.hits if p.endswith(suffix))

    def publish(self, tag="v9.0.0", exe=b"MZ new exe " * 1000, sums=None, sig=None, prerelease=False,
                draft=False, skip=(), sums_version=None, body="Fixed things."):
        ver = tag.lstrip("v")
        sums = sums if sums is not None else make_sums(sums_version or ver, {
            M.UPDATE_SETUP_ASSET: exe, "ModpackServerBuilder.exe": exe})
        sig = sig if sig is not None else sign_hex(sums)
        files = {M.UPDATE_SETUP_ASSET: exe, "ModpackServerBuilder.exe": exe, "SHA256SUMS": sums, "SHA256SUMS.sig": sig}
        assets = []
        for name, data in files.items():
            if name in skip:
                continue
            path = f"/dl/{tag}/{name}"
            self.routes[path] = (200, data, {"Content-Type": "application/octet-stream"})
            assets.append({"name": name, "size": len(data), "browser_download_url": self.base + path})
        rel = {"tag_name": tag, "draft": draft, "prerelease": prerelease, "body": body,
               "html_url": f"https://github.com/{M.UPDATE_REPO}/releases/tag/{tag}", "assets": assets}
        self.set_latest(200, json.dumps(rel).encode())
        return exe

    def set_latest(self, status, body=b"{}", headers=None):
        self.routes[f"/repos/{M.UPDATE_REPO}/releases/latest"] = (status, body, headers or {})


class Base(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        M.GITHUB_API = self.gh.base
        M.UPDATE_PUBKEY = PUB.hex()
        shutil.rmtree(M.app_data_dir(), ignore_errors=True)

    def tearDown(self):
        self.gh.close()
        M.UPDATE_PUBKEY = REAL_PUBKEY


def wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ---- tests ---------------------------------------------------------------------------------

class TestEd25519(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for name, seed, pub, msg, sig in RFC8032_VECTORS:
            with self.subTest(name):
                seed, pub, msg, sig = (bytes.fromhex(x) for x in (seed, pub, msg, sig))
                self.assertEqual(M.ed25519_public_key(seed), pub)
                self.assertEqual(M.ed25519_sign(seed, msg), sig)
                self.assertTrue(M.ed25519_verify(pub, msg, sig))
        self.assertEqual(len(RFC8032_VECTORS[3][3]) // 2, 1023)

    def test_roundtrip_and_tamper(self):
        msg = b"# ModpackServerBuilder 3.1.0\n" + b"ab" * 500
        sig = M.ed25519_sign(SEED, msg)
        self.assertTrue(M.ed25519_verify(PUB, msg, sig))
        self.assertFalse(M.ed25519_verify(PUB, msg + b"x", sig))
        self.assertFalse(M.ed25519_verify(PUB, msg[:-1] + b"c", sig))
        for i in (0, 31, 32, 63):                                       # flip a bit in R and in S
            bad = bytearray(sig)
            bad[i] ^= 1
            self.assertFalse(M.ed25519_verify(PUB, msg, bytes(bad)))
        self.assertFalse(M.ed25519_verify(M.ed25519_public_key(OTHER_SEED), msg, sig))
        self.assertFalse(M.ed25519_verify(PUB, msg, sig[:63]))
        self.assertFalse(M.ed25519_verify(PUB[:31], msg, sig))
        self.assertFalse(M.ed25519_verify(b"\xff" * 32, msg, sig))       # not a point
        # malleability: S + L is the same value mod L but must be rejected
        s = int.from_bytes(sig[32:], "little") + M._ED_L
        self.assertFalse(M.ed25519_verify(PUB, msg, sig[:32] + s.to_bytes(32, "little")))

    def test_verify_speed(self):
        name, seed, pub, msg, sig = RFC8032_VECTORS[3]
        t = time.perf_counter()
        self.assertTrue(M.ed25519_verify(bytes.fromhex(pub), bytes.fromhex(msg), bytes.fromhex(sig)))
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_release_pubkey_is_valid_point(self):
        self.assertEqual(len(bytes.fromhex(REAL_PUBKEY)), 32)
        self.assertIsNotNone(M._ed_decompress(bytes.fromhex(REAL_PUBKEY)))

    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_matches_openssl(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        key = d / "k.der"
        key.write_bytes(bytes.fromhex("302e020100300506032b657004220420") + OTHER_SEED)
        (d / "m").write_bytes(b"hello from openssl")
        r = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-keyform", "DER", "-rawin",
                            "-in", str(d / "m")], capture_output=True)
        if r.returncode:
            self.skipTest("openssl without Ed25519 rawin support")
        self.assertEqual(r.stdout, M.ed25519_sign(OTHER_SEED, b"hello from openssl"))


class TestVersion(unittest.TestCase):
    def test_version_tuple(self):
        vt = M.version_tuple
        self.assertEqual(vt("v3.1.0"), (3, 1, 0))
        self.assertEqual(vt("3.1"), (3, 1, 0))
        self.assertEqual(vt("3.1.0.0"), (3, 1, 0))
        self.assertEqual(vt("V10.0.2"), (10, 0, 2))
        self.assertEqual(vt("3.1.0-beta"), (3, 1, 0))
        self.assertEqual(vt("junk"), ())
        self.assertEqual(vt(None), ())
        self.assertGreater(vt("3.10.0"), vt("3.9.9"))


class TestCheck(Base):
    def test_newer_release(self):
        self.gh.publish("v9.0.0")
        info = M.check_for_update()
        self.assertEqual(info["version"], "9.0.0")
        self.assertEqual(info["notes"], "Fixed things.")
        self.assertTrue(info["setup_url"].endswith("/ModpackServerBuilder-Setup.exe"))
        self.assertTrue(info["sums_url"].endswith("/SHA256SUMS"))
        self.assertTrue(info["sig_url"].endswith("/SHA256SUMS.sig"))
        self.assertGreater(info["size"], 0)
        path, headers = self.gh.hits[0]
        self.assertEqual(path, f"/repos/{M.UPDATE_REPO}/releases/latest")
        self.assertIn("MinecraftServerManager", {k.lower(): v for k, v in headers.items()}["user-agent"])

    def test_not_newer(self):
        for tag in (f"v{M.APP_VERSION}", "v0.0.1"):
            self.gh.publish(tag)
            self.assertIsNone(M.check_for_update(), tag)

    def test_tolerated_failures(self):
        self.assertIsNone(M.check_for_update())                        # no release yet: 404
        self.gh.set_latest(403, b'{"message":"API rate limit exceeded"}', {"X-RateLimit-Remaining": "0"})
        self.assertIsNone(M.check_for_update())
        self.gh.set_latest(429, b"{}")
        self.assertIsNone(M.check_for_update())

    def test_ignored_releases(self):
        self.gh.publish("v9.0.0", prerelease=True)
        self.assertIsNone(M.check_for_update())
        self.gh.publish("v9.0.0", draft=True)
        self.assertIsNone(M.check_for_update())
        self.gh.publish("v9.0.0-rc1")
        self.assertIsNone(M.check_for_update())
        self.gh.publish("v9.0.0", skip=("SHA256SUMS.sig",))
        self.assertIsNone(M.check_for_update())

    def test_errors_raise_update_error(self):
        self.gh.set_latest(500, b"oops")
        with self.assertRaises(M.UpdateError):
            M.check_for_update()
        self.gh.set_latest(200, b"not json")
        with self.assertRaises(M.UpdateError):
            M.check_for_update()
        M.GITHUB_API = "http://127.0.0.1:9"                                # nothing listening
        with self.assertRaises(M.UpdateError):
            M.check_for_update(timeout=2)


class TestDownload(Base):
    def test_happy_path(self):
        exe = self.gh.publish("v9.0.0")
        seen = []
        path = M.download_update(M.check_for_update(), progress=seen.append)
        self.assertEqual(path, M.app_data_dir() / "updates" / "9.0.0" / "ModpackServerBuilder-Setup.exe")
        self.assertEqual(path.read_bytes(), exe)
        self.assertTrue(seen and seen[-1] == 1.0 and all(0 <= f <= 1 for f in seen))
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                         ["ModpackServerBuilder-Setup.exe", "SHA256SUMS", "SHA256SUMS.sig", "update.json"])
        # a second call re-verifies the cached exe instead of downloading it again
        M.download_update(M.check_for_update())
        self.assertEqual(self.gh.hit_count("/ModpackServerBuilder-Setup.exe"), 1)

    def assert_rejected(self, msg_part=None):
        info = M.check_for_update()
        with self.assertRaises(M.UpdateError) as cm:
            M.download_update(info)
        if msg_part:
            self.assertIn(msg_part, str(cm.exception))
        self.assertFalse((M.app_data_dir() / "updates" / "9.0.0").exists())
        return cm.exception

    def test_bad_signature(self):
        sums = make_sums("9.0.0", {M.UPDATE_SETUP_ASSET: b"evil"})
        self.gh.publish("v9.0.0", exe=b"evil", sums=sums, sig=sign_hex(sums, OTHER_SEED))
        self.assert_rejected("signature")
        self.assertEqual(self.gh.hit_count("/ModpackServerBuilder-Setup.exe"), 0)    # never fetched
        self.gh.publish("v9.0.0", sig=b"zz-not-hex")
        self.assert_rejected("signature")
        good = make_sums("9.0.0", {M.UPDATE_SETUP_ASSET: b"x"})
        self.gh.publish("v9.0.0", exe=b"x", sums=good + b"extra\n", sig=sign_hex(good))  # list altered after signing
        self.assert_rejected("signature")

    def test_bad_hash(self):
        sums = make_sums("9.0.0", {M.UPDATE_SETUP_ASSET: b"the real exe"})
        self.gh.publish("v9.0.0", exe=b"a different exe", sums=sums, sig=sign_hex(sums))
        self.assert_rejected("checksum")
        self.assertEqual(list((M.app_data_dir() / "updates").glob("**/*.part")), [])

    def test_oversized_download(self):
        exe = self.gh.publish("v9.0.0")
        info = M.check_for_update()
        info["size"] = len(exe) // 2
        with self.assertRaises(M.UpdateError):
            M.download_update(info)
        self.assertFalse((M.app_data_dir() / "updates" / "9.0.0").exists())

    def test_replayed_old_list(self):
        self.gh.publish("v9.0.0", sums_version="3.0.5")        # validly signed, but for another version
        self.assert_rejected("version")
        sums = b"0" * 64 + b"  " + M.UPDATE_SETUP_ASSET.encode() + b"\n"      # no version header at all
        self.gh.publish("v9.0.0", sums=sums, sig=sign_hex(sums))
        self.assert_rejected("version")

    def test_exe_missing_from_list(self):
        sums = make_sums("9.0.0", {"ModpackServerBuilder.exe": b"x"})
        self.gh.publish("v9.0.0", sums=sums, sig=sign_hex(sums))
        self.assert_rejected("not in the signed")


class TestPending(Base):
    def test_pending(self):
        self.assertIsNone(M.pending_update())
        self.gh.publish("v9.0.0", body="Notes 9")
        path = M.download_update(M.check_for_update())
        p = M.pending_update()
        self.assertEqual((p["version"], p["path"], p["notes"]), ("9.0.0", path, "Notes 9"))

    def test_tampered_after_download(self):
        self.gh.publish("v9.0.0")
        path = M.download_update(M.check_for_update())
        with open(path, "ab") as f:
            f.write(b"!")
        self.assertIsNone(M.pending_update())
        self.assertFalse(path.parent.exists())

    def test_not_newer_or_incomplete(self):
        self.gh.publish("v9.0.0")
        path = M.download_update(M.check_for_update())
        old = path.parent.with_name(M.APP_VERSION)
        shutil.copytree(path.parent, old)
        shutil.rmtree(path.parent)
        self.assertIsNone(M.pending_update())                      # same as running version
        shutil.copytree(old, path.parent)
        (path.parent / "SHA256SUMS.sig").unlink()
        self.assertIsNone(M.pending_update())

    def test_untrusted_key(self):
        self.gh.publish("v9.0.0")
        M.download_update(M.check_for_update())
        M.UPDATE_PUBKEY = M.ed25519_public_key(OTHER_SEED).hex()
        self.assertIsNone(M.pending_update())


class TestApply(Base):
    def test_targets(self):
        self.gh.publish("v9.0.0")
        path = M.download_update(M.check_for_update())
        calls = []
        with mock.patch.object(M, "_spawn_detached", lambda args, cwd=None: calls.append(list(args))):
            M.install_dir().mkdir(parents=True, exist_ok=True)
            with mock.patch.object(sys, "executable", str(M.installed_exe())):
                self.assertEqual(M.apply_update(path), M.installed_exe())
            portable = TMP / "Downloads" / "ModpackServerBuilder.exe"
            with mock.patch.object(sys, "executable", str(portable)):
                self.assertEqual(M.apply_update(path), portable)
        self.assertEqual(calls, [[path, "--finish-update", M.installed_exe()], [path, "--finish-update", portable]])

    def test_refuses_tampered(self):
        self.gh.publish("v9.0.0")
        path = M.download_update(M.check_for_update())
        path.write_bytes(b"swapped")
        with mock.patch.object(M, "_spawn_detached") as sp:
            with self.assertRaises(M.UpdateError):
                M.apply_update(path)
            sp.assert_not_called()

    def test_disabled_outside_frozen(self):
        with mock.patch.dict(os.environ, {"MSB_UPDATE_TEST": "0"}), self.assertRaises(M.UpdateError):
            M.apply_update(TMP / "x.exe")


class TestAutoUpdater(Base):
    def test_disabled_when_not_frozen(self):
        with mock.patch.dict(os.environ, {"MSB_UPDATE_TEST": "0"}):
            u = M.AutoUpdater(first_delay=0).start()
        self.assertEqual(u.status()["state"], "disabled")
        self.assertIsNone(u._thread)
        self.assertFalse(u.install_now())

    def test_up_to_date(self):
        u = M.AutoUpdater(first_delay=0).start()
        try:
            self.assertTrue(wait_for(lambda: u.status()["state"] == "up_to_date"), u.status())
            self.assertFalse(u.install_now())
        finally:
            u.stop()

    def test_download_then_install(self):
        exe = self.gh.publish("v9.0.0", exe=b"Z" * 300_000, body="Big update")
        states, u = [], M.AutoUpdater(first_delay=0)
        orig_set = u._set

        def spy(**kw):
            orig_set(**kw)
            states.append(u.status()["state"])
        u._set = spy
        u.start()
        try:
            self.assertTrue(wait_for(lambda: u.status()["state"] == "ready"), u.status())
            st = u.status()
            self.assertEqual((st["version"], st["notes"], st["progress"], st["error"]), ("9.0.0", "Big update", 1.0, None))
            self.assertEqual([s for i, s in enumerate(states) if i == 0 or states[i - 1] != s],
                             ["checking", "downloading", "ready"])
            with mock.patch.object(M, "apply_update") as ap:
                self.assertTrue(u.install_now())
                ap.assert_called_once()
                self.assertEqual(Path(ap.call_args[0][0]).read_bytes(), exe)
            # a later check finding the same version keeps "ready" and downloads nothing
            n = self.gh.hit_count("/ModpackServerBuilder-Setup.exe")
            u._cycle()
            self.assertEqual(u.status()["state"], "ready")
            self.assertEqual(self.gh.hit_count("/ModpackServerBuilder-Setup.exe"), n)
        finally:
            u.stop()

    def test_ready_from_pending_without_network(self):
        self.gh.publish("v9.0.0")
        M.download_update(M.check_for_update())
        M.GITHUB_API = "http://127.0.0.1:9"
        u = M.AutoUpdater(first_delay=3600).start()
        try:
            self.assertTrue(wait_for(lambda: u.status()["state"] == "ready"), u.status())
        finally:
            u.stop()

    def test_errors_go_to_status(self):
        self.gh.set_latest(500, b"down")
        u = M.AutoUpdater(first_delay=0).start()
        try:
            self.assertTrue(wait_for(lambda: u.status()["state"] == "error"), u.status())
            self.assertIn("500", u.status()["error"])
        finally:
            u.stop()
        sums = make_sums("9.0.0", {M.UPDATE_SETUP_ASSET: b"x"})
        self.gh.publish("v9.0.0", exe=b"x", sums=sums, sig=sign_hex(sums, OTHER_SEED))
        u = M.AutoUpdater(first_delay=0).start()
        try:
            self.assertTrue(wait_for(lambda: u.status()["state"] == "error"), u.status())
            self.assertIn("signature", u.status()["error"])
        finally:
            u.stop()

    def test_unexpected_exceptions_never_escape(self):
        u = M.AutoUpdater(first_delay=0)
        with mock.patch.object(M, "check_for_update", side_effect=ZeroDivisionError("boom")):
            self.assertFalse(u._cycle())
        self.assertEqual(u.status()["state"], "error")
        u._st["state"], u._path = "ready", TMP / "missing.exe"
        self.assertFalse(u.install_now())                       # apply_update fails -> False, not raise
        self.assertEqual(u.status()["state"], "error")

    def test_check_now_wakes_thread(self):
        u = M.AutoUpdater(first_delay=3600).start()
        try:
            time.sleep(0.1)
            self.assertEqual(u.status()["state"], "idle")
            u.check_now()
            self.assertTrue(wait_for(lambda: u.status()["state"] == "up_to_date"), u.status())
        finally:
            u.stop()


class TestFinishUpdate(Base):
    def setUp(self):
        super().setUp()
        self.new_exe = TMP / "updates_src" / "ModpackServerBuilder-Setup.exe"
        self.new_exe.parent.mkdir(parents=True, exist_ok=True)
        self.new_exe.write_bytes(b"NEW VERSION " * 5000)
        self.spawned = []
        self.p_spawn = mock.patch.object(M, "_spawn_detached", lambda args, cwd=None: self.spawned.append(list(args)))
        self.p_box = mock.patch.object(M, "message_box", lambda *a, **k: 1)
        self.p_spawn.start()
        self.p_box.start()

    def tearDown(self):
        self.p_spawn.stop()
        self.p_box.stop()
        super().tearDown()

    def locked_replace(self, target, fails):
        real = os.replace
        state = {"fails": 0}

        def fake(src, dst):
            if os.path.abspath(dst) == os.path.abspath(target) and state["fails"] < fails:
                state["fails"] += 1
                raise PermissionError(13, "The process cannot access the file because it is being used")
            return real(src, dst)
        return mock.patch.object(os, "replace", fake), state

    def test_portable_with_lock(self):
        target = TMP / "portable" / "ModpackServerBuilder.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"OLD")
        (M.updates_dir() / "1.0.0").mkdir(parents=True)
        (M.updates_dir() / "99.0.0").mkdir(parents=True)
        patch, state = self.locked_replace(target, 5)
        regs = []
        with patch, mock.patch.object(M, "refresh_shell_icons", regs.append, create=True):
            self.assertTrue(M.finish_update_mode(target, source=self.new_exe))
        self.assertEqual(state["fails"], 5)
        self.assertEqual(target.read_bytes(), self.new_exe.read_bytes())
        self.assertFalse(target.with_name(target.name + ".new").exists())
        self.assertEqual(self.spawned, [[target, "--updated", M.APP_VERSION]])
        self.assertEqual(regs, [])                                   # portable: no Installed apps entry
        self.assertFalse((M.updates_dir() / "1.0.0").exists())
        self.assertTrue((M.updates_dir() / "99.0.0").exists())

    def test_installed_registers(self):
        target = M.installed_exe()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"OLD")
        (target.parent / "install.json").write_text(json.dumps({"version": "1.0.0", "shortcuts": ["a.lnk"]}))
        regs = []
        patch, _ = self.locked_replace(target, 2)
        with patch, mock.patch.object(M, "refresh_shell_icons", regs.append, create=True), \
                mock.patch.object(M, "IS_WIN", True):
            self.assertTrue(M.finish_update_mode(str(target), source=self.new_exe))
        self.assertEqual(regs, [target])
        self.assertEqual(json.loads((target.parent / "install.json").read_text()),
                         {"version": M.APP_VERSION, "shortcuts": ["a.lnk"]})
        self.assertEqual(target.read_bytes(), self.new_exe.read_bytes())

    def test_gives_up_when_locked_forever(self):
        target = TMP / "locked" / "ModpackServerBuilder.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"OLD")
        patch, _ = self.locked_replace(target, 10 ** 9)
        with patch:
            self.assertFalse(M.finish_update_mode(target, source=self.new_exe, wait=0.3))
        self.assertEqual(target.read_bytes(), b"OLD")
        self.assertFalse(target.with_name(target.name + ".new").exists())
        self.assertEqual(self.spawned, [[target]])                   # old version started again

    def test_bad_target(self):
        self.assertFalse(M.finish_update_mode(TMP / "nope.exe", source=self.new_exe))
        self.assertFalse(M.finish_update_mode(self.new_exe, source=self.new_exe))
        self.assertEqual(self.spawned, [])


class TestHandleArgs(Base):
    def test_args(self):
        with mock.patch.object(M, "finish_update_mode") as fm:
            self.assertFalse(M.handle_update_args([]))
            self.assertFalse(M.handle_update_args(["--gui"]))
            fm.assert_not_called()
            self.assertTrue(M.handle_update_args(["--finish-update", r"C:\x\Modpack Server Builder.exe"]))
            fm.assert_called_once_with(r"C:\x\Modpack Server Builder.exe")
            self.assertTrue(M.handle_update_args(["--finish-update"]))          # no target: still exit
            fm.side_effect = RuntimeError("x")
            self.assertTrue(M.handle_update_args(["--finish-update", "t"]))     # never raises
        (M.updates_dir() / "1.0.0").mkdir(parents=True)
        self.assertFalse(M.handle_update_args(["--updated", "9.9.9"]))
        self.assertEqual(M.JUST_UPDATED, "9.9.9")
        self.assertFalse((M.updates_dir() / "1.0.0").exists())

    def test_leftover_cleanup(self):
        d = TMP / "exe_dir"
        d.mkdir(exist_ok=True)
        names = ["ModpackServerBuilder.exe.new", "Modpack Server Builder.exe.old", "other.txt.new", "notes.old"]
        for n in names:
            (d / n).write_text("x")
        M._cleanup_leftovers(d, min_age=0)
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["notes.old", "other.txt.new"])
        (d / "ModpackServerBuilder.exe.new").write_text("fresh")      # an update in progress right now
        M._cleanup_leftovers(d)
        self.assertTrue((d / "ModpackServerBuilder.exe.new").exists())


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
