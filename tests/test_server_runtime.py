"""Tests for the server runtime (start/stop/console/fix). Run: python tests/test_server_runtime.py"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAIN = Path(os.environ.get("MSB_SOURCE") or Path(__file__).resolve().parent.parent / "modpack_server_builder.py")
FAKE = HERE / "fake_server.py"
WORK = Path(tempfile.mkdtemp(prefix="rt_", dir=HERE))
os.environ["MSB_HOME"] = str(WORK / "home")

spec = importlib.util.spec_from_file_location("msb_rt_test", MAIN)
m = importlib.util.module_from_spec(spec)
sys.modules["msb_rt_test"] = m
spec.loader.exec_module(m)

# deterministic Java handling: pretend a Java exists unless a test says otherwise
m.find_java = lambda want: ("/fake/java", want, True)
GET_JAVA_CALLS = []


def fake_get_java(want, log=print, online=True):
    GET_JAVA_CALLS.append(want)
    return None


m.get_java = fake_get_java
m.launch_cmd = lambda server_dir, ctx: [sys.executable, "-u", str(FAKE)]


def wait_for(cond, timeout=10.0, msg="condition"):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.03)
    raise AssertionError(f"timed out waiting for {msg}")


def make_jar(path, modid, pkg="com/example/fancyhud"):
    toml = (f'modLoader="javafml"\nloaderVersion="[47,)"\nlicense="MIT"\n[[mods]]\nmodId="{modid}"\n'
            f'version="1.0.0"\ndisplayName="Fancy HUD"\n')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/mods.toml", toml)
        z.writestr(f"{pkg}/FancyHud.class", b"\xca\xfe\xba\xbe")


_n = [0]


def make_server(mode="normal", eula=True, props=None, jar=True):
    _n[0] += 1
    d = WORK / f"srv{_n[0]}"
    (d / "mods").mkdir(parents=True)
    if jar:
        (d / "server.jar").write_bytes(b"")
    if eula:
        (d / "eula.txt").write_text("#x\neula=true\n")
    (d / "fake_mode.txt").write_text(mode)
    (d / ".modpack_builder.json").write_text(json.dumps({"mc": "1.20.1", "loader": "forge", "ram_mb": 4096}))
    if props is not None:
        (d / "server.properties").write_text(props)
    return d


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.r = m.ServerRunner()

    def tearDown(self):
        self.r.shutdown(timeout=3)

    def kinds(self):
        return [(x["kind"], x["t"]) for x in self.r.lines()[0]]

    def test_full_lifecycle(self):
        d = make_server(props="max-players=12\n")
        self.r.start(d)
        self.assertEqual(self.r.state, "starting")
        self.assertEqual(self.r.status()["max_players"], 12)
        wait_for(lambda: self.r.state == "online", msg="online")
        st = self.r.status()
        self.assertEqual(st["port"], 25570)                 # picked up from "Starting Minecraft server on"
        self.assertIsNotNone(st["online_at"])
        self.assertEqual(st["dir"], str(d))
        self.r.send("join Steve")
        self.r.send("join Alex")
        wait_for(lambda: self.r.status()["players"] == ["Steve", "Alex"], msg="players")
        self.r.send("leave Steve")
        wait_for(lambda: self.r.status()["players"] == ["Alex"], msg="leave")
        self.r.send("/say hello there")
        self.r.send("chat Alex hi all")
        self.r.send("list")
        wait_for(lambda: self.r.status()["max_players"] == 20, msg="list reply")
        self.r.send("err")
        wait_for(lambda: any("boom" in t for _, t in self.kinds()), msg="err")
        k = self.kinds()
        self.assertIn(("command", "> say hello there"), k)
        self.assertIn(("chat", next(t for _, t in k if "[Server] hello there" in t)), k)
        self.assertIn(("chat", next(t for _, t in k if "<Alex> hi all" in t)), k)
        self.assertIn("join", [x for x, t in k if "Steve joined the game" in t])
        self.assertIn("leave", [x for x, t in k if "Steve left the game" in t])
        self.assertEqual([x for x, t in k if "Done (3.2s)" in t], ["done"])
        self.assertEqual([x for x, t in k if "invalid version" in t], ["warn"])
        self.assertEqual([x for x, t in k if "ModLauncher running" in t], ["info"])
        self.assertEqual([x for x, t in k if "Something bad" in t], ["error"])
        self.assertEqual([x for x, t in k if "IllegalStateException: boom" in t], ["error"])
        self.assertEqual([x for x, t in k if "Foo.bar" in t], ["error"])
        self.assertTrue(any(t.endswith("Loading properties") and "\x1b" not in t for _, t in k), "ANSI stripped")
        # "logged in with entity id" / "lost connection" are plain info, not join/leave
        self.assertEqual({x for x, t in k if "logged in with" in t or "lost connection" in t}, {"info"})
        # sequence numbers and incremental reads
        items, nxt = self.r.lines()
        seqs = [x["seq"] for x in items]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(nxt, seqs[-1] + 1)
        self.assertEqual(self.r.lines(nxt), ([], nxt))
        self.r.send("say again")
        wait_for(lambda: len(self.r.lines(nxt)[0]) >= 2, msg="incremental")
        # old-style list reply (names on the next line)
        self.r.send("oldlist")
        wait_for(lambda: any("players online:" in t and "/20" in t for _, t in self.kinds()), msg="oldlist")
        time.sleep(0.2)
        self.assertEqual(self.r.status()["players"], ["Alex"])
        # stop is non-blocking
        self.r.stop()
        self.assertEqual(self.r.state, "stopping")
        wait_for(lambda: self.r.state == "offline", msg="offline")
        st = self.r.status()
        self.assertEqual(st["players"], [])
        self.assertEqual(st["exit_code"], 0)
        self.assertIsNone(st["error"])
        self.assertIn(("command", "> stop"), self.kinds())
        with self.assertRaises(RuntimeError):
            self.r.send("list")
        # can start again afterwards
        self.r.start(d)
        wait_for(lambda: self.r.state == "online", msg="online again")

    def test_stop_typed_in_console_is_not_a_crash(self):
        d = make_server()
        self.r.start(d)
        wait_for(lambda: self.r.state == "online")
        self.r.send("/stop")
        self.assertEqual(self.r.state, "stopping")
        wait_for(lambda: self.r.state == "offline")
        self.assertIsNone(self.r.status()["error"])

    def test_double_start_and_no_jar_and_bad_send(self):
        d = make_server()
        self.r.start(d)
        with self.assertRaises(RuntimeError) as cm:
            self.r.start(d)
        self.assertIn("already running", str(cm.exception))
        wait_for(lambda: self.r.state == "online")
        with self.assertRaises(RuntimeError):
            self.r.start(make_server())
        with self.assertRaises(ValueError):
            self.r.send("say a\nstop")
        with self.assertRaises(ValueError):
            self.r.send("  / ")
        r2 = m.ServerRunner()
        with self.assertRaises(RuntimeError) as cm:
            r2.start(make_server(jar=False))
        self.assertIn("no Minecraft server in this folder", str(cm.exception))
        with self.assertRaises(RuntimeError):
            r2.start(WORK / "does-not-exist")
        self.assertEqual(r2.state, "offline")

    def test_eula(self):
        d = make_server(eula=False)
        with self.assertRaises(m.EulaRequired) as cm:
            self.r.start(d)
        self.assertIn("EULA", str(cm.exception))
        self.assertEqual(self.r.state, "offline")
        (d / "eula.txt").write_text("eula=false\n")
        with self.assertRaises(m.EulaRequired):
            self.r.start(d)
        self.r.start(d, accept_eula=True)
        self.assertIn("eula=true", (d / "eula.txt").read_text())
        wait_for(lambda: self.r.state == "online")

    def test_hung_server_is_forced_closed(self):
        d = make_server("hang")
        self.r.start(d)
        wait_for(lambda: self.r.state == "online")
        proc = self.r._proc
        t0 = time.time()
        self.r.stop(timeout=1)
        self.assertEqual(self.r.state, "stopping")
        wait_for(lambda: self.r.state == "offline", timeout=10)
        self.assertLess(time.time() - t0, 6)
        self.assertIsNotNone(proc.poll())
        self.assertTrue(any("forced to close" in t for _, t in self.kinds()))

    def test_shutdown_kills_and_reaps(self):
        d = make_server("hang")
        self.r.start(d)
        wait_for(lambda: self.r.state == "online")
        proc = self.r._proc
        t0 = time.time()
        self.r.shutdown(timeout=1)
        self.assertLess(time.time() - t0, 8)
        self.assertIsNotNone(proc.returncode)                   # waited for -> no zombie
        self.assertEqual(self.r.state, "offline")
        if os.name != "nt":
            with self.assertRaises(ProcessLookupError):
                os.kill(proc.pid, 0)

    def test_shutdown_during_startup(self):
        d = make_server("slow")
        self.r.start(d)
        wait_for(lambda: self.r._proc is not None and self.r._alive())
        proc = self.r._proc
        self.r.shutdown(timeout=1)
        self.assertIsNotNone(proc.returncode)
        self.assertEqual(self.r.state, "offline")

    def test_killed_while_online_is_a_crash(self):
        d = make_server()
        self.r.start(d)
        wait_for(lambda: self.r.state == "online")
        self.r._proc.kill()
        wait_for(lambda: self.r.state == "crashed")
        st = self.r.status()
        self.assertEqual(st["error"], "The server stopped unexpectedly.")
        self.assertEqual(st["players"], [])
        dg = self.r.diagnose()
        self.assertFalse(dg["fixable"])
        self.assertTrue(dg["summary"])

    def test_crash_diagnose_fix_restart(self):
        d = make_server("crash")
        make_jar(d / "mods" / "fancyhud.jar", "fancyhud")
        make_jar(d / "mods" / "goodmod.jar", "goodmod", pkg="org/good/mod")
        self.r.start(d)
        wait_for(lambda: self.r.state == "crashed", msg="crash")
        st = self.r.status()
        self.assertEqual(st["exit_code"], 1)
        self.assertIn("while it was starting up", st["error"])
        self.assertIn("error", [k for k, t in self.kinds() if "invalid dist" in t])
        self.assertIn("error", [k for k, t in self.kinds() if "TRANSFORMER/fancyhud" in t])
        dg = self.r.diagnose()
        self.assertTrue(dg["fixable"])
        self.assertIn('"fancyhud.jar"', dg["summary"])
        self.assertIn("not on a server", dg["summary"])
        top = dg["findings"][0]
        self.assertEqual((top["kind"], top["action"], top["confidence"], top["mod"]),
                         ("client_mod", "remove", "high", "fancyhud.jar"))
        self.assertEqual(set(top) >= {"kind", "message", "confidence", "action"}, True)
        self.assertIs(self.r.diagnose(), dg)                    # cached until the next start
        res = self.r.fix_and_restart()
        self.assertTrue(res["changed"])
        self.assertTrue(res["restarted"])
        self.assertTrue(any("fancyhud.jar" in n for n in res["notes"]))
        self.assertFalse((d / "mods" / "fancyhud.jar").exists())
        self.assertTrue((d / "mods_removed_client_only" / "fancyhud.jar").exists())
        self.assertTrue((d / "mods" / "goodmod.jar").exists())
        wait_for(lambda: self.r.state == "online", msg="online after fix")
        self.assertIsNone(self.r.status()["error"])
        learned = json.loads((WORK / "home" / "learned.json").read_text())
        self.assertIn("fancyhud", learned["client_modids"])
        with self.assertRaises(RuntimeError):
            self.r.fix_and_restart()                            # running -> refuse

    def test_fix_with_nothing_to_fix(self):
        d = make_server()
        self.r.start(d)
        wait_for(lambda: self.r.state == "online")
        self.r._proc.kill()
        wait_for(lambda: self.r.state == "crashed")
        res = self.r.fix_and_restart()
        self.assertFalse(res["changed"])
        self.assertFalse(res["restarted"])
        self.assertEqual(self.r.state, "crashed")

    def test_java_lookup_and_missing_java(self):
        GET_JAVA_CALLS.clear()
        old_find, old_cmd = m.find_java, m.launch_cmd
        m.find_java = lambda want: None
        m.launch_cmd = lambda server_dir, ctx: [ctx.java, "-version"]
        try:
            d = make_server()
            self.r.start(d)
            wait_for(lambda: self.r.state == "crashed", msg="launch failure")
            self.assertEqual(GET_JAVA_CALLS, [17])              # Minecraft 1.20.1 -> Java 17
            st = self.r.status()
            if not shutil.which("java"):
                self.assertIn("Java couldn't be found", st["error"])
                self.assertIn("adoptium", self.r.diagnose()["summary"])
        finally:
            m.find_java, m.launch_cmd = old_find, old_cmd
        # launch_cmd raising -> plain error, state crashed, no exception escapes
        m.launch_cmd = lambda server_dir, ctx: (_ for _ in ()).throw(RuntimeError("No server jar found to launch."))
        try:
            r2 = m.ServerRunner()
            r2.start(make_server())
            wait_for(lambda: r2.state == "crashed")
            self.assertIn("couldn't be started", r2.status()["error"])
            self.assertEqual(r2.diagnose()["summary"], r2.status()["error"])
        finally:
            m.launch_cmd = old_cmd
        m.launch_cmd = lambda server_dir, ctx: ["/no/such/java-binary"]
        try:
            r3 = m.ServerRunner()
            r3.start(make_server())
            wait_for(lambda: r3.state == "crashed")
            self.assertIn("Java couldn't be found", r3.status()["error"])
        finally:
            m.launch_cmd = old_cmd

    def test_ring_buffer(self):
        d = make_server()
        self.r.start(d)
        wait_for(lambda: self.r.state == "online")
        self.r.send("spam 3500")
        wait_for(lambda: any("spam line 3499" in x["t"] for x in self.r.lines()[0]), timeout=20)
        items, nxt = self.r.lines()
        self.assertEqual(len(items), m.CONSOLE_KEEP)
        self.assertEqual(items[-1]["seq"], nxt - 1)
        self.assertEqual(items[0]["seq"], nxt - m.CONSOLE_KEEP)
        self.assertEqual(len(self.r.lines(nxt - 5)[0]), 5)
        self.assertEqual(len(self.r.lines(0)[0]), m.CONSOLE_KEEP)

    def test_thread_safety(self):
        d = make_server()
        self.r.start(d)
        wait_for(lambda: self.r.state == "online")
        errors = []

        def hammer(i):
            try:
                for j in range(150):
                    self.r.status()
                    self.r.lines(j)
                    if j % 10 == 0:
                        self.r.send(f"say t{i}-{j}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        wait_for(lambda: sum("[Server] t" in x["t"] for x in self.r.lines()[0]) == 120, msg="all says")
        # many threads calling stop at once
        ss = [threading.Thread(target=self.r.stop) for _ in range(6)]
        for t in ss:
            t.start()
        for t in ss:
            t.join()
        wait_for(lambda: self.r.state == "offline")
        self.assertEqual(sum(x["t"] == "> stop" for x in self.r.lines()[0]), 1)

    def test_diagnose_before_start(self):
        dg = self.r.diagnose()
        self.assertFalse(dg["fixable"])
        self.assertEqual(dg["findings"], [])
        with self.assertRaises(RuntimeError):
            self.r.fix_and_restart()


class ClassifyTests(unittest.TestCase):
    def test_formats(self):
        r = m.ServerRunner()
        r._state = "starting"
        cases = [
            ("[12:00:00] [Server thread/INFO]: Starting minecraft server version 1.20.1", "info"),      # vanilla
            ("[12:00:00] [main/INFO]: Loading Minecraft 1.20.1 with Fabric Loader 0.15.0", "info"),     # fabric
            ("[12:00:01] [main/WARN]: Mod x uses deprecated thing", "warn"),
            ("[12:00:01] [main/ERROR]: Failed to start the minecraft server", "error"),
            ("\tat net.minecraft.server.Main.main(Main.java:1)", "error"),
            ("[12:00:02] [Server thread/INFO]: Done (12.345s)! For help, type \"help\"", "done"),
            ("[12:00:03] [Server thread/INFO]: Notch joined the game", "join"),
            ("[12:00:03] [Server thread/INFO]: [Not Secure] <Notch> hello", "chat"),
            ("[12:00:03] [Server thread/INFO]: * Notch waves", "chat"),
            ("[12:00:03] [Server thread/INFO]: <Notch> Bob joined the game", "chat"),
            ("[12:00:04] [Server thread/INFO]: Notch left the game", "leave"),
            ("[12:00:05 INFO]: Steve joined the game", "join"),                                           # paper
            ("[12:00:05 WARN]: Can't keep up!", "warn"),
            ("[15Oct2026 12:00:00.123] [main/FATAL] [net.minecraftforge.server.loading.ServerModLoader/]: Crash", "error"),
            ("2014-01-01 12:00:00 [SEVERE] Oh no", "error"),                                              # ancient
            ("Exception in thread \"main\" java.lang.NoClassDefFoundError: x", "error"),
            ("plain text line", "info"),
            ("[12:00:06] [Server thread/INFO]: There are 0 of a max of 10 players online: ", "info"),
        ]
        for line, want in cases:
            self.assertEqual(r._classify(line), want, line)
        self.assertEqual(r._state, "online")
        self.assertEqual(r._players, {})              # list reply with 0 players cleared Steve/Notch
        self.assertEqual(r._max_players, 10)
        r._classify("[12:00:07] [Server thread/INFO]: There are 2 of a max of 10 players online: A_b, c.d")
        self.assertEqual(list(r._players), ["A_b", "c.d"])


class NetworkTests(unittest.TestCase):
    def setUp(self):
        m._PUBLIC_IP.update(ip=None, ts=0.0)
        self.real_http = m.http_open

    def tearDown(self):
        m.http_open = self.real_http
        m._PUBLIC_IP.update(ip=None, ts=0.0)

    def test_lan_ip(self):
        ip = m.lan_ip()
        if ip is not None:
            parts = ip.split(".")
            self.assertEqual(len(parts), 4)
            self.assertFalse(ip.startswith("127.") or ip.startswith("169.254."))

    def _resp(self, body):
        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()
        return R(body.encode())

    def test_public_ip_fail_success_cache(self):
        calls = []

        def boom(url, headers=None, timeout=60, **kw):
            calls.append(url)
            raise OSError("blocked")
        m.http_open = boom
        self.assertIsNone(m.public_ip(timeout=1))
        self.assertEqual(len(calls), 2)                    # tried ipify then ifconfig.me
        self.assertIsNone(m.public_ip())
        self.assertEqual(len(calls), 2)                    # failure remembered briefly
        m._PUBLIC_IP.update(ip=None, ts=0.0)
        seen = []

        def ok(url, headers=None, timeout=60, **kw):
            seen.append(url)
            if "ipify" in url:
                raise OSError("down")
            return self._resp("81.2.69.160\n")
        m.http_open = ok
        self.assertEqual(m.public_ip(), "81.2.69.160")
        self.assertEqual(m.public_ip(), "81.2.69.160")
        self.assertEqual(len(seen), 2)                     # second call came from the cache
        m._PUBLIC_IP.update(ip=None, ts=0.0)
        m.http_open = lambda *a, **k: self._resp("<html>nope</html>")
        self.assertIsNone(m.public_ip())                   # garbage answers are rejected
        m._PUBLIC_IP.update(ip=None, ts=0.0)
        m.http_open = lambda *a, **k: self._resp("192.168.1.5")
        self.assertIsNone(m.public_ip())                   # a private address isn't a public address

    def test_public_ip_real_network_graceful(self):
        t0 = time.time()
        ip = m.public_ip(timeout=3)
        self.assertTrue(ip is None or isinstance(ip, str))
        self.assertLess(time.time() - t0, 15)

    def test_check_reachable(self):
        got = {}

        def fake(url, headers=None, timeout=60, **kw):
            got["url"] = url
            return self._resp(json.dumps(self.payload))
        m.http_open = fake
        self.payload = {"online": True, "ip": "203.0.113.7", "port": 25565, "debug": {"cachehit": False}}
        r = m.check_reachable("203.0.113.7", 25565)
        self.assertEqual(r["reachable"], True)
        self.assertIn("203.0.113.7", r["detail"])
        self.assertNotIn(":25565", r["detail"])
        self.assertEqual(got["url"], "https://api.mcsrvstat.us/3/203.0.113.7:25565")
        self.payload = {"online": False, "debug": {"cachehit": True}}
        r = m.check_reachable("203.0.113.7", 25570)
        self.assertEqual(r["reachable"], False)
        self.assertIn("port forward", r["detail"])
        self.assertIn("25570", r["detail"])
        self.assertIn("few minutes old", r["detail"])
        self.payload = ["weird"]
        self.assertIsNone(m.check_reachable("example.com", 1)["reachable"])
        m.http_open = lambda *a, **k: (_ for _ in ()).throw(OSError("blocked"))
        r = m.check_reachable("203.0.113.7", 25565)
        self.assertIsNone(r["reachable"])
        self.assertIn("couldn't check", r["detail"])
        self.assertIsNone(m.check_reachable("bad/host?x", 25565)["reachable"])
        self.assertIsNone(m.check_reachable("1.2.3.4", 99999)["reachable"])

    def test_check_reachable_real_network_graceful(self):
        r = m.check_reachable("203.0.113.7", 25565, timeout=3)
        self.assertIn(r["reachable"], (True, False, None))
        self.assertTrue(r["detail"])

    def test_join_info(self):
        d = make_server(props="server-port=25565\n")
        old_lan, old_pub = m.lan_ip, m.public_ip
        try:
            m.lan_ip = lambda: "192.168.1.20"
            m.public_ip = lambda timeout=5: "203.0.113.7"
            self.assertEqual(m.join_info(d), {"lan": "192.168.1.20", "public": "203.0.113.7", "port": 25565,
                                              "local": "localhost"})
            m.write_properties(d, {"server-port": 25570})
            self.assertEqual(m.join_info(d), {"lan": "192.168.1.20:25570", "public": "203.0.113.7:25570",
                                              "port": 25570, "local": "localhost:25570"})
            self.assertIsNone(m.join_info(d, fetch_public=False)["public"])
            m.lan_ip = lambda: None
            m.public_ip = lambda timeout=5: None
            self.assertEqual(m.join_info(d), {"lan": None, "public": None, "port": 25570,
                                              "local": "localhost:25570"})
            m.public_ip = lambda timeout=5: "2001:db8::1"
            self.assertEqual(m.join_info(d)["public"], "[2001:db8::1]:25570")
            e = WORK / "empty_dir"
            e.mkdir(exist_ok=True)
            self.assertEqual(m.join_info(e, fetch_public=False)["port"], 25565)   # no server.properties
        finally:
            m.lan_ip, m.public_ip = old_lan, old_pub


class PropertiesTests(unittest.TestCase):
    def test_round_trip(self):
        d = WORK / "props1"
        d.mkdir()
        original = ("#Minecraft server properties\r\n#Thu Oct 15 12:00:00 CEST 2026\r\n"
                    "! bang comment\r\n"
                    "enable-jmx-monitoring=false\r\n"
                    "motd=A Minecraft Server\r\n"
                    "   # indented comment\r\n"
                    "some-mod-key = spaced value\r\n"
                    "colon-key:colon value\r\n"
                    "long-key=first \\\r\n    second\r\n"
                    "escaped=a\\=b\\:c\\\\d \\u00a7aGreen\r\n"
                    "white-list=false\r\n"
                    "server-port=25565\r\n"
                    "\r\n"
                    "level-seed=\r\n")
        (d / "server.properties").write_bytes(original.encode())
        p = m.read_properties(d)
        self.assertEqual(p["motd"], "A Minecraft Server")
        self.assertEqual(p["some-mod-key"], "spaced value")
        self.assertEqual(p["colon-key"], "colon value")
        self.assertEqual(p["long-key"], "first second")
        self.assertEqual(p["escaped"], "a=b:c\\d \u00a7aGreen")
        self.assertEqual(p["level-seed"], "")
        m.write_properties(d, {"motd": "Bob's §6Gold: server = fun! #1 😀", "white-list": True,
                               "long-key": "short", "new-key": 5})
        raw = (d / "server.properties").read_bytes().decode("ascii")      # file stays plain ASCII
        lines = raw.split("\r\n")
        self.assertEqual(lines[:4], ["#Minecraft server properties", "#Thu Oct 15 12:00:00 CEST 2026",
                                     "! bang comment", "enable-jmx-monitoring=false"])
        self.assertIn("   # indented comment", lines)
        self.assertIn("some-mod-key = spaced value", lines)
        self.assertIn("colon-key:colon value", lines)
        self.assertIn("white-list=true", lines)
        self.assertIn("long-key=short", lines)
        self.assertNotIn("    second", lines)
        self.assertEqual(lines[-2], "new-key=5")                          # new keys appended at the end
        self.assertTrue(raw.endswith("\r\n"))
        self.assertLess(lines.index("motd=" + lines[lines.index(next(l for l in lines if l.startswith("motd=")))][5:]),
                        lines.index("white-list=true"))                    # order kept
        p2 = m.read_properties(d)
        self.assertEqual(p2["motd"], "Bob's §6Gold: server = fun! #1 😀")
        self.assertEqual(p2["escaped"], p["escaped"])
        self.assertEqual(p2["new-key"], "5")
        self.assertEqual(len(p2), len(p) + 1)
        # Java-compatible escaping
        self.assertIn("motd=Bob's \\u00A76Gold\\: server \\= fun\\! \\#1 \\uD83D\\uDE00", lines)
        # leading space and duplicate keys
        (d / "server.properties").write_text("a=1\nb=2\na=3\n")
        self.assertEqual(m.read_properties(d)["a"], "3")
        m.write_properties(d, {"a": " x"})
        self.assertEqual((d / "server.properties").read_text(), "a=\\ x\nb=2\n")
        self.assertEqual(m.read_properties(d)["a"], " x")

    def test_missing_file_created(self):
        d = WORK / "props2"
        d.mkdir()
        self.assertEqual(m.read_properties(d), {})
        m.write_properties(d, {"motd": "Hi", "pvp": False})
        txt = (d / "server.properties").read_text()
        self.assertTrue(txt.startswith("#Minecraft server properties\n#"))
        self.assertEqual(m.read_properties(d), {"motd": "Hi", "pvp": "false"})

    def test_latin1_file(self):
        d = WORK / "props3"
        d.mkdir()
        (d / "server.properties").write_bytes("motd=Caf\xe9\n".encode("latin-1"))
        self.assertEqual(m.read_properties(d)["motd"], "Café")


class SettingsTests(unittest.TestCase):
    def test_schema(self):
        keys = [s["key"] for s in m.PROPERTY_SCHEMA]
        self.assertEqual(keys, ["motd", "max-players", "difficulty", "gamemode", "pvp", "white-list",
                                "online-mode", "allow-flight", "view-distance", "spawn-protection",
                                "server-port", "level-seed"])
        for s in m.PROPERTY_SCHEMA:
            self.assertTrue({"key", "label", "help", "type", "default"} <= set(s), s["key"])
            self.assertIn(s["type"], ("text", "int", "bool", "choice"))
            if s["type"] == "int":
                self.assertTrue("min" in s and "max" in s)
            if s["type"] == "choice":
                self.assertTrue(all(len(c) == 2 for c in s["choices"]))
        by = {s["key"]: s for s in m.PROPERTY_SCHEMA}
        self.assertEqual(by["motd"]["label"], "Server name in the list")
        self.assertEqual(by["white-list"]["label"], "Only let people I approve join")
        self.assertIn("unsafe", by["online-mode"]["help"])
        self.assertIn("flying", by["allow-flight"]["help"])
        json.dumps(m.PROPERTY_SCHEMA)

    def test_get_and_apply(self):
        d = WORK / "set1"
        d.mkdir()
        (d / "server.properties").write_text("# keep me\ndifficulty=2\ngamemode=0\npvp=false\nmax-players=abc\n"
                                             "custom=1\n")
        s = {x["key"]: x["value"] for x in m.get_settings(d)}
        self.assertEqual(s["difficulty"], "normal")        # numeric (old servers) shown as a name
        self.assertEqual(s["gamemode"], "survival")
        self.assertIs(s["pvp"], False)
        self.assertEqual(s["max-players"], 20)             # unreadable -> default
        self.assertEqual(s["server-port"], 25565)
        self.assertEqual(s["motd"], "A Minecraft Server")
        out = m.apply_settings(d, {"difficulty": "hard", "gamemode": "creative", "pvp": "on", "max-players": "8",
                                   "view-distance": 12, "motd": "Fun times", "white-list": True,
                                   "unknown-key": "ignored", "level-seed": "  12345 "})
        self.assertEqual(out["difficulty"], "3")           # stays numeric for an old server
        self.assertEqual(out["gamemode"], "1")
        p = m.read_properties(d)
        self.assertEqual((p["pvp"], p["max-players"], p["view-distance"], p["white-list"], p["level-seed"]),
                         ("true", "8", "12", "true", "12345"))
        self.assertNotIn("unknown-key", p)
        self.assertEqual(p["custom"], "1")
        self.assertTrue((d / "server.properties").read_text().startswith("# keep me\n"))
        s = {x["key"]: x["value"] for x in m.get_settings(d)}
        self.assertEqual((s["difficulty"], s["gamemode"], s["pvp"], s["max-players"]), ("hard", "creative", True, 8))
        # a modern server gets names
        d2 = WORK / "set2"
        d2.mkdir()
        m.apply_settings(d2, {"difficulty": "Peaceful"})
        self.assertEqual(m.read_properties(d2)["difficulty"], "peaceful")

    def test_validation(self):
        d = WORK / "set3"
        d.mkdir()
        (d / "server.properties").write_text("motd=Old\n")
        bad = [{"max-players": 0}, {"max-players": "lots"}, {"max-players": 2.5}, {"max-players": True},
               {"view-distance": 64}, {"server-port": 80}, {"server-port": 70000}, {"difficulty": "nightmare"},
               {"pvp": "maybe"}, {"motd": "two\nlines"}, {"motd": "x" * 151}, {"spawn-protection": -1}]
        for b in bad:
            with self.assertRaises(ValueError, msg=b) as cm:
                m.apply_settings(d, {"motd": "Changed", **b})
            msg = str(cm.exception)
            self.assertTrue(msg and "Traceback" not in msg)
        self.assertEqual(m.read_properties(d)["motd"], "Old")     # nothing saved when anything is invalid
        with self.assertRaises(ValueError):
            m.apply_settings(d, "nope")
        self.assertEqual(m.apply_settings(d, {"nothing": 1}), {})
        m.apply_settings(d, {"max-players": 4.0, "server-port": "25570", "spawn-protection": "0"})
        p = m.read_properties(d)
        self.assertEqual((p["max-players"], p["server-port"], p["spawn-protection"]), ("4", "25570", "0"))
        with self.assertRaises(ValueError) as cm:
            m.apply_settings(d, {"view-distance": 1})
        self.assertIn("from 3 to 32", str(cm.exception))


class RamTests(unittest.TestCase):
    def test_heuristic(self):
        f = m.recommended_ram_mb
        self.assertEqual(f(0, 16384), 3072)
        self.assertEqual(f(0, 4096), 2048)
        self.assertEqual(f(30, 16384), 4096)
        self.assertEqual(f(50, 16384), 4096)
        self.assertEqual(f(51, 16384), 6144)
        self.assertEqual(f(150, 16384), 6144)
        self.assertEqual(f(300, 32768), 8192)
        self.assertEqual(f(300, 8192), 5632)               # 70% of 8 GB, rounded down to 512 MB
        self.assertEqual(f(300, 2048), 2048)               # never under 2 GB
        self.assertEqual(f(100, 6000), 4096)               # 4200 -> 4096
        for n in (0, 10, 80, 200):
            for sysmb in (2000, 4096, 6000, 8192, 12000, 65536):
                v = f(n, sysmb)
                self.assertEqual(v % 512, 0)
                self.assertGreaterEqual(v, 2048)
                self.assertLessEqual(v, max(2048, sysmb * 0.7))
        d = make_server()
        for i in range(60):
            (d / "mods" / f"m{i}.jar").write_bytes(b"")
        self.assertEqual(f(d, 16384), 6144)
        self.assertEqual(f(str(d), 16384), 6144)
        self.assertEqual(f(WORK / "nope", 16384), 3072)
        self.assertIn(f(10, None), (2048, 2560, 3072, 3584, 4096))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
