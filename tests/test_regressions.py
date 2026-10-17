"""Regression tests for the bugs found by the independent audits."""
import json, os, sys, tempfile, threading, time, zipfile
from pathlib import Path
TMP = Path(tempfile.mkdtemp()); os.environ["MSB_HOME"] = str(TMP / "home"); os.environ["HOME"] = str(TMP)
sys.path.insert(0, os.path.dirname(os.path.abspath(os.environ.get("MSB_SOURCE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "modpack_server_builder.py"))))
import modpack_server_builder as m
log = lambda s: None

def jar(path, files):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for k, v in files.items(): z.writestr(k, v)
    return path
def fabric(p, mid, deps=None, jij=(), env="*"):
    f = {"fabric.mod.json": json.dumps({"id": mid, "version": "1", "environment": env, "depends": deps or {}})}
    for n in jij:
        inner = jar(TMP / f"inner-{n}.jar", {"fabric.mod.json": json.dumps({"id": n, "version": "1"})})
        f[f"META-INF/jars/{n}.jar"] = inner.read_bytes()
    return jar(p, f)
def forge(p, mid, deps=(), classes=(), client_deps=()):
    t = f'[[mods]]\nmodId="{mid}"\nversion="1"\n'
    for d in deps: t += f'[[dependencies.{mid}]]\nmodId="{d}"\nmandatory=true\nside="BOTH"\n'
    for d in client_deps: t += f'[[dependencies.{mid}]]\nmodId="{d}"\nmandatory=true\nside="CLIENT"\n'
    f = {"META-INF/mods.toml": t}; f.update({c: "x" for c in classes}); return jar(p, f)
def srv(name):
    d = TMP / name; (d / "mods").mkdir(parents=True); return d
ok = lambda msg: print("ok -", msg)

# 1. harmless WARN-level mixin/class messages on a server that started: nothing removed, nothing learned
d = srv("warn"); fabric(d / "mods/goodmod.jar", "goodmod")
forge(d / "mods/storagemod.jar", "storagemod", classes=["com/storage/mod/S.class"])
(d / "logs").mkdir(); (d / "logs/latest.log").write_text(
    "[main/WARN] (mixin) @Mixin target net.minecraft.class_310 was not found goodmod.mixins.json:X from mod goodmod\n"
    "[main/WARN] [mixin/]: Error loading class: net/minecraft/client/gui/screens/Screen (java.lang.ClassNotFoundException: net.minecraft.client.gui.screens.Screen)\n"
    "\tat com.storage.mod.S.go(S.java:1)\n"
    '[Server thread/INFO]: Done (12.345s)! For help, type "help"\n')
m.fix_from_log(d / "logs/latest.log", d, online=False, log=log)
assert sorted(p.name for p in (d / "mods").iterdir()) == ["goodmod.jar", "storagemod.jar"] and not m.load_learned()
ok("warnings on a healthy server don't remove or learn anything")

# 2. a library in the stack isn't blamed; the mod that called it is
d = srv("lib"); forge(d / "mods/geckolib.jar", "geckolib", classes=["software/bernie/geckolib/R.class"])
forge(d / "mods/coolarmor.jar", "coolarmor", classes=["com/cool/armor/Items.class"])
t = ("java.lang.RuntimeException: Attempted to load class net/minecraft/client/model/HumanoidModel for invalid dist DEDICATED_SERVER\n"
     "\tat TRANSFORMER/geckolib@4/software.bernie.geckolib.R.<init>(R.java:1)\n\tat TRANSFORMER/coolarmor@1/com.cool.armor.Items.reg(Items.java:3)\n"
     "[main/INFO]: something else\n\tat TRANSFORMER/unrelated@1/x.Y.z(Y.java:1)\n")
f = [x for x in m.analyze_text(t, d, m.Ctx("1.20.1", "forge", log=log, online=False)) if x.action == "remove"]
assert [x.info.name for x in f] == ["coolarmor.jar"], f
ok("library frames are skipped when blaming")

# 3. Modrinth's server verdict beats learned history
m.learn_client_modids({"geckolib"})
idx = m.ModIndex(d / "mods"); sides = {"geckolib.jar": m.Side("required", "required", "GeckoLib")}
assert "geckolib.jar" not in {i.name for i in m.decide_client_mods(idx, (), sides, m.load_learned())}
ok("Modrinth server-supported verdict overrides learned list")

# 4. jar-in-jar library still provided by a kept jar isn't 'gone'
d = srv("jij"); fabric(d / "mods/modmenu.jar", "modmenu", jij=["placeholder-api"], env="client")
fabric(d / "mods/placeholder-api.jar", "placeholder-api"); fabric(d / "mods/styledchat.jar", "styledchat", {"placeholder-api": "*"})
rm = {i.name for i in m.decide_client_mods(m.ModIndex(d / "mods"), (), {}, set())}
assert rm == {"modmenu.jar"}, rm
ok("bundled libraries don't cascade-remove server mods")

# 5. CLIENT-side dependency doesn't drag a server mod out
d = srv("side"); forge(d / "mods/oculus.jar", "oculus"); forge(d / "mods/servermod.jar", "servermod", client_deps=["oculus"])
assert {i.name for i in m.decide_client_mods(m.ModIndex(d / "mods"), (), {}, set())} == {"oculus.jar"}
ok("side=CLIENT dependencies are ignored")

# 6. a jar with two mod ids that's duplicated is moved once, no crash; mcmod.info boilerplate isn't a duplicate
d = srv("dup")
jar(d / "mods/alpha-1.0.jar", {"META-INF/mods.toml": '[[mods]]\nmodId="alpha"\nversion="1.0"\n[[mods]]\nmodId="alphacore"\nversion="1.0"\n'})
jar(d / "mods/alpha-2.0.jar", {"META-INF/mods.toml": '[[mods]]\nmodId="alpha"\nversion="2.0"\n[[mods]]\nmodId="alphacore"\nversion="2.0"\n'})
jar(d / "mods/realA.jar", {"mcmod.info": json.dumps([{"modid": "examplemod"}])}); jar(d / "mods/realB.jar", {"mcmod.info": json.dumps([{"modid": "examplemod"}])})
m.prune_client_mods(d, m.Ctx("1.12.2", "forge", log=log, online=False))
assert sorted(p.name for p in (d / "mods").iterdir()) == ["alpha-2.0.jar", "realA.jar", "realB.jar"]
assert not m.move_mod(d / "mods/nope.jar", d, "problem", "x")
ok("duplicates moved once; boilerplate ids ignored; moving a missing jar is harmless")

# 7. CurseForge: NeoForge 1.20.1 ids, disabled files
z = TMP / "cf.zip"
with zipfile.ZipFile(z, "w") as zz:
    zz.writestr("manifest.json", json.dumps({"name": "N", "minecraft": {"version": "1.20.1", "modLoaders": [{"id": "neoforge-1.20.1-47.1.99", "primary": True}]},
                                             "files": [{"projectID": 1, "fileID": 2, "required": False}, {"projectID": 3, "fileID": 4, "required": True}]}))
with zipfile.ZipFile(z) as zz: p = m.parse_modpack(zz)
assert (p.loader, p.loader_ver, len(p.files)) == ("neoforge", "47.1.99", 1)
ok("NeoForge 1.20.1 CurseForge version parsed; disabled files skipped")

# 8. Java for year-based versions; Fabric dependency wordings
assert m.recommended_java("26.1") == 25 and m.recommended_java("1.21.1") == 21
d = srv("fab"); fabric(d / "mods/modmenu.jar", "modmenu"); fabric(d / "mods/fapi.jar", "fabric-api")
lines = ["- Mod 'Mod Menu' (modmenu) 7.2.2 requires any version after 0.5.0 of cloth-config, which is missing!",
         "- Mod 'Mod Menu' (modmenu) 7.2.2 requires any 11.x version of cloth-config2, which is missing!",
         "- Mod 'Fabric API' (fabric-api) 0.92.0 requires version 1.20.2 of 'Minecraft' (minecraft), but only the wrong version is present: 1.20.1!"]
fs = m.analyze_text("\n".join(lines), d, m.Ctx("1.20.1", "fabric", log=log))
kinds = sorted((x.kind, x.data.get("modid") or (x.info.name if x.info else "")) for x in fs)
assert kinds == [("missing_dep", "cloth-config"), ("missing_dep", "cloth-config2"), ("wrong_version", "fapi.jar")], kinds
ok("Java 25 for MC 26.x; all Fabric dependency message forms understood")

# 9. a newer-loader requirement is reported, not 'fixed' by deleting the mod
d = srv("ldr"); forge(d / "mods/create.jar", "create")
fs = m.analyze_text("\tMod ID: 'forge', Requested by: 'create', Expected range: '[47.2,)', Actual version: '47.1.0'", d, m.Ctx("1.20.1", "forge", log=log))
assert [x.action for x in fs] == ["manual"] and "newer" in fs[0].message
ok("too-old loader is a manual step")

# 10. test launch that never reaches Done but keeps logging still times out
d = srv("spam"); (d / "server.jar").write_bytes(b"")
fake = TMP / "spam.py"; fake.write_text("import time\nwhile True:\n    print('[Server thread/WARN]: Can\\'t keep up!', flush=True); time.sleep(0.2)\n")
m.launch_cmd = lambda dd, c: [sys.executable, str(fake)]
t0 = time.time(); okk, out, _ = m.run_server_once(d, m.Ctx("1.20.1", "vanilla", log=log), timeout=3)
assert not okk and time.time() - t0 < 10
ok("chatty, never-ready server is stopped at the timeout")

# 11. start scripts: Xms never above Xmx; .bat switches to UTF-8
d = srv("bat"); (d / "server.jar").write_bytes(b"")
m.write_start_scripts(d, 256, "java", log)
bat = (d / "start.bat").read_text(encoding="utf-8")
assert "-Xms256M -Xmx256M" in bat and "chcp 65001" in bat
ok("valid memory flags; UTF-8 batch file")
# 12. a fix that doesn't help is not repeated (the first-start test used to go round in circles)
d = srv("loop"); (d / "server.jar").write_bytes(b""); fabric(d / "mods/needy.jar", "needy")
crash = TMP / "crash.py"
crash.write_text("print(\"- Mod 'Needy' (needy) 1.0 requires any version of cloth-config, which is missing!\", flush=True)\n")
m.launch_cmd = lambda dd, c: [sys.executable, str(crash)]
installs = []
def fake_install(modid, ctx, mods_dir):
    installs.append(modid); fabric(mods_dir / f"{modid}-{len(installs)}.jar", f"x{len(installs)}"); return "installed", modid
m.install_dependency = fake_install
c = m.Ctx("1.20.1", "fabric", log=log, online=True)
t0 = time.time(); res = m.test_and_fix(d, c, max_attempts=8, timeout=20)
assert res is False and installs == ["cloth-config"], (res, installs)
assert any("Already tried" in n for n in c.notes), c.notes
ok("a fix that didn't help is tried once, then reported instead of looping")

# 13. port already taken: said plainly, no launch attempts
import socket as _s
busy = _s.socket(); busy.bind(("0.0.0.0", 0)); busy.listen(1); port = busy.getsockname()[1]
(d / "server.properties").write_text(f"server-port={port}\n")
launched = []
m.launch_cmd = lambda dd, c: launched.append(1) or [sys.executable, "-c", "pass"]
c = m.Ctx("1.20.1", "fabric", log=log, online=False)
assert m.test_and_fix(d, c, max_attempts=3, timeout=5) is False and not launched
assert any(f"Port {port}" in n for n in c.notes); busy.close()
ok("a busy port is caught before launching")

# 14. "Skip the test" ends the first start without failing the build
d = srv("skip"); (d / "server.jar").write_bytes(b"")
slow = TMP / "slow.py"; slow.write_text("import time\nwhile True:\n    print('loading', flush=True); time.sleep(0.2)\n")
m.launch_cmd = lambda dd, c: [sys.executable, str(slow)]
threading.Timer(1.5, m.SKIP_TEST.set).start()
t0 = time.time(); r = m.test_and_fix(d, m.Ctx("1.20.1", "vanilla", log=log, online=False), max_attempts=3, timeout=60)
assert r is None and time.time() - t0 < 10, r
ok("skipping the test start returns quickly and keeps the server")
# 15. Forge's user_jvm_args.txt examples ("# -Xmx3G") aren't mistaken for the real memory setting
d = srv("forgemem"); (d / "user_jvm_args.txt").write_text("# For example, to set the maximum to 3GB: -Xmx3G\n# -Xmx4G\n-Xms4G\n-Xmx10G\n")
assert m.read_ram_mb(d) == 10240, m.read_ram_mb(d)
(d / "user_jvm_args.txt").write_text("# -Xmx4G\n"); m.save_state(d, m.Ctx("1.20.1", "forge", log=log), "My Pack")
c = m.Ctx("1.20.1", "forge", log=log); c.ram_mb = 7168; m.save_state(d, c)
assert m.read_ram_mb(d) == 7168 and json.loads((d / m.STATE_FILE).read_text())["pack"] == "My Pack"
ok("memory is read from real settings only, and changing it keeps the modpack name")

# 16. server icons: only real PNGs of the right size are accepted
import base64, struct as _st, zlib as _z
def _png(w, h):
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * w for _ in range(h))
    ch = lambda t, dd: _st.pack(">I", len(dd)) + t + dd + _st.pack(">I", _z.crc32(t + dd) & 0xffffffff)
    return "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", _st.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", _z.compress(raw)) + ch(b"IEND", b"")).decode()
d = srv("icon"); (d / "server.jar").write_bytes(b""); rec = m.add_server(d, "Icon test")
for bad in ("not base64!!", "data:image/png;base64," + base64.b64encode(b"GIF89a....").decode()):
    try:
        m.set_server_icon(rec["id"], bad); raise AssertionError("accepted a bad picture")
    except ValueError:
        pass
try:
    m.set_server_icon(rec["id"], _png(256, 256), _png(32, 32)); raise AssertionError("accepted a wrong-size game icon")
except ValueError:
    pass
r = m.set_server_icon(rec["id"], _png(256, 256), _png(64, 64))
assert r["icon"].startswith("local:") and (d / "server-icon.png").is_file() and m.server_icon_bytes(r, "app")
r = m.reset_server_icon(rec["id"]); assert r["icon"] == "" and not (d / "server-icon.png").exists()
ok("custom icons: bad files are refused; saving and resetting work")
# 17. "…" in a Windows-1252 server.properties (byte 0x85) doesn't split the line or corrupt the file on save
d = srv("props"); (d / "server.properties").write_bytes(b"motd=Hello\x85 world\r\nmax-players=20\r\n")
assert m.read_properties(d)["motd"] == "Hello\x85 world"
m.write_properties(d, {"pvp": "false"})
props = m.read_properties(d); assert props["motd"] == "Hello\x85 world" and props["max-players"] == "20", props
ok("server.properties keeps lines intact with unusual characters")

# 18. "Unsupported class file major version" means Java is too NEW: ask for the version the game needs
fs = [f for f in m.analyze_text("java.lang.IllegalArgumentException: Unsupported class file major version 65", d,
                                m.Ctx("1.12.2", "forge", log=log)) if f.kind == "java_version"]
assert fs and fs[0].data["java"] == 8 and "too new" in fs[0].message, [f.message for f in fs]
ok("too-new Java is recognised and the right Java (8) is requested")

# 19. port messages use the server's own port
(d / "server.properties").write_text("server-port = 25570\n")
fs = [f for f in m.analyze_text("**** FAILED TO BIND TO PORT!", d, m.Ctx("1.20.1", "vanilla", log=log)) if f.kind == "port"]
assert fs and "25570" in fs[0].message and m.server_port(d) == 25570, [f.message for f in fs]
ok("port problems mention the server's real port")
# 20. modpack file names with characters Windows forbids (":" in "Technical Electrical: Striking Surprise") are made safe
assert m.win_safe_filename("Technical Electrical: Striking Surprise-6.2.12.mrpack") == "Technical Electrical_ Striking Surprise-6.2.12.mrpack"
assert m.win_safe_filename('a<b>c"d|e?f*.zip') == "a_b_c_d_e_f_.zip" and m.win_safe_filename("CON.jar").lower() != "con.jar"
assert m.win_safe_filename("trailing. ") == "trailing" and m.win_safe_filename("") == "download"
ok("download names are made safe for Windows")

# 21. errors while creating a server come with plain steps
e = OSError("[WinError 87] The parameter is incorrect: 'C:\\x\\Pack: 1.mrpack.part' -> 'C:\\x\\Pack: 1.mrpack'")
x = m.explain_error(e); assert x["title"] and len(x["steps"]) >= 2 and "WinError 87" in x["details"], x
x = m.explain_error(OSError("[Errno 28] No space left on device")); assert "space" in (x["title"] + " ".join(x["steps"])).lower(), x
x = m.explain_error(RuntimeError("something odd")); assert x["steps"] and "github" in " ".join(x["steps"]).lower(), x
ok("errors are explained step by step")

# 22. Mods tab checks: client-only, wrong loader, wrong Minecraft version, plugins, duplicates, missing dependencies
assert m.mc_req_matches(("fabric", "~1.20.1"), "1.20.4") is True and m.mc_req_matches(("fabric", "1.21.x"), "1.20.1") is False
assert m.mc_req_matches(("maven", "[1.20.1,1.21)"), "1.21") is False and m.mc_req_matches(("maven", "1.20.1"), "1.19") is None
d = srv("modtab"); (d / "server.properties").write_text("")
forge(d / "mods/create.jar", "create")
src = TMP / "newmods"
a = jar(src / "sodium.jar", {"fabric.mod.json": json.dumps({"id": "sodium", "name": "Sodium", "environment": "client", "depends": {"minecraft": "1.21.x"}})})
b = forge(src / "create-copy.jar", "create", deps=["flywheel"])
c = jar(src / "Essentials.jar", {"plugin.yml": "name: x"})
r = m.add_mods(d, [a, b, c, src / "missing.jar"], {"mc": "1.20.1", "loader": "forge"}, online=False)
kinds = {x["file"]: sorted(w["kind"] for w in x["warnings"]) for x in r["added"]}
assert kinds == {"sodium.jar": ["client", "loader", "version"], "create-copy.jar": ["deps", "duplicate"], "Essentials.jar": ["plugin"]}, kinds
assert r["skipped"] and r["skipped"][0]["file"] == "missing.jar"
m.set_mod_enabled(d, "sodium.jar", False); assert (d / "mods/sodium.jar.disabled").exists()
m.remove_user_mod(d, "Essentials.jar"); assert [x["jar"] for x in m.summarize_server(d)["removed"]] == ["Essentials.jar"]
for bad in ("../x.jar", "create.txt"):
    try:
        m.set_mod_enabled(d, bad, True); raise AssertionError("accepted " + bad)
    except ValueError:
        pass
ok("mods are checked when added; turn off/remove work and refuse odd names")

# 23. deleting a server refuses folders that aren't just a server
home_rec = m.add_server(Path.home(), "home as server", {})
try:
    m.delete_server_folder(home_rec["id"]); raise AssertionError("deleted the home folder")
except ValueError:
    pass
d = srv("todelete"); (d / "server.properties").write_text(""); rec = m.add_server(d, "del", {})
m.delete_server_folder(rec["id"]); assert not d.exists() and all(x["id"] != rec["id"] for x in m.list_servers())
ok("Delete server removes a server folder but never the home folder")

# 24. player names and offline UUIDs
m.check_player_name("Steve_2")
try:
    m.check_player_name("bad name!"); raise AssertionError("accepted a bad name")
except ValueError:
    pass
assert m.offline_uuid("Notch") == "b50ad385-829d-3141-a216-7e7d7539ba7f"
ok("offline-mode UUIDs match Minecraft's")

# 25. the tick probe command for each kind of server
assert m.tick_probe_command("1.21.4", "fabric") == "tick query" and m.tick_probe_command("1.20.1", "forge") == "forge tps"
assert m.tick_probe_command("1.20.4", "neoforge") == "tick query" and m.tick_probe_command("1.12.2", "vanilla") is None
ok("the right tick-speed command is used for each server")
# 26. several servers at once: one runner per server, and a lag warning on a small computer
app = m.WebApp("app")
a_dir = srv("multi_a"); b_dir = srv("multi_b")
ra = app.runner_for({"dir": str(a_dir)}, create=True); rb = app.runner_for({"dir": str(b_dir)}, create=True)
assert ra is not rb and app.runner_for({"dir": str(a_dir)}) is ra and app.runner_for({"dir": str(TMP / "never")}) is None
class FakeRunner:
    state = "online"; _ram_limit = 6144
    def status(self): return {"dir": str(a_dir)}
old = (m.system_ram_mb, m.os.cpu_count)
try:
    m.system_ram_mb = lambda: 8192; m.os.cpu_count = lambda: 4
    load = m.multi_server_load(app, {"dir": str(b_dir)}, [FakeRunner()])
    assert load["level"] == "bad" and load["reasons"], load
    m.system_ram_mb = lambda: 65536; m.os.cpu_count = lambda: 16
    assert m.multi_server_load(app, {"dir": str(b_dir)}, [FakeRunner()])["level"] == "ok"
finally:
    m.system_ram_mb, m.os.cpu_count = old
ok("each server gets its own runner; a small PC gets a lag warning, a big one doesn't")
# 27. a settings file that can't be read for a moment (antivirus, OneDrive) or is damaged never wipes the server list
d = srv("keepme"); (d / "server.properties").write_text(""); rec = m.add_server(d, "Keep me", {})
sf = m.app_data_dir() / "settings.json"
real_read = Path.read_text
fails = {"n": 0}
def flaky(self, *a, **k):
    if self == sf and fails["n"] < 1:
        fails["n"] += 1; raise PermissionError("locked")
    return real_read(self, *a, **k)
Path.read_text = flaky
try:
    assert any(x["id"] == rec["id"] for x in m.list_servers())
    fails["n"] = -100                     # unreadable for a long time: saves keep what's known
    def always(self, *a, **k):
        if self == sf: raise PermissionError("locked")
        return real_read(self, *a, **k)
    Path.read_text = always
    m.save_settings({"theme": "light"})
finally:
    Path.read_text = real_read
assert any(x["id"] == rec["id"] for x in m.list_servers()), "server list lost"
sf.write_text("{ broken json")            # damaged file: the backup from the last save is used
assert any(x["id"] == rec["id"] for x in m.list_servers()), "damaged settings lost the list"
m.save_settings({"theme": "dark"})
assert json.loads(sf.read_text())["theme"] == "dark" and any(x["id"] == rec["id"] for x in m.list_servers())
ok("settings survive a locked or damaged settings.json")

# 28. ops/whitelist: a BOM is fine, a broken file is never overwritten, existing entries are kept as they are
d = srv("players2"); (d / "server.properties").write_text("online-mode=false\n")
(d / "whitelist.json").write_bytes(b"\xef\xbb\xbf" + json.dumps([{"uuid": "x", "name": "Notch"}]).encode())
assert m.player_lists(d)["whitelist"] == ["Notch"]
m.edit_player_list(d, "whitelist", "Steve", True)
assert [x["name"] for x in json.loads((d / "whitelist.json").read_text())] == ["Notch", "Steve"]
(d / "ops.json").write_text('[{"uuid":"u","name":"Alex","level":2,"bypassesPlayerLimit":true},]')
try:
    m.edit_player_list(d, "ops", "Bob", True); raise AssertionError("overwrote a broken ops.json")
except ValueError as e:
    assert "ops.json" in str(e)
(d / "ops.json").write_text('[{"uuid":"u","name":"Alex","level":2,"bypassesPlayerLimit":true}]')
m.edit_player_list(d, "ops", "alex", True)
assert json.loads((d / "ops.json").read_text()) == [{"uuid": "u", "name": "Alex", "level": 2, "bypassesPlayerLimit": True}]
ok("player lists: BOM read, broken file refused, existing entry kept")

# 29. Delete server refuses a Desktop/Downloads-like folder, a folder holding another server, or lots of other files
desk = Path.home() / "Desktop"; desk.mkdir(exist_ok=True)
for n in ("server.properties", "eula.txt", "tax-return.pdf"): (desk / n).write_text("x")
r1 = m.add_server(desk, "Desktop server", {})
try:
    m.delete_server_folder(r1["id"]); raise AssertionError("deleted the Desktop")
except ValueError:
    pass
outer = srv("outer"); (outer / "server.properties").write_text(""); inner = outer / "creative"; inner.mkdir()
(inner / "server.properties").write_text(""); ro = m.add_server(outer, "Outer", {}); m.add_server(inner, "Inner", {})
try:
    m.delete_server_folder(ro["id"]); raise AssertionError("deleted a folder holding another server")
except ValueError as e:
    assert "Inner" in str(e)
junk = srv("junk"); (junk / "server.properties").write_text("")
for i in range(9): (junk / f"holiday-{i}.pdf").write_text("x")
rj = m.add_server(junk, "Junk", {})
try:
    m.delete_server_folder(rj["id"]); raise AssertionError("deleted a folder full of other files")
except ValueError:
    pass
assert desk.exists() and outer.exists() and junk.exists()
ok("Delete server refuses folders that hold more than the server")

# 30. removing and putting back mods never overwrites a file
d = srv("modsafe")
jar(d / "mods/foo.jar", {"fabric.mod.json": json.dumps({"id": "foo", "version": "1"})})
m.remove_user_mod(d, "foo.jar")
jar(d / "mods/foo.jar", {"fabric.mod.json": json.dumps({"id": "foo", "version": "2"})})
m.remove_user_mod(d, "foo.jar")
assert sorted(p.name for p in (d / "mods_removed_by_you").glob("*.jar")) == ["foo (2).jar", "foo.jar"]
jar(d / "mods/foo.jar", {"fabric.mod.json": json.dumps({"id": "foo", "version": "3"})})
try:
    m.restore_mod(d, "user", "foo.jar"); raise AssertionError("put back over a newer copy")
except ValueError:
    pass
for bad in ("REMOVED.txt", "../x.jar"):
    try:
        m.restore_mod(d, "user", bad); raise AssertionError("restored " + bad)
    except ValueError:
        pass
ok("mods are never overwritten when removed or put back")

# 31. long names keep their extension; snapshots and unknown versions don't produce false warnings
n = m.win_safe_filename("x" * 230 + ".jar"); assert n.endswith(".jar") and len(n) <= 200, n
assert m.mc_req_matches(("maven", "[1.20,1.21)"), "24w14a") is None
assert m.mc_req_matches(("fabric", ">=23w51a"), "1.20.4") is None
assert m.mc_req_matches(("maven", "[1.20.1],[1.20.4]"), "1.20.2") is False
assert m._loader_fits({"META-INF/mods.toml"}, "neoforge", "") is None
ok("long names, snapshots and unknown versions are handled")

# 32. memory: an existing start.bat is read, and only its -Xmx/-Xms change
d = srv("startbat"); (d / "fabric-server-launch.jar").write_bytes(b"x")
(d / "start.bat").write_bytes(b'@echo off\r\n"C:\\Zulu\\bin\\java.exe" -Xms2G -Xmx8G -XX:+UseG1GC -jar fabric-server-launch.jar nogui\r\n')
assert m.read_ram_mb(d) == 8192
m.set_ram(d, m.ctx_for_server(d), 6144)
b = (d / "start.bat").read_bytes()
assert b == b'@echo off\r\n"C:\\Zulu\\bin\\java.exe" -Xms3G -Xmx6G -XX:+UseG1GC -jar fabric-server-launch.jar nogui\r\n', b
ok("memory changes keep the user's own start.bat")

# 33. downloads: "not found" isn't retried, and no half file is left behind
import urllib.error
calls = {"n": 0}
real_open = m.http_open
def nf(url, headers=None):
    calls["n"] += 1; raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
m.http_open = nf
t0 = time.time()
try:
    m.download("https://x/a.jar", TMP / "dl" / "a.jar"); raise AssertionError("no error")
except RuntimeError:
    pass
finally:
    m.http_open = real_open
assert calls["n"] == 1 and time.time() - t0 < 1 and not (TMP / "dl" / "a.jar.part").exists()
ok("permanent download errors fail fast")

# 34. a removed server's runner is forgotten, so a new server in that folder starts fresh
app = m.WebApp("app")
d = srv("forget"); (d / "server.properties").write_text(""); rec = m.add_server(d, "Old", {})
old = app.runner_for(rec, create=True)
class H:
    def _json(self, o, code=200): self.out = o; return True
m.api_post_extra(H(), app, "/api/servers/remove", {"id": rec["id"]})
rec2 = m.add_server(d, "New", {})
assert app.runner_for(rec2) is None
ok("removing a server forgets its old console and crash")
print("ALL REGRESSION TESTS PASSED")
