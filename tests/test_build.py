import json, os, subprocess, sys, tempfile, zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(os.environ.get("MSB_SOURCE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "modpack_server_builder.py"))))
import modpack_server_builder as m

TMP = Path(tempfile.mkdtemp())
os.environ["MSB_HOME"] = str(TMP / "home")

def jar(files):
    import io
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()

JARS = {
    "mods/create.jar": jar({"fabric.mod.json": json.dumps({"id": "create", "version": "1"}), "com/simi/create/C.class": "x"}),
    "mods/sodium.jar": jar({"fabric.mod.json": json.dumps({"id": "sodium", "version": "1"})}),
    "mods/hidden-client.jar": jar({"fabric.mod.json": json.dumps({"id": "hc", "version": "1"}), "com/hc/mod/H.class": "x"}),
}
# Modrinth pack with 3 mods (none flagged in the index) + overrides
pack = TMP / "Test.mrpack"
with zipfile.ZipFile(pack, "w") as z:
    z.writestr("modrinth.index.json", json.dumps({
        "name": "E2E", "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.15.7"},
        "files": [{"path": p, "downloads": ["http://x/" + p], "hashes": {}} for p in JARS]}))
    z.writestr("overrides/config/a.toml", "x=1")

m.fetch_modrinth_file = lambda f, sd: (m.safe_join(sd, f["path"]).parent.mkdir(parents=True, exist_ok=True),
                                       m.safe_join(sd, f["path"]).write_bytes(JARS[f["path"]]), f["path"])[2]
m.install_loader = lambda pack, sd, java, log: (sd / "fabric-server-launch.jar").write_bytes(b"x")
m.find_java = lambda want: (sys.executable, want, True)
m.java_major = lambda p: 17
m.modrinth_sides = lambda jars, log: {"sodium.jar": m.Side("required", "unsupported", "Sodium")}
# fake server: crashes if hidden-client.jar present (client class error w/ stack frame), else Done
fake = TMP / "fake.py"
fake.write_text("""import os,sys
if os.path.exists('mods/hidden-client.jar'):
    print('java.lang.NoClassDefFoundError: net/minecraft/client/gui/screens/Screen')
    print('\\tat knot//com.hc.mod.H.init(H.java:3)'); sys.exit(1)
print('[Server thread/INFO]: Done (1.0s)! For help, type "help"', flush=True)
for l in sys.stdin:
    if l.strip()=='stop': break
""")
m.launch_cmd = lambda sd, ctx: [sys.executable, str(fake)]

out = TMP / "server"
logs = []
m.build_server(pack, out, "4G", accept_eula=True, log=logs.append)
text = "\n".join(logs)
mods = sorted(p.name for p in (out / "mods").glob("*.jar"))
assert mods == ["create.jar"], mods
assert (out / "config/a.toml").exists()
assert (out / "mods_removed_client_only/sodium.jar").exists() and (out / "mods_removed_client_only/hidden-client.jar").exists()
assert "test-launched successfully" in text, text
assert (out / "eula.txt").read_text().strip().endswith("eula=true")
st = json.loads((out / ".modpack_builder.json").read_text())
assert st["mc"] == "1.20.1" and st["loader"] == "fabric" and st["ram_mb"] == 4096
assert "-Xmx4G" in (out / "start.sh").read_text() and "fabric-server-launch.jar" in (out / "start.bat").read_text()
rep = (out / "SERVER_BUILD_REPORT.txt").read_text()
assert "sodium.jar" in rep and "hidden-client.jar" in rep

# EULA not accepted -> no test launch, clear message, eula=false
out2 = TMP / "server2"
logs = []
m.build_server(pack, out2, "4G", accept_eula=False, log=logs.append)
assert "Skipping the test launch" in "\n".join(logs) and "eula=false" in (out2 / "eula.txt").read_text()
# build #1's crash taught the app that "hc" is client-only, so build #2 removes it up front
assert not (out2 / "mods/hidden-client.jar").exists()
assert "earlier crash" in (out2 / "mods_removed_client_only/REMOVED.txt").read_text()

# CLI surface
S = os.path.join(sys.path[0], "modpack_server_builder.py")
r = subprocess.run([sys.executable, S, "--help"], capture_output=True, text=True)
assert r.returncode == 0 and "--fix-log" in r.stdout
r = subprocess.run([sys.executable, S, "--fix-log", "x.log"], capture_output=True, text=True)
assert r.returncode != 0 and "--server-dir" in r.stderr
r = subprocess.run([sys.executable, S, "--fix-log", str(out2 / "nolog.log"), "--server-dir", str(TMP / "nope")], capture_output=True, text=True)
assert r.returncode == 1 and "ERROR" in r.stderr
print("E2E PASSED")
