import json, os, sys, tempfile, zipfile, textwrap, hashlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(os.environ.get("MSB_SOURCE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "modpack_server_builder.py"))))
import modpack_server_builder as m

TMP = Path(tempfile.mkdtemp())
os.environ["MSB_HOME"] = str(TMP / "home")
LOGS = []
def log(s): LOGS.append(s)

def mkjar(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return path

def fabric(path, mid, env="*", deps=None, classes=(), version="1.0", extra=None):
    f = {"fabric.mod.json": json.dumps({"id": mid, "version": version, "environment": env, "depends": deps or {}})}
    for c in classes:
        f[c] = b"x"
    f.update(extra or {})
    return mkjar(path, f)

def forge(path, mid, deps=(), classes=(), version="1.0"):
    toml = f'modLoader="javafml"\n[[mods]]\nmodId="{mid}"\nversion="{version}"\ndescription=\'\'\'multi\n[[fake]]\n\'\'\'\n'
    for d in deps:
        toml += f'[[dependencies.{mid}]]\nmodId="{d}"\nmandatory=true\nversionRange="[1,)"\nside="BOTH"\n'
    toml += f'[[dependencies.{mid}]]\nmodId="optionalthing"\nmandatory=false\n'
    f = {"META-INF/mods.toml": toml}
    for c in classes:
        f[c] = b"x"
    return mkjar(path, f)

def newserver(name):
    d = TMP / name
    (d / "mods").mkdir(parents=True)
    return d

# ---------------------------------------------------------------- toml + jar parsing
ids, deps, ver = m.parse_mods_toml(open("/dev/null").read() or
    'modLoader="javafml"\n[[mods]]\nmodId="create"\nversion="0.5"\n[[dependencies.create]]\nmodId="flywheel"\nmandatory=true\n'
    '[[dependencies.create]]\nmodId="jei"\nmandatory=false\n[[dependencies.create]]\nmodId="neo"\ntype="required"\n')
assert ids == ["create"] and sorted(deps) == ["flywheel", "neo"] and ver == "0.5", (ids, deps, ver)

d = newserver("parse")
forge(d / "a.jar", "alpha", deps=["beta"], classes=["com/alpha/mod/Main.class"])
info = m.parse_jar(d / "a.jar")
assert info.own_ids == {"alpha"} and info.deps >= {"beta"} and "optionalthing" not in info.deps
assert "com/alpha/mod" in info.packages
# nested jar-in-jar provides ids
inner = TMP / "inner.jar"; fabric(inner, "innerlib")
with zipfile.ZipFile(d / "outer.jar", "w") as z:
    z.writestr("fabric.mod.json", json.dumps({"id": "outer", "version": "2"}))
    z.writestr("META-INF/jars/inner.jar", inner.read_bytes())
oi = m.parse_jar(d / "outer.jar")
assert oi.own_ids == {"outer"} and "innerlib" in oi.provided

# ---------------------------------------------------------------- modrinth + cascade classification
d = newserver("classify")
fabric(d / "mods/sodium-fabric-0.5.jar", "sodium")
fabric(d / "mods/sodium-extra-0.5.jar", "sodium-extra", deps={"sodium": "*"})
fabric(d / "mods/clientmeta.jar", "cm", env="client")
fabric(d / "mods/create.jar", "create")
fabric(d / "mods/unknownclient-iris-addon.jar", "irisaddon")   # unknown id, name token 'iris' => by filename
fabric(d / "mods/servermod.jar", "servermod", deps={"clientlib": "*"})   # verified server mod needs clientlib
fabric(d / "mods/clientlib.jar", "clientlib")                  # Modrinth says client-only, but needed
fabric(d / "mods/unverifiedaddon.jar", "unverifiedaddon", deps={"clientlib2": "*"})
fabric(d / "mods/clientlib2.jar", "clientlib2")
fabric(d / "mods/falsepositive-iris-name.jar", "totallyserver")   # name matches 'iris' but Modrinth says server required
fabric(d / "mods/dup-1.0.jar", "dupmod", version="1.0")
fabric(d / "mods/dup-2.0.jar", "dupmod", version="2.0")

def sha(p): return m.sha1_file(p)
H = {p.name: sha(p) for p in (d / "mods").glob("*.jar")}
PROJ = {
    "create.jar": ("P1", "required", "required", "Create"),
    "servermod.jar": ("P2", "required", "required", "Server Mod"),
    "clientlib.jar": ("P3", "required", "unsupported", "Client Lib"),
    "clientlib2.jar": ("P4", "required", "unsupported", "Client Lib 2"),
    "falsepositive-iris-name.jar": ("P5", "optional", "required", "Totally Server"),
    "sodium-fabric-0.5.jar": ("P6", "required", "unsupported", "Sodium"),
}
def fake_post(url, payload, headers=None):
    assert url.endswith("/version_files") and payload["algorithm"] == "sha1"
    out = {}
    for name, (pid, *_r) in PROJ.items():
        if H[name] in payload["hashes"]:
            out[H[name]] = {"project_id": pid}
    return out
def fake_get(url, headers=None):
    assert "/projects?ids=" in url
    ids = json.loads(m.urllib.parse.unquote(url.split("ids=")[1]))
    return [{"id": pid, "client_side": c, "server_side": s, "title": t}
            for name, (pid, c, s, t) in PROJ.items() if pid in ids]
m.post_json, m.get_json = fake_post, fake_get

ctx = m.Ctx("1.20.1", "fabric", "0.15", log=log)
n = m.prune_client_mods(d, ctx)
left = sorted(p.name for p in (d / "mods").glob("*.jar"))
gone = sorted(p.name for p in (d / "mods_removed_client_only").glob("*.jar"))
assert "dup-1.0.jar" in [p.name for p in (d / "mods_removed_duplicates").glob("*.jar")]
assert left == ["clientlib.jar", "create.jar", "dup-2.0.jar", "falsepositive-iris-name.jar", "servermod.jar"], left
assert gone == ["clientlib2.jar", "clientmeta.jar", "sodium-extra-0.5.jar", "sodium-fabric-0.5.jar",
                "unknownclient-iris-addon.jar", "unverifiedaddon.jar"], gone
reasons = (d / "mods_removed_client_only/REMOVED.txt").read_text()
assert "Modrinth lists 'Sodium'" in reasons and "requires client-only" in reasons

# ---------------------------------------------------------------- analyzer: Forge dist error
d = newserver("forge")
forge(d / "mods/clientmod.jar", "clientmod", classes=["com/clientmod/ClientMod.class"])
forge(d / "mods/goodmod.jar", "goodmod", classes=["com/good/mod/Good.class"])
ctx = m.Ctx("1.20.1", "forge", "47.2.0", log=log)
FORGE_LOG = textwrap.dedent("""\
    [12:00:20] [main/ERROR] [net.minecraftforge.fml.loading.RuntimeDistCleaner/DISTXFORM]: Attempted to load class net/minecraft/client/gui/screens/Screen for invalid dist DEDICATED_SERVER
    [12:00:20] [main/ERROR] [net.minecraftforge.fml.ModLoader/]: Failed to create mod instance. ModID: clientmod, class com.clientmod.ClientMod
    net.minecraftforge.fml.ModLoadingException: Client Mod (clientmod) has failed to load correctly
    	at TRANSFORMER/clientmod@1.0/com.clientmod.ClientMod.<init>(ClientMod.java:20) ~[?:?]
    java.lang.RuntimeException: Attempted to load class net/minecraft/client/gui/screens/Screen for invalid dist DEDICATED_SERVER
    	at net.minecraftforge.fml.loading.RuntimeDistCleaner.processClassWithFlags(RuntimeDistCleaner.java:56)
    	at cpw.mods.modlauncher.TransformingClassLoader.loadClass(TransformingClassLoader.java:96)
    	at TRANSFORMER/clientmod@1.0/com.clientmod.ClientMod.<init>(ClientMod.java:20)
""")
fs = m.analyze_text(FORGE_LOG, d, ctx)
rem = [f for f in fs if f.action == "remove"]
assert len(rem) == 1 and rem[0].info.name == "clientmod.jar" and rem[0].confidence == "high" and rem[0].kind == "client_mod", [(f.kind, f.confidence) for f in fs]
changed, manual = m.apply_findings(fs, d, ctx)
assert changed and (d / "mods_removed_client_only/clientmod.jar").exists() and (d / "mods/goodmod.jar").exists()
assert "clientmod" in m.load_learned()   # learned for next time

# ---------------------------------------------------------------- analyzer: stack-frame-only blame (no modid in text)
d = newserver("frames")
fabric(d / "mods/fabclient.jar", "fabclient", classes=["com/fabmod/client/FabClient.class"])
fabric(d / "mods/other.jar", "other", classes=["com/other/thing/Other.class"])
ctx = m.Ctx("1.20.1", "fabric", log=log)
FAB_LOG = textwrap.dedent("""\
    java.lang.RuntimeException: Cannot load class net.minecraft.class_310 in environment type SERVER
    	at net.fabricmc.loader.impl.launch.knot.KnotClassDelegate.loadClassData(KnotClassDelegate.java:226)
    	at java.base/java.lang.ClassLoader.loadClass(ClassLoader.java:520)
    	at knot//com.fabmod.client.FabClient.onInit(FabClient.java:10)
""")
fs = m.analyze_text(FAB_LOG, d, ctx)
rem = [f for f in fs if f.action == "remove"]
assert len(rem) == 1 and rem[0].info.name == "fabclient.jar" and rem[0].confidence == "high", fs

# Fabric mixin target not found
d = newserver("mixin")
fabric(d / "mods/mixmod.jar", "mixmod", extra={"mixmod.mixins.json": "{}"})
fs = m.analyze_text("[main/ERROR]: Mixin apply for mod mixmod failed mixmod.mixins.json:ClientMixin from mod mixmod -> net.minecraft.class_310: @Mixin target net.minecraft.class_310 was not found\n", d, ctx)
assert [f.kind for f in fs if f.action == "remove"] == ["client_mod"], fs
# entrypoint failure -> medium, removed by default (not low)
fs = m.analyze_text("Could not execute entrypoint stage 'main' due to errors, provided by 'mixmod'!\n", d, ctx)
assert fs[0].kind == "mod_failed" and fs[0].confidence == "medium"

# ---------------------------------------------------------------- dependencies (Forge, Fabric, NeoForge)
d = newserver("deps")
forge(d / "mods/needy.jar", "needy")
fabric(d / "mods/fneedy.jar", "fneedy")
ctx = m.Ctx("1.20.1", "forge", log=log)
txt = ("Missing or unsupported mandatory dependencies:\n"
       "\tMod ID: 'geckolib3', Requested by: 'needy', Expected range: '[3.1.0,)', Actual version: '[MISSING]'\n"
       "\tMod ID: 'minecraft', Requested by: 'needy', Expected range: '[1.19.2]', Actual version: '1.20.1'\n"
       "Incompatible mod set!\n- Mod 'F Needy' (fneedy) 1.0 requires version 0.80.0 or later of fabric-api, which is missing!\n"
       "- Mod 'F Needy' (fneedy) 1.0 requires version 0.5 or later of mod 'flywheel', which is missing!\n"
       "Mod create requires flywheel 1.0.0 or above\nCurrently, flywheel is not installed\n")
fs = m.analyze_text(txt, d, ctx)
mis = sorted(f.data["modid"] for f in fs if f.kind == "missing_dep")
assert mis == ["fabric-api", "flywheel", "geckolib3"], mis
assert any(f.kind == "wrong_version" and f.info.name == "needy.jar" for f in fs)

# install_dependency with mocked Modrinth
calls = []
def get2(url, headers=None):
    calls.append(url)
    if "/project/fabric-api" in url and "/version" not in url:
        return {"id": "FAPI", "title": "Fabric API", "server_side": "required"}
    if "/project/FAPI/version" in url:
        return [{"version_type": "beta", "date_published": "2", "files": [{"primary": True, "filename": "fapi-beta.jar", "url": "u", "hashes": {}}]},
                {"version_type": "release", "date_published": "1", "files": [{"primary": True, "filename": "fapi.jar", "url": "u", "hashes": {}}]}]
    if "/project/clientonlylib" in url:
        return {"id": "CL", "title": "Client Lib", "server_side": "unsupported"}
    raise m.urllib.error.HTTPError(url, 404, "nf", {}, None)
m.get_json = get2
m.download = lambda url, dest, **kw: Path(dest).write_bytes(b"jar")
res = m.install_dependency("fabric", ctx, d / "mods")      # alias fabric -> fabric-api, prefers release
assert res == ("installed", "fapi.jar") and (d / "mods/fapi.jar").exists(), res
assert m.install_dependency("clientonlylib", ctx, d / "mods")[0] == "client_only"
m.get_json = lambda url, headers=None: (_ for _ in ()).throw(m.urllib.error.HTTPError(url, 404, "nf", {}, None)) if "search" not in url else {"hits": []}
assert m.install_dependency("ghost", ctx, d / "mods")[0] == "not_found"

# requester removed when its dependency is client-only
d = newserver("deps2"); forge(d / "mods/req.jar", "req")
ctx = m.Ctx("1.20.1", "forge", log=log)
m.get_json = get2
fs = [m.Finding("missing_dep", "x", "install", "high", m.ModIndex(d / "mods").by_modid("req"), modid="clientonlylib", requester="req")]
ch, man = m.apply_findings(fs, d, ctx)
assert ch and (d / "mods_removed_client_only/req.jar").exists()

# ---------------------------------------------------------------- environment problems
m.system_ram_mb = lambda: 32768
d = newserver("env")
ctx = m.Ctx("1.20.1", "forge", ram_mb=4096, log=log)
(d / "user_jvm_args.txt").write_text("# hi\n-Xms2G\n-Xmx4G\n-XX:+UseG1GC\n")
(d / "run.sh").write_text('java @user_jvm_args.txt @libraries/net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt "$@"\n')
m.write_start_scripts(d, 4096, "java", log)
fs = m.analyze_text("java.lang.UnsupportedClassVersionError: x has been compiled by a more recent version of the Java Runtime (class file version 65.0), this version of the Java Runtime only recognizes class file versions up to 61.0\n"
                    "java.lang.OutOfMemoryError: Java heap space\nYou need to agree to the EULA in order to run the server.\n"
                    "**** FAILED TO BIND TO PORT!\n", d, ctx)
kinds = sorted(f.kind for f in fs)
assert kinds == ["eula", "java_version", "oom", "port"], kinds
assert any(f.kind == "java_version" and "Java 21" in f.message for f in fs)
_gj = m.get_java; m.get_java = lambda *a, **k: None      # no suitable Java available -> manual step
ch, man = m.apply_findings(fs, d, ctx)
m.get_java = _gj
jv = (d / "user_jvm_args.txt").read_text()
assert "-Xmx6G" in jv and "-Xms3G" in jv and "-XX:+UseG1GC" in jv and jv.count("-Xmx") == 1, jv
assert (d / "start.sh").read_text().count("@libraries/net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt") == 1
assert "win_args.txt" in (d / "start.bat").read_text()
assert any("EULA" in x for x in man) and any("Java 21" in x for x in man)   # not accepted -> manual
ctx.accept_eula = True
m.apply_findings([f for f in fs if f.kind == "eula"], d, ctx)
assert "eula=true" in (d / "eula.txt").read_text()

# ---------------------------------------------------------------- confidence tiers
d = newserver("tiers")
forge(d / "mods/victim.jar", "victim", classes=["com/victim/mod/V.class"])
ctx = m.Ctx("1.20.1", "forge", log=log)
crash = "java.lang.NullPointerException\nCaused by: java.lang.IllegalStateException: boom\n\tat TRANSFORMER/victim@1.0/com.victim.mod.V.go(V.java:1)\n"
fs = m.analyze_text(crash, d, ctx)
assert fs[0].kind == "mod_crash" and fs[0].confidence == "low"
ch, man = m.apply_findings(fs, d, ctx)
assert not ch and (d / "mods/victim.jar").exists() and any("aggressive" in x for x in man)
ctx.aggressive = True
ch, man = m.apply_findings(fs, d, ctx)
assert ch and (d / "mods_removed_problem_mods/victim.jar").exists()
# unknown error gets an excerpt, not silence
fs = m.analyze_text("[main/ERROR]: Something weird\njava.lang.Exception: weird\n", newserver("unk"), ctx)
assert fs[0].kind == "unknown" and "Something weird" in fs[0].message
# healthy log -> no findings
assert m.analyze_text('[Server thread/INFO]: Done (12.3s)! For help, type "help"\n', newserver("ok"), ctx) == []

# ---------------------------------------------------------------- test_and_fix with a fake server process
d = newserver("loop")
forge(d / "mods/clientmod.jar", "clientmod", classes=["com/clientmod/ClientMod.class"])
forge(d / "mods/badmod2.jar", "badmod2", classes=["com/bad/two/B.class"])
fabric(d / "mods/dupe-1.jar", "dupe", version="1"); fabric(d / "mods/dupe-2.jar", "dupe", version="2")
fake = d / "fake_server.py"
fake.write_text(textwrap.dedent("""\
    import os, sys
    mods = os.listdir("mods")
    if "clientmod.jar" in mods:
        print("[main/ERROR]: Attempted to load class net/minecraft/client/gui/screens/Screen for invalid dist DEDICATED_SERVER")
        print("\\tat TRANSFORMER/clientmod@1.0/com.clientmod.ClientMod.<init>(ClientMod.java:20)")
        sys.exit(1)
    if "badmod2.jar" in mods:
        print("net.minecraftforge.fml.ModLoadingException: Bad Two (badmod2) has failed to load correctly")
        sys.exit(1)
    print('[Server thread/INFO]: Done (3.2s)! For help, type "help"', flush=True)
    for line in sys.stdin:
        if line.strip() == "stop":
            print("stopping"); break
"""))
ctx = m.Ctx("1.20.1", "forge", accept_eula=True, java=sys.executable, log=log)
m.launch_cmd = lambda server_dir, ctx: [sys.executable, str(fake)]
LOGS.clear()
ok = m.test_and_fix(d, ctx, max_attempts=5, timeout=60)
assert ok, "\n".join(LOGS)
assert sorted(p.name for p in (d / "mods").glob("*.jar")) == ["dupe-2.jar"]
assert (d / "mods_removed_client_only/clientmod.jar").exists() and (d / "mods_removed_problem_mods/badmod2.jar").exists()
assert any("Test launch succeeded" in n for n in ctx.notes)

# loop gives up politely when it can't fix anything
d2 = newserver("loop2")
fake2 = d2 / "f.py"; fake2.write_text("import sys\nprint('[main/ERROR]: mysterious failure Exception')\nsys.exit(1)\n")
m.launch_cmd = lambda server_dir, ctx: [sys.executable, str(fake2)]
LOGS.clear()
assert m.test_and_fix(d2, m.Ctx("1.20.1", "forge", accept_eula=True, log=log), 3, 30) is False
assert any("couldn't fix" in l for l in LOGS)

# ---------------------------------------------------------------- fix_from_log end to end
d = newserver("fixlog")
forge(d / "mods/clientmod.jar", "clientmod", classes=["com/clientmod/ClientMod.class"])
(d / "logs").mkdir(); (d / "logs/latest.log").write_text("Starting minecraft server version 1.20.1\n" + FORGE_LOG)
LOGS.clear()
r = m.fix_from_log(d / "logs/latest.log", d, report_only=True, online=False, log=log)
assert (d / "mods/clientmod.jar").exists(), "report-only must not change anything"
r = m.fix_from_log(d / "logs/latest.log", d, online=False, log=log)
assert r is True and (d / "mods_removed_client_only/clientmod.jar").exists()
assert "Minecraft 1.20.1" in "\n".join(LOGS)
assert "clientmod.jar" in (d / "SERVER_BUILD_REPORT.txt").read_text()
try:
    m.fix_from_log(d / "logs/latest.log", TMP / "nonexistent", log=log); raise SystemExit("should fail")
except FileNotFoundError:
    pass

# ---------------------------------------------------------------- misc helpers
assert [m.recommended_java(v) for v in ("1.12.2", "1.16.5", "1.18.2", "1.20.4", "1.20.6", "1.21.1", "26.1.2")] == [8, 8, 17, 17, 21, 21, 25]
assert m.parse_ram_mb("6G") == 6144 and m.parse_ram_mb("512M") == 512 and m.fmt_ram(6144) == "6G"
try: m.parse_ram_mb("lots"); raise SystemExit("bad")
except ValueError: pass
assert m.is_client_class("net/minecraft/client/Minecraft") and not m.is_client_class("net.minecraft.server.Main")
print("ALL V2 TESTS PASSED")
