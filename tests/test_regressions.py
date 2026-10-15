import os, sys
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
print("ALL REGRESSION TESTS PASSED")
