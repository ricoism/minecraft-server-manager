#!/usr/bin/env python3
"""
Modpack Server Builder  (v2)
============================
Turn a client-only Minecraft modpack into a ready-to-run dedicated server,
and automatically work out which mods don't belong on a server.

How client-only mods are caught
  1. BEFORE the server is built
       * Modrinth lookup by file hash (works for CurseForge packs too): Modrinth
         records, per mod, whether it runs on servers.
       * The mod's own metadata (Fabric / Quilt "environment": "client").
       * Mods this app learned about from earlier crashes, plus a built-in list.
       * Dependency cascade: a mod that needs a client-only mod is removed too.
  2. AFTER the server is built (test launch)
       * The server is started, the crash is read, the guilty mod is identified
         (stack traces, mod ids, mixin configs), moved aside, and the server is
         started again - until it reaches "Done!" or nothing more can be fixed.
  3. LATER, on a live server
       * "Fix from log": give the app a latest.log / crash report and the server
         folder, and it diagnoses and repairs what it can.

Nothing is ever deleted. Removed mods go to mods_removed_*/ folders together with
a REMOVED.txt that says why.

Supports CurseForge (.zip) and Modrinth (.mrpack) packs; Forge, NeoForge, Fabric,
Quilt and vanilla. Standard library only. The interface opens in its own window (Edge/Chrome app mode, served
from your own computer; nothing leaves it). Java is needed to run the server.

Usage
  python modpack_server_builder.py                     # opens the interface in your browser
  python modpack_server_builder.py pack.zip -o my_server --ram 8G --accept-eula
  python modpack_server_builder.py --fix-log latest.log --server-dir my_server
"""

import argparse
import glob
import hashlib
import io
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

UA = "ModpackServerBuilder/2.0 (personal tool)"
MODRINTH = "https://api.modrinth.com/v2"
STATE_FILE = ".modpack_builder.json"
REPORT_FILE = "SERVER_BUILD_REPORT.txt"
BUCKETS = {
    "client_only": "mods_removed_client_only",
    "problem": "mods_removed_problem_mods",
    "duplicates": "mods_removed_duplicates",
}
CONF_RANK = {"low": 0, "medium": 1, "high": 2}
IS_WIN = os.name == "nt"
APP_NAME = "Modpack Server Builder"
# Child processes (java, installers) must not flash console windows when running as a windowed .exe
NOWIN = {"creationflags": 0x08000000} if IS_WIN else {}


APP_VERSION = "3.1.0"


def app_data_dir() -> Path:
    if os.environ.get("MSB_HOME"):
        return Path(os.environ["MSB_HOME"])
    base = os.environ.get("LOCALAPPDATA") if IS_WIN else None
    return Path(base) / "ModpackServerBuilder" if base else Path.home() / ".modpack_server_builder"


class Cancelled(Exception):
    """Raised inside a running job when the user presses Stop."""


CANCEL = threading.Event()      # one job runs at a time; the UI sets this to stop it
SKIP_TEST = threading.Event()   # "Skip the test": end the first-start check, keep the server


class TestSkipped(Exception):
    """Raised inside a test launch when the user presses 'Skip the test'."""


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled("Stopped by you.")


_SETTINGS_LOCK = threading.Lock()
SETTINGS_KEYS = {"theme", "ram", "eula", "test", "aggressive", "cfkey", "desktop_shortcut", "recent",
                 "servers", "titlebar", "seen_version", "updates", "update_skip"}


def load_settings() -> dict:
    try:
        d = json.loads((app_data_dir() / "settings.json").read_text(encoding="utf-8"))
        return {k: v for k, v in d.items() if k in SETTINGS_KEYS}
    except Exception:  # noqa: BLE001
        return {}


def save_settings(upd: dict) -> dict:
    with _SETTINGS_LOCK:
        cur = load_settings()
        cur.update({k: v for k, v in upd.items() if k in SETTINGS_KEYS})
        try:
            d = app_data_dir()
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "settings.json.tmp"
            tmp.write_text(json.dumps(cur, indent=1), encoding="utf-8")
            os.replace(tmp, d / "settings.json")
        except OSError:
            pass
        return cur


def remember_server(server_dir, name=""):
    recent = [r for r in load_settings().get("recent", []) if isinstance(r, dict) and r.get("dir") != str(server_dir)]
    recent.insert(0, {"dir": str(server_dir), "name": name or Path(server_dir).name, "ts": int(time.time())})
    save_settings({"recent": recent[:8]})

# ============================================================================
# Small utilities
# ============================================================================


def write_file(path: Path, text: str, newline="\n"):
    with open(path, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)


def ints(s):
    return [int(x) for x in re.findall(r"\d+", s or "")]


def vkey(s):
    return tuple(ints(s))


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def http_open(url, headers=None, timeout=60, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def get_json(url, headers=None):
    with http_open(url, headers) as r:
        return json.load(r)


def post_json(url, payload, headers=None):
    with http_open(url, {"Content-Type": "application/json", **(headers or {})},
                   data=json.dumps(payload).encode(), method="POST") as r:
        return json.load(r)


def download(url, dest: Path, headers=None, sha1=None, sha512=None, retries=3, sha256=None) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last = None
    for attempt in range(retries):
        try:
            h1, h5, h2 = hashlib.sha1(), hashlib.sha512(), hashlib.sha256()
            with http_open(url, headers) as r, open(tmp, "wb") as f:
                final_url = r.geturl()
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    h1.update(chunk)
                    h5.update(chunk)
                    h2.update(chunk)
                    if CANCEL.is_set():
                        raise Cancelled("Stopped by you.")
            if sha1 and h1.hexdigest().lower() != sha1.lower():
                raise IOError("SHA-1 mismatch")
            if sha512 and h5.hexdigest().lower() != sha512.lower():
                raise IOError("SHA-512 mismatch")
            if sha256 and h2.hexdigest().lower() != sha256.lower():
                raise IOError("SHA-256 mismatch")
            os.replace(tmp, dest)
            return final_url
        except Cancelled:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    if tmp.exists():
        tmp.unlink()
    raise RuntimeError(f"{url}: {last}")


def safe_join(base: Path, rel: str) -> Path:
    base_r = base.resolve()
    p = (base / rel).resolve()
    if p != base_r and base_r not in p.parents:
        raise ValueError(f"Unsafe path in modpack: {rel}")
    return p


def system_ram_mb():
    try:
        if IS_WIN:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            s = MS()
            s.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return int(s.ullTotalPhys // (1 << 20))
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1 << 20)
    except Exception:  # noqa: BLE001
        return None


def parse_ram_mb(s) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([MmGg])?[Bb]?\s*", str(s))
    if not m:
        raise ValueError(f"Can't understand RAM value '{s}' (try 4G or 6144M)")
    n = int(m.group(1))
    return n * 1024 if (m.group(2) or "G").upper() == "G" else n


def fmt_ram(mb: int) -> str:
    return f"{mb // 1024}G" if mb % 1024 == 0 else f"{mb}M"


# ============================================================================
# Java discovery
# ============================================================================

_JAVA_CACHE = {}


def java_major(path) -> int:
    """Major version of a java executable, or -1 if it can't be determined."""
    if path in _JAVA_CACHE:
        return _JAVA_CACHE[path]
    major = -1
    try:
        out = subprocess.run([str(path), "-version"], capture_output=True, text=True, timeout=30, **NOWIN)
        txt = out.stderr + out.stdout
        m = re.search(r'version "(\d+)(?:\.(\d+))?', txt)
        if m and IS_WIN and "64-bit" not in txt.lower():
            m = None          # 32-bit Java can't give a modded server enough memory
        if m:
            major = int(m.group(1))
            if major == 1 and m.group(2):
                major = int(m.group(2))
    except Exception:  # noqa: BLE001
        pass
    _JAVA_CACHE[path] = major
    return major


def discover_javas():
    exe = "java.exe" if IS_WIN else "java"
    cands = []
    w = shutil.which("java")
    if w:
        cands.append(w)
    jh = os.environ.get("JAVA_HOME")
    if jh:
        cands.append(str(Path(jh) / "bin" / exe))
    home = str(Path.home())
    pats = []
    if IS_WIN:
        for base in ("C:/Program Files", "C:/Program Files (x86)"):
            for vendor in ("Java", "Eclipse Adoptium", "Microsoft", "Zulu", "Amazon Corretto",
                           "BellSoft", "Semeru", "Eclipse Foundation"):
                pats.append(f"{base}/{vendor}/*/bin/{exe}")
        pats += [f"{home}/AppData/Roaming/.minecraft/runtime/*/*/*/bin/{exe}",
                 f"C:/Program Files (x86)/Minecraft Launcher/runtime/*/*/*/bin/{exe}"]
    else:
        pats += ["/usr/lib/jvm/*/bin/java", f"{home}/.sdkman/candidates/java/*/bin/java",
                 f"{home}/.jdks/*/bin/java", "/opt/java/*/bin/java", "/usr/local/openjdk*/bin/java",
                 "/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java",
                 f"{home}/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java",
                 "/opt/homebrew/opt/openjdk*/bin/java", "/usr/local/opt/openjdk*/bin/java",
                 f"{home}/.minecraft/runtime/*/*/*/bin/java"]
    store = str(java_store())
    pats += [f"{store}/*/*/bin/{exe}", f"{store}/*/*/Contents/Home/bin/java"]
    for p in pats:
        cands += glob.glob(p)
    seen, out = set(), {}
    for c in cands:
        try:
            rp = str(Path(c).resolve())
        except OSError:
            continue
        if rp in seen or not os.path.isfile(c):
            continue
        seen.add(rp)
        v = java_major(c)
        if v > 0:
            out[c] = v
    return out


def recommended_java(mc: str) -> int:
    p = ints(mc)[:3] + [0, 0, 0]
    if p[0] != 1:                 # year-based versions: 26.x needs Java 25
        return 25 if p[0] >= 26 else 21
    minor, patch = p[1], p[2]
    if minor <= 16:
        return 8
    if minor < 20 or (minor == 20 and patch < 5):
        return 17
    return 21


def java_store() -> Path:
    return app_data_dir() / "java"


def _temurin_platform():
    os_ = "windows" if IS_WIN else "mac" if sys.platform == "darwin" else "linux"
    import platform as _pf
    mach = _pf.machine().lower()
    return os_, ("aarch64" if mach in ("arm64", "aarch64") else "x64")


def download_java(want: int, log=print) -> str:
    """Download Eclipse Temurin JRE `want` into the app's own folder. Returns the java executable path."""
    check_cancel()
    os_, arch = _temurin_platform()
    q = urllib.parse.urlencode({"architecture": arch, "image_type": "jre", "os": os_, "vendor": "eclipse"})
    assets = get_json(f"https://api.adoptium.net/v3/assets/latest/{want}/hotspot?{q}")
    if not assets:
        raise RuntimeError(f"No Java {want} download is available for this computer.")
    pkg = assets[0]["binary"]["package"]
    store = java_store()
    store.mkdir(parents=True, exist_ok=True)
    archive = store / pkg["name"]
    size = int(pkg.get("size") or 0)
    log(f"Downloading Java {want} (Eclipse Temurin{f', {size // (1 << 20)} MB' if size else ''})...")
    download(pkg["link"], archive, sha256=pkg.get("checksum"))
    check_cancel()
    stage = store / f".jre-{want}-unpack"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                safe_join(stage, m.filename)
            z.extractall(stage)
    else:
        import tarfile
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                safe_join(stage, m.name)
            t.extractall(stage)
    archive.unlink(missing_ok=True)
    dest = store / f"jre-{want}"
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(stage, dest)
    exe = "java.exe" if IS_WIN else "java"
    hits = sorted(dest.glob(f"*/bin/{exe}")) + sorted(dest.glob("*/Contents/Home/bin/java"))
    if not hits:
        raise RuntimeError("The Java download didn't contain a java program.")
    java = str(hits[0])
    if not IS_WIN:
        os.chmod(java, 0o755)
    _JAVA_CACHE.pop(java, None)
    log(f"  Java {want} is ready ({java})")
    return java


def get_java(want: int, log=print, online=True):
    """Best Java for `want`: an installed exact match, else a fresh download, else the nearest newer one.
    -> (path, major, exact) or None"""
    found = find_java(want)
    if found and found[2]:
        return found
    if online:
        try:
            path = download_java(want, log)
            v = java_major(path)
            if v > 0:
                return path, v, v == want
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"  Couldn't download Java {want} automatically: {e}")
    return found


def find_java(want: int):
    """-> (path, major, exact) or None. Prefers an exact match, else the nearest newer one."""
    javas = discover_javas()
    if not javas:
        return None
    exact = [p for p, v in javas.items() if v == want]
    if exact:
        return exact[0], want, True
    newer = sorted((v, p) for p, v in javas.items() if v > want)
    if newer:
        return newer[0][1], newer[0][0], False
    return None


# ============================================================================
# Learned data (remembers mods that crashed servers as client-only)
# ============================================================================


def learned_path() -> Path:
    return app_data_dir() / "learned.json"


def load_learned() -> set:
    out = set()
    for p in {learned_path(), Path.home() / ".modpack_server_builder" / "learned.json"}:
        try:
            out |= set(json.loads(p.read_text(encoding="utf-8")).get("client_modids", []))
        except Exception:  # noqa: BLE001
            pass
    return out


def learn_client_modids(ids):
    try:
        cur = load_learned() | {i for i in ids if i}
        p = learned_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"client_modids": sorted(cur)}, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ============================================================================
# Context passed around (what we know about this server + a running notebook)
# ============================================================================


class Ctx:
    def __init__(self, mc="", loader="", loader_ver="", accept_eula=False, aggressive=False,
                 java=None, java_script="java", ram_mb=6144, log=print, online=True):
        self.mc, self.loader, self.loader_ver = mc, loader, loader_ver
        self.accept_eula, self.aggressive = accept_eula, aggressive
        self.java = java or "java"
        self.java_script = java_script
        self.ram_mb = ram_mb
        self.log = log
        self.online = online
        self.notes = []          # lines for the final report

    def note(self, s):
        self.notes.append(s)
        self.log("  " + s)


def save_state(server_dir: Path, ctx: Ctx, pack_name=""):
    d = {"pack": pack_name, "mc": ctx.mc, "loader": ctx.loader, "loader_ver": ctx.loader_ver,
         "ram_mb": ctx.ram_mb, "java_script": ctx.java_script}
    try:
        write_file(server_dir / STATE_FILE, json.dumps(d, indent=1))
    except OSError:
        pass


def detect_server_env(server_dir: Path, text="") -> dict:
    d = {}
    try:
        d = json.loads((server_dir / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    if not d.get("loader"):
        if (server_dir / "fabric-server-launch.jar").exists():
            d["loader"] = "fabric"
        elif (server_dir / "quilt-server-launch.jar").exists():
            d["loader"] = "quilt"
        elif (server_dir / "libraries/net/neoforged").exists():
            d["loader"] = "neoforge"
        elif (server_dir / "libraries/net/minecraftforge").exists() or list(server_dir.glob("forge-*.jar")):
            d["loader"] = "forge"
    if not d.get("mc"):
        try:
            lt = text or (server_dir / "logs/latest.log").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            lt = text
        m = (re.search(r"Starting minecraft server version (\S+)", lt) or
             re.search(r"Loading Minecraft (\S+) with", lt) or
             re.search(r"minecraft version (\d+\.\d+(?:\.\d+)?)", lt, re.I))
        if m:
            d["mc"] = m.group(1)
    return d


def ctx_for_server(server_dir: Path, text="", accept_eula=False, aggressive=False, log=print, online=True):
    env = detect_server_env(server_dir, text)
    ctx = Ctx(env.get("mc", ""), env.get("loader", ""), env.get("loader_ver", ""), accept_eula,
              aggressive, log=log, online=online)
    ctx.ram_mb = env.get("ram_mb") or read_ram_mb(server_dir)
    ctx.java_script = env.get("java_script", "java")
    if ctx.mc:
        f = find_java(recommended_java(ctx.mc))
        if f:
            ctx.java = f[0]
    elif shutil.which("java"):
        ctx.java = shutil.which("java")
    return ctx


# ============================================================================
# Jar inspection
# ============================================================================

SKIP_PKG = ("net/minecraft", "net/minecraftforge", "net/neoforged", "net/fabricmc", "org/spongepowered",
            "com/mojang", "cpw/mods", "org/quiltmc", "META-INF", "assets", "data", "mcp", "javax",
            "java", "kotlin", "kotlinx", "org/apache", "com/google", "io/netty", "org/objectweb",
            "it/unimi", "org/jetbrains", "org/intellij", "org/slf4j", "org/lwjgl", "joptsimple",
            "org/yaml", "com/electronwill", "org/jline", "com/electronwill", "org/json", "org/w3c")
SKIP_CLASS_PREFIX = ("java.", "javax.", "jdk.", "sun.", "net.minecraft.", "net.minecraftforge.", "net.neoforged.",
                     "cpw.mods.", "org.spongepowered.", "net.fabricmc.", "org.quiltmc.", "com.mojang.",
                     "io.netty.", "org.apache.", "com.google.", "it.unimi.", "org.lwjgl.", "kotlin.")
KNOWN_LIBRARY_IDS = {
    "geckolib", "geckolib3", "architectury", "cloth_config", "cloth-config", "cloth-config2", "fabric-api", "fabric",
    "kotlinforforge", "fabric-language-kotlin", "curios", "trinkets", "balm", "bookshelf", "moonlight", "puzzleslib",
    "citadel", "playeranimator", "player-animator", "framework", "collective", "resourcefullib", "ftblibrary",
    "owo", "owo-lib", "forgeconfigapiport", "yet_another_config_lib_v3", "yacl", "azurelib", "terrablender",
    "lithostitched", "smartbrainlib", "mixinextras", "cristellib", "creativecore", "supermartijn642corelib",
    "supermartijn642configlib", "fzzy_config", "konkrete", "melody", "iceberg", "prism", "jei", "rei", "emi",
    "patchouli", "configured", "catalogue", "mru", "zeta", "autoreglib", "selene", "blueprint", "lodestone",
    "kiwi", "attributefix", "caelus", "corgilib", "midnightlib", "libraryferret", "uranus", "coroutil",
}


def is_library_id(mid: str) -> bool:
    m = mid.lower()
    return m in KNOWN_LIBRARY_IDS or bool(re.search(r"(lib|api|core|library|config)$", m))


PLACEHOLDER_IDS = {"examplemod", "modid", "yourmodid", "mymod", "example_mod", "template", "examplemodid"}
BUILTIN_IDS = {"minecraft", "forge", "neoforge", "java", "fabricloader", "fabric-loader", "quilt_loader",
               "quilt_base", "mcp", "fml", "javafml", "lowcodefml", "mclanguage", "lowcodelanguage",
               "javafmllanguage", "fabric-language-kotlin-dummy"}


class ModInfo:
    def __init__(self, jar):
        self.jar = jar
        self.own_ids, self.provided = set(), set()
        self.version = ""
        self.env = None
        self.deps = set()
        self.loader = None
        self.packages, self.mixins = set(), set()
        self.dup_ids = set()      # ids from modern metadata only (mcmod.info ids are often copied boilerplate)
        self.broken = False

    @property
    def name(self):
        return self.jar.name if self.jar else "?"

    def __repr__(self):
        return f"<ModInfo {self.name} {sorted(self.own_ids)}>"


def parse_mods_toml(text):
    ids, deps, version = [], [], ""
    section, cur, multiline = None, {}, None

    def flush():
        nonlocal version
        if section == "mods" and cur.get("modId"):
            ids.append(cur["modId"])
            version = version or cur.get("version", "")
        elif section and section.startswith("dependencies.") and cur.get("modId"):
            mand = str(cur.get("mandatory", "")).lower()
            client_only = str(cur.get("side", "")).upper() == "CLIENT"
            if not client_only and (mand == "true" or str(cur.get("type", "")).lower() == "required"):
                deps.append(cur["modId"])

    for raw in text.splitlines():
        if multiline:
            if multiline in raw:
                multiline = None
            continue
        line = raw.split("#", 1)[0].strip() if not re.search(r"=\s*['\"]", raw) else raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            flush()
            section, cur = line.strip("[] ").strip(), {}
            continue
        m = re.match(r"(\w+)\s*=\s*(.*)", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        for q in ("'''", '"""'):
            if v.startswith(q):
                if v.count(q) < 2:
                    multiline = q
                v = ""
                break
        else:
            qm = re.match(r"""(['"])(.*?)\1""", v)
            v = qm.group(2) if qm else v.split()[0] if v else ""
        cur[k] = v
    flush()
    return ids, deps, version


def _scan_zip(z, info, depth):
    names = z.namelist()
    nameset = set(names)

    def rd(n):
        return z.read(n).decode("utf-8-sig", "ignore")

    try:
        if "fabric.mod.json" in nameset:
            d = json.loads(rd("fabric.mod.json"), strict=False)
            info.loader = info.loader or "fabric"
            if d.get("id"):
                info.own_ids.add(d["id"])
                info.dup_ids.add(d["id"])
                info.provided.add(d["id"])
            for p in d.get("provides") or []:
                pid = p if isinstance(p, str) else (p or {}).get("id")
                if pid:
                    info.provided.add(pid)
            info.version = info.version or str(d.get("version", ""))
            if str(d.get("environment", "*")).lower() == "client":
                info.env = "client"
            if isinstance(d.get("depends"), dict):
                info.deps |= set(d["depends"].keys())
    except Exception:  # noqa: BLE001
        pass
    try:
        if "quilt.mod.json" in nameset:
            d = json.loads(rd("quilt.mod.json"), strict=False)
            ql = d.get("quilt_loader", {})
            info.loader = info.loader or "quilt"
            if ql.get("id"):
                info.own_ids.add(ql["id"])
                info.dup_ids.add(ql["id"])
                info.provided.add(ql["id"])
            for p in ql.get("provides") or []:
                pid = p if isinstance(p, str) else (p or {}).get("id")
                if pid:
                    info.provided.add(pid)
            info.version = info.version or str(ql.get("version", ""))
            if str((d.get("minecraft") or {}).get("environment", "*")).lower() == "client":
                info.env = "client"
            for dep in ql.get("depends") or []:
                if isinstance(dep, str):
                    info.deps.add(dep)
                elif isinstance(dep, dict) and not dep.get("optional") and dep.get("id"):
                    info.deps.add(dep["id"])
    except Exception:  # noqa: BLE001
        pass
    for tn in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
        try:
            if tn in nameset:
                ids, deps, ver = parse_mods_toml(rd(tn))
                info.loader = info.loader or ("neoforge" if "neoforge" in tn else "forge")
                info.own_ids |= set(ids)
                info.dup_ids |= set(ids)
                info.provided |= set(ids)
                info.deps |= set(deps)
                info.version = info.version or ver
        except Exception:  # noqa: BLE001
            pass
    try:
        if "mcmod.info" in nameset:
            d = json.loads(rd("mcmod.info"), strict=False)
            lst = d.get("modList", []) if isinstance(d, dict) else d
            for e in lst or []:
                if e.get("modid"):
                    info.own_ids.add(e["modid"])
                    info.provided.add(e["modid"])
                    info.version = info.version or str(e.get("version", ""))
                    for r in e.get("requiredMods") or []:
                        info.deps.add(str(r))
            info.loader = info.loader or "forge"
    except Exception:  # noqa: BLE001
        pass
    if depth == 0:
        for n in names:
            if n.endswith(".class"):
                parts = n.split("/")[:-1]
                if len(parts) >= 2:
                    pkg = "/".join(parts[:3])
                    if not pkg.startswith(SKIP_PKG):
                        info.packages.add(pkg)
            elif "/" not in n and re.search(r"mixins?[\w.\-]*\.json$|[\w.\-]*mixins?\.json$", n):
                info.mixins.add(n)
        for n in names:
            if (n.startswith("META-INF/jars/") or n.startswith("META-INF/jarjar/")) and n.endswith(".jar"):
                try:
                    zi = z.getinfo(n)
                    if zi.file_size > 30 << 20:
                        continue
                    with zipfile.ZipFile(io.BytesIO(z.read(n))) as nz:
                        sub = ModInfo(None)
                        _scan_zip(nz, sub, 1)
                        info.provided |= sub.provided
                except Exception:  # noqa: BLE001
                    pass


def parse_jar(jar: Path) -> ModInfo:
    info = ModInfo(jar)
    try:
        with zipfile.ZipFile(jar) as z:
            _scan_zip(z, info, 0)
    except Exception:  # noqa: BLE001
        info.broken = True
    return info


def blame_skip_class(cls):
    return cls.startswith(SKIP_CLASS_PREFIX)


# Stack-trace / log patterns used to point at a mod
RX_TRANSFORMER = re.compile(r"TRANSFORMER/(?P<id>[\w\-.]+)@")
RX_MODID_FAIL = re.compile(r"Failed to create mod instance\. ModID: (?P<id>[\w\-.]+)")
RX_PROVIDED_BY = re.compile(r"provided by '(?P<id>[\w\-.]+)'")
RX_FROM_MOD = re.compile(r"from mod (?P<id>[\w\-.]+)")
RX_LOADFAIL = re.compile(r"\((?P<id>[\w\-.]+)\) has failed to load correctly")
RX_FRAME = re.compile(r"\bat (?:[^\s/()]+//?)*?(?:[\w\-.]+@[\w\-.+]+/)?([A-Za-z_][\w.$]*)\.[\w$<>\-]+\(")


class ModIndex:
    def __init__(self, mods_dir):
        self.mods_dir = Path(mods_dir)
        jars = sorted(self.mods_dir.glob("*.jar")) if self.mods_dir.is_dir() else []
        self.infos = [parse_jar(j) for j in jars]
        self._pkg = {}
        for i in self.infos:
            for p in i.packages:
                self._pkg.setdefault(p, []).append(i)

    def by_modid(self, modid):
        for i in self.infos:
            if modid in i.own_ids:
                return i
        for i in self.infos:
            if modid in i.provided:
                return i
        return None

    def by_jarname(self, name):
        n = name.lower()
        for i in self.infos:
            if i.name.lower() == n:
                return i
        return None

    def for_mixin(self, cfg):
        for i in self.infos:
            if cfg in i.mixins:
                return i
        return None

    def for_class(self, cls):
        cls = cls.replace("/", ".")
        if blame_skip_class(cls):
            return None
        parts = cls.split(".")[:-1]
        for depth in (3, 2):
            if len(parts) >= depth:
                hits = self._pkg.get("/".join(parts[:depth]), [])
                if len(hits) == 1:
                    return hits[0]
        return None

    def libraries(self):
        """Jars that other installed mods depend on (shared libraries)."""
        if getattr(self, "_libs", None) is None:
            needed = set()
            for i in self.infos:
                needed |= i.deps
            self._libs = {i for i in self.infos if i.provided & needed or any(is_library_id(x) for x in i.own_ids)}
        return self._libs

    def blame(self, window):
        """Best guess at which jar a block of log lines is about. Libraries (geckolib, etc.) are often just
        the code a misbehaving mod called, so a mod nothing depends on is preferred."""
        cands = []
        for line in window:
            for rx in (RX_TRANSFORMER, RX_MODID_FAIL, RX_PROVIDED_BY, RX_FROM_MOD, RX_LOADFAIL):
                m = rx.search(line)
                if m and m.group("id") not in BUILTIN_IDS:
                    i = self.by_modid(m.group("id"))
                    if i and i not in cands:
                        cands.append(i)
        if not cands:
            for line in window:
                m = RX_FRAME.search(line)
                if m:
                    i = self.for_class(m.group(1))
                    if i and i not in cands:
                        cands.append(i)
        if not cands:
            return None
        libs = self.libraries()
        return next((i for i in cands if i not in libs), cands[-1])

    def duplicates(self):
        groups = {}
        for i in self.infos:
            for mid in i.dup_ids - PLACEHOLDER_IDS:
                groups.setdefault(mid, []).append(i)
        out = []
        for mid, lst in groups.items():
            if len(lst) > 1:
                def key(i):
                    return (vkey(i.version) if i.version and "$" not in i.version else vkey(i.name),
                            i.jar.stat().st_mtime)
                lst = sorted(lst, key=key, reverse=True)
                out.append((mid, lst[0], lst[1:]))
        return out


# ============================================================================
# Client-only classification
# ============================================================================

CLIENT_ONLY_NAMES = [
    "optifine", "optifabric", "sodium", "embeddium", "rubidium", "oculus", "iris", "indium",
    "reeses-sodium-options", "dynamic-fps", "dynamicfps", "fpsreducer", "betterfps", "immediatelyfast",
    "entityculling", "modmenu", "xaerominimap", "xaeroworldmap", "xaeros-minimap", "xaeros-world-map",
    "minihud", "itemscroller", "tweakeroo", "litematica", "continuity", "lambdynamiclights",
    "lambdynlights", "betterf3", "mousetweaks", "mouse-tweaks", "controlling", "searchables",
    "notenoughanimations", "not-enough-animations", "skinlayers3d", "3dskinlayers",
    "sound-physics-remastered", "soundphysics", "presencefootsteps", "particular", "cull-leaves",
    "cullleaves", "legendarytooltips", "extremesoundmuffler", "ok-zoomer", "zoomify", "fancymenu",
    "konkrete", "drippyloadingscreen", "ping-wheel", "moreculling", "entity-model-features",
    "entity-texture-features", "citresewn", "fabricskyboxes",
]
CLIENT_RE = re.compile(
    "(?<![a-z0-9])(?:" + "|".join(re.escape(n) for n in CLIENT_ONLY_NAMES) + ")(?![a-z])", re.I)

CLIENT_ONLY_MODIDS = {
    "sodium", "rubidium", "embeddium", "oculus", "iris", "optifine", "optifabric", "indium",
    "reeses-sodium-options", "reeses_sodium_options", "sodium-extra", "sodiumextra", "sodium_extra",
    "embeddiumextra", "embeddium_extra", "sodiumdynamiclights", "dynamic_fps", "dynamicfps",
    "fpsreducer", "betterfps", "immediatelyfast", "entityculling", "modmenu", "xaerominimap",
    "xaeroworldmap", "xaerominimapfair", "xaeroworldmapfair", "minihud", "itemscroller", "tweakeroo",
    "litematica", "continuity", "lambdynlights", "lambdynamiclights", "betterf3", "mousetweaks",
    "controlling", "searchables", "notenoughanimations", "skinlayers3d", "soundphysics",
    "presencefootsteps", "particular", "cullleaves", "legendarytooltips", "extremesoundmuffler",
    "zoomify", "ok_zoomer", "fancymenu", "konkrete", "drippyloadingscreen", "melody", "pingwheel",
    "ping-wheel", "moreculling", "entity_model_features", "entity_texture_features", "citresewn",
    "fabricskyboxes", "toastcontrol", "tooltipfix", "blur", "shouldersurfing", "betteradvancements",
}

SKIP_OVERRIDE_DIRS = {"resourcepacks", "shaderpacks", "texturepacks", "saves", "screenshots", "logs",
                      "crash-reports", "essential"}
SKIP_OVERRIDE_FILES = {"options.txt", "optionsof.txt", "optionsshaders.txt", "servers.dat",
                       "servers.dat_old", "launcher_profiles.json", "usercache.json"}

Side = namedtuple("Side", "client server title")
BuildResult = namedtuple("BuildResult", "server_dir test_ok failures")


def modrinth_sides(jars, log):
    """jar name -> Side, from Modrinth's records (looked up by file hash)."""
    out = {}
    try:
        h2j = {}
        for j in jars:
            h2j.setdefault(sha1_file(j), []).append(j)
        hashes = list(h2j)
        versions = {}
        for i in range(0, len(hashes), 200):
            versions.update(post_json(f"{MODRINTH}/version_files",
                                      {"hashes": hashes[i:i + 200], "algorithm": "sha1"}))
        pids = sorted({v["project_id"] for v in versions.values()})
        projects = {}
        for i in range(0, len(pids), 50):
            q = urllib.parse.quote(json.dumps(pids[i:i + 50]))
            for p in get_json(f"{MODRINTH}/projects?ids={q}"):
                projects[p["id"]] = p
        for h, v in versions.items():
            p = projects.get(v["project_id"])
            if p:
                for j in h2j.get(h, []):
                    out[j.name] = Side(p.get("client_side"), p.get("server_side"), p.get("title", j.name))
        log(f"  Modrinth recognised {len(out)} of {len(jars)} mods")
    except Exception as e:  # noqa: BLE001
        log(f"  (Modrinth lookup unavailable: {e}. Using offline detection only.)")
    return out


def decide_client_mods(index, excludes, sides, learned):
    """-> {ModInfo: reason} for jars that should not be on a server."""
    excl = [e.lower() for e in excludes or []]
    remove, verified = {}, set()
    for info in index.infos:
        name = info.name.lower()
        side = sides.get(info.name)
        reason = None
        if any(e in name for e in excl):
            reason = "excluded by you"
        elif info.env == "client":
            reason = "its own metadata says client-only"
        elif side and side.server == "unsupported":
            reason = f"Modrinth lists '{side.title}' as client-only"
        elif side and side.server in ("required", "optional"):
            verified.add(info)
        elif info.own_ids & learned:
            reason = "an earlier crash showed it is client-only"
        elif info.own_ids & CLIENT_ONLY_MODIDS:
            reason = "known client-only mod"
        elif side is None and CLIENT_RE.search(name):
            reason = "known client-only mod (matched by file name)"
        if reason:
            remove[info] = reason
    pinned, changed = set(), True
    while changed:                                  # dependency cascade
        changed = False
        gone = set()
        for i in remove:
            gone |= i.provided
        for i in index.infos:                       # still provided by a jar we keep -> not gone
            if i not in remove:
                gone -= i.provided
        for info in index.infos:
            if info in remove or info in pinned:
                continue
            hit = info.deps & gone
            if not hit:
                continue
            if info in verified:                    # server-capable mod: keep what it needs
                for r in [r for r in remove if r.provided & hit and remove[r] != "excluded by you"]:
                    del remove[r]
                    pinned.add(r)
                    changed = True
            else:
                remove[info] = f"it requires client-only mod(s): {', '.join(sorted(hit))}"
                changed = True
    return remove


def move_mod(jar: Path, server_dir: Path, bucket: str, reason: str) -> bool:
    """Move a jar out of mods/. False if it's already gone or locked (e.g. the server is running)."""
    if not jar.exists():
        return False
    d = server_dir / BUCKETS[bucket]
    try:
        d.mkdir(exist_ok=True)
        dest = d / jar.name
        if dest.exists():
            dest.unlink()
        shutil.move(str(jar), str(dest))
        with open(d / "REMOVED.txt", "a", encoding="utf-8") as f:
            f.write(f"{jar.name}\t{reason.replace(chr(9), ' ')}\n")
        return True
    except OSError:
        return False


def remove_duplicates(index, server_dir: Path, ctx):
    seen = set()
    for mid, keep, others in index.duplicates():
        for o in others:
            if o.jar in seen or o is keep:
                continue
            seen.add(o.jar)
            if move_mod(o.jar, server_dir, "duplicates", f"duplicate of {keep.name}"):
                ctx.note(f"Removed duplicate {o.name} (kept {keep.name})")


def prune_client_mods(server_dir: Path, ctx: Ctx, excludes=()):
    mods = server_dir / "mods"
    if not mods.is_dir():
        return 0
    # duplicates first (loaders refuse to start with two copies of one mod)
    remove_duplicates(ModIndex(mods), server_dir, ctx)
    index = ModIndex(mods)
    sides = modrinth_sides([i.jar for i in index.infos], ctx.log) if ctx.online else {}
    remove = decide_client_mods(index, excludes, sides, load_learned())
    for info, reason in sorted(remove.items(), key=lambda kv: kv[0].name.lower()):
        if move_mod(info.jar, server_dir, "client_only", reason):
            ctx.note(f"Removed {info.name}: {reason}")
    return len(remove)


# ============================================================================
# Modpack parsing, mod downloads
# ============================================================================


class Pack:
    def __init__(self):
        self.kind = ""
        self.name = "modpack"
        self.mc = ""
        self.loader = "vanilla"
        self.loader_ver = ""
        self.files = []
        self.override_prefixes = []


def parse_modpack(zf: zipfile.ZipFile) -> Pack:
    names = zf.namelist()
    p = Pack()
    if "modrinth.index.json" in names:
        idx = json.loads(zf.read("modrinth.index.json"))
        p.kind = "modrinth"
        p.name = idx.get("name", "modpack")
        deps = idx.get("dependencies", {})
        p.mc = deps.get("minecraft", "")
        for key, loader in (("neoforge", "neoforge"), ("forge", "forge"),
                            ("fabric-loader", "fabric"), ("quilt-loader", "quilt")):
            if key in deps:
                p.loader, p.loader_ver = loader, deps[key]
                break
        for f in idx.get("files", []):
            env = f.get("env") or {}
            if env.get("server") == "unsupported":
                continue
            path = f["path"].replace("\\", "/")
            if path.split("/")[0].lower() in SKIP_OVERRIDE_DIRS:
                continue
            p.files.append({"path": path, "urls": f.get("downloads", []),
                            "sha1": (f.get("hashes") or {}).get("sha1"),
                            "sha512": (f.get("hashes") or {}).get("sha512")})
        p.override_prefixes = [("overrides/", True), ("server-overrides/", False)]
    elif "manifest.json" in names:
        m = json.loads(zf.read("manifest.json"))
        p.kind = "curseforge"
        p.name = m.get("name", "modpack")
        mc = m.get("minecraft", {})
        p.mc = mc.get("version", "")
        loaders = mc.get("modLoaders", [])
        primary = next((l for l in loaders if l.get("primary")), loaders[0] if loaders else None)
        if primary:
            name, _, ver = primary["id"].partition("-")
            if p.mc and ver.startswith(p.mc + "-"):
                ver = ver[len(p.mc) + 1:]
            p.loader, p.loader_ver = name.lower(), ver
        for f in m.get("files", []):
            if f.get("required") is False:          # turned off in the pack
                continue
            p.files.append({"pid": f["projectID"], "fid": f["fileID"]})
        p.override_prefixes = [(m.get("overrides", "overrides").rstrip("/") + "/", True)]
    else:
        raise ValueError("Not a recognised modpack: expected manifest.json (CurseForge) "
                         "or modrinth.index.json (Modrinth) inside the archive.")
    if not p.mc:
        raise ValueError("Could not determine the Minecraft version from the modpack.")
    return p


def extract_overrides(zf, prefix, server_dir: Path, filter_client: bool, log=None):
    n = 0
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith(prefix):
            continue
        rel = info.filename[len(prefix):]
        if not rel:
            continue
        if filter_client:
            first = rel.split("/")[0].lower()
            if first in SKIP_OVERRIDE_DIRS or rel.lower() in SKIP_OVERRIDE_FILES:
                continue
        try:
            dest = safe_join(server_dir, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
        except (ValueError, OSError) as e:
            if log:
                log(f"  ! Skipped {rel}: {e}")
    return n


def fetch_modrinth_file(f, server_dir: Path):
    dest = safe_join(server_dir, f["path"])
    last = None
    for url in f["urls"]:
        try:
            download(url, dest, sha1=f.get("sha1"), sha512=f.get("sha512"))
            return dest.name
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"{f['path']}: {last}")


def fetch_curseforge_file(f, mods_dir: Path, api_key):
    pid, fid = f["pid"], f["fid"]
    errors = []
    if api_key:
        try:
            data = get_json(f"https://api.curseforge.com/v1/mods/{pid}/files/{fid}",
                            {"x-api-key": api_key})["data"]
            name = data["fileName"]
            if not name.lower().endswith(".jar"):
                return f"skipped {name} (not a mod)"
            urls = [data.get("downloadUrl"),
                    f"https://edge.forgecdn.net/files/{fid // 1000}/{fid % 1000}/{urllib.parse.quote(name)}",
                    f"https://mediafilez.forgecdn.net/files/{fid // 1000}/{fid % 1000}/{urllib.parse.quote(name)}"]
            sha1 = next((h["value"] for h in data.get("hashes", []) if h.get("algo") == 1), None)
            for u in filter(None, urls):
                try:
                    download(u, mods_dir / name, sha1=sha1)
                    return name
                except Exception as e:  # noqa: BLE001
                    errors.append(str(e))
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    tmp = mods_dir / f".dl-{pid}-{fid}.tmp"
    try:
        final = download(f"https://www.curseforge.com/api/v1/mods/{pid}/files/{fid}/download", tmp)
        name = urllib.parse.unquote(urllib.parse.urlparse(final).path.rsplit("/", 1)[-1])
        if not name.lower().endswith(".jar"):
            try:
                with zipfile.ZipFile(tmp) as z:
                    names = z.namelist()
            except zipfile.BadZipFile:
                names = []
            if "pack.mcmeta" in names or any(n.startswith("shaders/") for n in names) or not names:
                tmp.unlink(missing_ok=True)
                return f"skipped {name} (not a mod)"
            name = Path(name).stem + ".jar" if name else f"{pid}-{fid}.jar"
        os.replace(tmp, mods_dir / Path(name).name)
        return name
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
        if tmp.exists():
            tmp.unlink()
    raise RuntimeError(f"project {pid} file {fid}: {errors[-1] if errors else 'unknown error'}")


# ============================================================================
# Loader / server installation
# ============================================================================


def run_installer(cmd, cwd: Path, log):
    log("  $ " + " ".join(str(c) for c in cmd))
    proc = subprocess.Popen([str(c) for c in cmd], cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace", **NOWIN)

    def watch():
        while proc.poll() is None:
            if CANCEL.wait(0.5):
                proc.kill()
                return
    threading.Thread(target=watch, daemon=True).start()
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log("    " + line)
    code = proc.wait()
    check_cancel()
    if code != 0:
        raise RuntimeError("Loader installer failed (see log above).")


def install_vanilla(mc, server_dir: Path, log):
    log("Downloading vanilla server jar...")
    manifest = get_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
    entry = next((v for v in manifest["versions"] if v["id"] == mc), None)
    if not entry:
        raise RuntimeError(f"Minecraft {mc} not found in Mojang's version manifest.")
    srv = get_json(entry["url"])["downloads"]["server"]
    download(srv["url"], server_dir / "server.jar", sha1=srv.get("sha1"))


def install_loader(pack: Pack, server_dir: Path, java: str, log):
    mc, loader, ver = pack.mc, pack.loader, pack.loader_ver
    if loader == "vanilla":
        install_vanilla(mc, server_dir, log)
    elif loader == "fabric":
        log(f"Downloading Fabric server launcher (loader {ver})...")
        installers = get_json("https://meta.fabricmc.net/v2/versions/installer")
        inst = next(i for i in installers if i.get("stable"))["version"]
        download(f"https://meta.fabricmc.net/v2/versions/loader/{mc}/{ver}/{inst}/server/jar",
                 server_dir / "fabric-server-launch.jar")
    elif loader == "quilt":
        log(f"Installing Quilt server (loader {ver})...")
        inst = get_json("https://meta.quiltmc.org/v3/versions/installer")[0]["version"]
        jar = server_dir / "quilt-installer.jar"
        download(f"https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer/{inst}/quilt-installer-{inst}.jar", jar)
        run_installer([java, "-jar", jar.name, "install", "server", mc, ver,
                       "--download-server", "--install-dir=."], server_dir, log)
        jar.unlink(missing_ok=True)
        if not (server_dir / "quilt-server-launch.jar").exists():
            raise RuntimeError("The Quilt installer didn't create a server (see the messages above).")
    elif loader == "forge":
        log(f"Installing Forge {mc}-{ver} server...")
        full = f"{mc}-{ver}"
        jar = server_dir / "forge-installer.jar"
        try:
            download(f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full}/forge-{full}-installer.jar", jar)
        except Exception:  # noqa: BLE001
            full = f"{mc}-{ver}-{mc}"
            download(f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full}/forge-{full}-installer.jar", jar)
        run_installer([java, "-jar", jar.name, "--installServer"], server_dir, log)
        jar.unlink(missing_ok=True)
        (server_dir / "forge-installer.jar.log").unlink(missing_ok=True)
    elif loader == "neoforge":
        log(f"Installing NeoForge {ver} server...")
        jar = server_dir / "neoforge-installer.jar"
        if mc == "1.20.1":
            full = f"{mc}-{ver}"
            url = f"https://maven.neoforged.net/releases/net/neoforged/forge/{full}/forge-{full}-installer.jar"
        else:
            url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{ver}/neoforge-{ver}-installer.jar"
        download(url, jar)
        run_installer([java, "-jar", jar.name, "--installServer"], server_dir, log)
        jar.unlink(missing_ok=True)
    else:
        raise RuntimeError(f"Unsupported mod loader: {loader}")


# ============================================================================
# Launch commands, start scripts, RAM
# ============================================================================


def launch_spec(server_dir: Path):
    run_sh = server_dir / "run.sh"
    if run_sh.exists():
        m = re.search(r"@(libraries/\S+?unix_args\.txt)", run_sh.read_text(encoding="utf-8", errors="ignore"))
        if m:
            return {"mode": "args", "args": m.group(1)}
    cands = []
    for pat in ("fabric-server-launch.jar", "quilt-server-launch.jar"):
        cands += list(server_dir.glob(pat))
    cands += [j for j in server_dir.glob("forge-*.jar") if "installer" not in j.name]
    cands += list(server_dir.glob("server.jar")) + list(server_dir.glob("minecraft_server*.jar"))
    if cands:
        return {"mode": "jar", "jar": cands[0].name}
    return None


def read_ram_mb(server_dir: Path) -> int:
    for f in ("user_jvm_args.txt", "start.sh"):
        p = server_dir / f
        if p.exists():
            m = re.search(r"-Xmx(\d+)([MmGg])", p.read_text(encoding="utf-8", errors="ignore"))
            if m:
                return int(m.group(1)) * (1024 if m.group(2).upper() == "G" else 1)
    return 4096


def _q(java):
    return java if java == "java" else f'"{java}"'


def write_start_scripts(server_dir: Path, ram_mb: int, java_exec="java", log=print):
    spec = launch_spec(server_dir)
    if not spec:
        raise RuntimeError("Could not find a server jar to launch after installation.")
    xmx, xms = fmt_ram(ram_mb), fmt_ram(min(ram_mb, max(ram_mb // 2, 512)))
    bat_java = java_exec
    la = os.environ.get("LOCALAPPDATA")
    if IS_WIN and la and java_exec.lower().startswith(la.lower()):
        bat_java = "%LOCALAPPDATA%" + java_exec[len(la):]     # survives non-ASCII user names
    if spec["mode"] == "args":
        jvm = server_dir / "user_jvm_args.txt"
        old = jvm.read_text(encoding="utf-8", errors="ignore").splitlines() if jvm.exists() else \
            ["# Edit these to change memory / JVM flags"]
        keep = [l for l in old if not re.match(r"\s*-Xm[sx]", l)]
        write_file(jvm, "\n".join(keep + [f"-Xms{xms}", f"-Xmx{xmx}"]) + "\n")
        unix = spec["args"]
        win = unix.replace("unix_args.txt", "win_args.txt")
        sh = (f'#!/usr/bin/env bash\ncd "$(dirname "$0")"\n'
              f'exec {_q(java_exec)} @user_jvm_args.txt @{unix} nogui "$@"\n')
        bat = (f'@echo off\r\nchcp 65001 >nul\r\ncd /d "%~dp0"\r\n{_q(bat_java)} @user_jvm_args.txt @{win} nogui %*\r\npause\r\n')
    else:
        flags = f"-Xms{xms} -Xmx{xmx}"
        sh = f'#!/usr/bin/env bash\ncd "$(dirname "$0")"\nexec {_q(java_exec)} {flags} -jar "{spec["jar"]}" nogui\n'
        bat = (f'@echo off\r\nchcp 65001 >nul\r\ncd /d "%~dp0"\r\n{_q(bat_java)} {flags} -jar "{spec["jar"]}" nogui\r\npause\r\n')
    write_file(server_dir / "start.sh", sh, "\n")
    write_file(server_dir / "start.bat", bat, "")
    try:
        (server_dir / "start.sh").chmod(0o755)
    except OSError:
        pass
    log("Wrote start.sh and start.bat")


def launch_cmd(server_dir: Path, ctx: Ctx):
    spec = launch_spec(server_dir)
    if not spec:
        raise RuntimeError("No server jar found to launch.")
    if spec["mode"] == "args":
        jvm = server_dir / "user_jvm_args.txt"
        if not jvm.exists():
            write_file(jvm, f"-Xms{fmt_ram(min(ctx.ram_mb, max(ctx.ram_mb // 2, 512)))}\n-Xmx{fmt_ram(ctx.ram_mb)}\n")
        args = spec["args"].replace("unix_args.txt", "win_args.txt") if IS_WIN else spec["args"]
        return [ctx.java, "@user_jvm_args.txt", "@" + args, "nogui"]
    return [ctx.java, f"-Xms{fmt_ram(min(ctx.ram_mb, max(ctx.ram_mb // 2, 512)))}", f"-Xmx{fmt_ram(ctx.ram_mb)}",
            "-jar", spec["jar"], "nogui"]


def set_ram(server_dir: Path, ctx: Ctx, mb: int):
    ctx.ram_mb = mb
    write_start_scripts(server_dir, mb, ctx.java_script, lambda s: None)
    save_state(server_dir, ctx)


# ============================================================================
# Fetching a missing dependency from Modrinth
# ============================================================================

DEP_ALIASES = {"fabric": "fabric-api", "fabricapi": "fabric-api", "fabric_api": "fabric-api",
               "geckolib3": "geckolib", "geckolib4": "geckolib"}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def modrinth_loaders(loader, mc=""):
    if loader == "neoforge" and mc == "1.20.1":
        return ["neoforge", "forge"]
    return {"forge": ["forge"], "neoforge": ["neoforge"], "fabric": ["fabric"],
            "quilt": ["quilt", "fabric"]}.get(loader, [loader])


def modrinth_find_project(modid, mc, loader):
    slug = DEP_ALIASES.get(modid, modid)
    try:
        return get_json(f"{MODRINTH}/project/{urllib.parse.quote(slug)}")
    except urllib.error.HTTPError:
        pass
    facets = [["project_type:mod"]]
    if mc:
        facets.append([f"versions:{mc}"])
    facets.append([f"categories:{l}" for l in modrinth_loaders(loader, mc)])
    q = urllib.parse.quote(modid)
    f = urllib.parse.quote(json.dumps(facets))
    hits = get_json(f"{MODRINTH}/search?query={q}&limit=5&facets={f}").get("hits", [])
    n = _norm(modid)
    for h in hits:
        if n in (_norm(h.get("slug", "")), _norm(h.get("title", ""))):
            return get_json(f"{MODRINTH}/project/{h['project_id']}")
    for h in hits:
        if n in _norm(h.get("slug", "")):
            return get_json(f"{MODRINTH}/project/{h['project_id']}")
    return None


def install_dependency(modid, ctx: Ctx, mods_dir: Path):
    """-> ('installed', filename) | ('client_only', title) | ('not_found', None)"""
    proj = modrinth_find_project(modid, ctx.mc, ctx.loader)
    if not proj:
        return "not_found", None
    if proj.get("server_side") == "unsupported":
        return "client_only", proj.get("title", modid)
    gv = urllib.parse.quote(json.dumps([ctx.mc]))
    ld = urllib.parse.quote(json.dumps(modrinth_loaders(ctx.loader, ctx.mc)))
    versions = get_json(f"{MODRINTH}/project/{proj['id']}/version?game_versions={gv}&loaders={ld}")
    if not versions:
        return "not_found", None
    versions.sort(key=lambda v: (v.get("version_type") == "release", v.get("date_published", "")), reverse=True)
    v = versions[0]
    f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
    download(f["url"], mods_dir / f["filename"], sha1=(f.get("hashes") or {}).get("sha1"),
             sha512=(f.get("hashes") or {}).get("sha512"))
    return "installed", f["filename"]


# ============================================================================
# Log analyzer
# ============================================================================


class Finding:
    def __init__(self, kind, message, action="manual", confidence="high", info=None, **data):
        self.kind, self.message, self.action = kind, message, action
        self.confidence, self.info, self.data = confidence, info, data

    @property
    def key(self):
        if self.action == "remove":
            return ("remove", self.info.name)
        if self.action == "install":
            return ("install", self.data.get("modid"))
        if self.action in ("ram_up", "ram_down", "eula"):
            return (self.action,)
        return (self.kind, self.message[:80])


CLIENT_PKGS = ("net.minecraft.client", "net.minecraftforge.client", "net.neoforged.neoforge.client",
               "net.fabricmc.fabric.api.client", "net.fabricmc.fabric.impl.client", "com.mojang.blaze3d",
               "com.mojang.realmsclient", "org.lwjgl.opengl", "org.lwjgl.glfw")
CLIENT_INTERMEDIARY = re.compile(r"net\.minecraft\.class_(?:310|437|332|327|638|746|304|4587|287)$")


def is_client_class(c):
    c = c.replace("/", ".")
    return c.startswith(CLIENT_PKGS) or bool(CLIENT_INTERMEDIARY.match(c))


def short_class(c):
    return c.replace("/", ".").rsplit(".", 1)[-1]


RX_DIST = re.compile(r"Attempted to load class (\S+) for invalid dist DEDICATED_SERVER")
RX_FABRIC_ENV = re.compile(r"Cannot load class (\S+) in environment type SERVER")
RX_NOCLASS = re.compile(r"(NoClassDefFoundError|ClassNotFoundException):\s*([\w./$]+)")
RX_ENTRYPOINT = re.compile(r"Could not execute entrypoint stage '(\w+)' due to errors, provided by '([\w\-.]+)'")
RX_MIXIN_APPLY = re.compile(r"Mixin apply for mod ([\w\-.]+) failed")
RX_MIXIN_CFG = re.compile(r"\b([\w.\-]+\.mixins?\.json|mixins?\.[\w.\-]+\.json)")
RX_LOADFAIL_LINE = re.compile(r"ModLoadingException: (.*?) \(([\w\-.]+)\) has failed to load correctly")
RX_FORGE_DEP = re.compile(r"Mod ID: '([^']+)', Requested by: '([^']+)', Expected range: '([^']*)', Actual version: '([^']*)'")
RX_FABRIC_DEP = re.compile(r"Mod '([^']*)' \(([\w\-.]+)\) \S+ requires (?P<range>.+?) of "
                           r"(?:(?:mod )?'[^']*' \((?P<id>[\w.\-]+)\)|(?:mod )?'?(?P<id2>[\w.\-]+)'?), "
                           r"(?P<kind>which is missing|but only the wrong version is present)")
RX_NEO_DEP = re.compile(r"[Mm]od '?([\w\-.]+)'? requires '?([\w\-.]+)'? ")
RX_NEO_MISSING = re.compile(r"Currently, '?([\w\-.]+)'? is not installed")
RX_BROKEN_JAR = re.compile(r"Mod file (\S+?\.jar) is missing")
RX_JAVA_VER = re.compile(r"class file version (\d+)\.\d+")
RX_JAVA_VER2 = re.compile(r"Unsupported class file major version (\d+)")
RX_OOM_UP = re.compile(r"java\.lang\.OutOfMemoryError: (?:Java heap space|GC overhead limit exceeded)")
RX_OOM_DOWN = re.compile(r"Could not reserve enough space|There is insufficient memory for the Java Runtime|Invalid maximum heap size")
RX_EULA = re.compile(r"You need to agree to the EULA")
RX_PORT = re.compile(r"FAILED TO BIND TO PORT|Failed to bind to port|Address already in use")
RX_SUSPECT = re.compile(r"Suspected Mods?:\s*(.*?)(?:\n\s*\n|\Z)", re.S)
RX_FAILURE = re.compile(r"Exception|Crash|crashed|FATAL|Failed to start|has failed to load|Error:", re.I)
RX_DONE = re.compile(r'Done \(\d+[.,]\d+s\)! For help, type "help"')
RX_LOW_LEVEL = re.compile(r"/(?:WARN|INFO|DEBUG|TRACE)\]|\[(?:WARN|INFO|DEBUG|TRACE)\]")
RX_STACK_LINE = re.compile(r"\s+at |\s*Caused by:|\s+\.\.\. \d+ more|\s*Suppressed:")
RX_MIXIN_FATAL = re.compile(r"InvalidMixinException|Critical injection failure|MixinApplyError|MixinTransformerError|"
                            r"InvalidInjectionException")


def analyze_text(text: str, server_dir: Path, ctx: Ctx):
    idx = ModIndex(server_dir / "mods")
    lines = text.splitlines()
    found = {}

    def add(f):
        old = found.get(f.key)
        if not old or CONF_RANK[f.confidence] > CONF_RANK[old.confidence]:
            found[f.key] = f

    def win(i):
        return lines[max(0, i - 3): i + 70]

    def stack(i):
        """The exception at line i plus its stack trace (not unrelated lines further down)."""
        out = lines[max(0, i - 3): i + 1]
        for l in lines[i + 1: i + 150]:
            if RX_STACK_LINE.match(l):
                out.append(l)
            else:
                break
        return out

    def fatal_follows(i):
        return any(RX_MIXIN_FATAL.search(l) for l in lines[i: i + 6])

    started_ok = bool(RX_DONE.search(text))       # the server reached "Done!" in this log

    for mid, keep, others in idx.duplicates():
        for o in others:
            add(Finding("duplicate", f"'{o.name}' is a second copy of '{keep.name}'; keeping the newer one",
                        "remove", "high", o))

    unresolved_client = False
    for i, line in enumerate(lines):
        # --- client-only code running on a server -------------------------------------------
        cls = None
        quiet = bool(RX_LOW_LEVEL.search(line))       # WARN/INFO lines are not what crashed the server
        m = RX_DIST.search(line) or RX_FABRIC_ENV.search(line)
        if m and not quiet:
            cls = m.group(1)
        elif not quiet:
            m = RX_NOCLASS.search(line)
            if m and is_client_class(m.group(2)):
                cls = m.group(2)
        if cls:
            info = idx.blame(stack(i))
            if info:
                add(Finding("client_mod", f"'{info.name}' needs client-only game code ({short_class(cls)}), "
                            f"so it is a client-side mod and can't run on a server", "remove", "high", info,
                            learn=info not in idx.libraries()))
            else:
                unresolved_client = True
        # --- Fabric entrypoint / mixin failures -----------------------------------------------
        m = RX_ENTRYPOINT.search(line)
        if m:
            info = idx.by_modid(m.group(2))
            if info:
                client = m.group(1) == "client"
                add(Finding("client_mod" if client else "mod_failed",
                            f"'{info.name}' crashed while starting ({m.group(1)} entrypoint)", "remove",
                            "high" if client else "medium", info))
        m = RX_MIXIN_APPLY.search(line)
        mixin_line = m or (re.search(r"mixin", line, re.I) and re.search(r"FAILED|failed|not found|Critical injection", line)
                           and RX_FROM_MOD.search(line))
        if mixin_line and quiet and not fatal_follows(i):
            mixin_line = None                          # e.g. "@Mixin target ... was not found" warnings are harmless
        if mixin_line:
            mid = m.group(1) if m else RX_FROM_MOD.search(line).group("id")
            info = idx.by_modid(mid)
            if not info:
                cm = RX_MIXIN_CFG.search(line)
                info = idx.for_mixin(cm.group(1)) if cm else None
            if info and mid not in BUILTIN_IDS:
                nf = "was not found" in line or is_client_class(line.split("->")[-1].split(":")[0].strip())
                add(Finding("client_mod" if nf else "mod_failed",
                            f"'{info.name}' failed to patch the game ({'client-only target' if nf else 'incompatible'})",
                            "remove", "high" if nf else "medium", info))
        # --- Forge-style load failures --------------------------------------------------------
        m = RX_LOADFAIL_LINE.search(line) or RX_MODID_FAIL.search(line)
        if m:
            mid = m.group(2) if m.re is RX_LOADFAIL_LINE else m.group("id")
            info = idx.by_modid(mid)
            if info and mid not in BUILTIN_IDS:
                add(Finding("mod_failed", f"'{info.name}' failed to load", "remove", "medium", info))
        # --- missing / wrong dependencies ---------------------------------------------------------
        m = RX_FORGE_DEP.search(line)
        if m:
            dep, req, exp, act = m.groups()
            rinfo = idx.by_modid(req)
            if act.strip("[]").upper() == "MISSING":
                if dep not in BUILTIN_IDS:
                    add(Finding("missing_dep", f"'{req}' needs the mod '{dep}', which is missing", "install",
                                "high", rinfo, modid=dep, requester=req))
            elif dep == "minecraft" and rinfo:
                add(Finding("wrong_version", f"'{rinfo.name}' was built for {dep} {exp}, but this server has {act}",
                            "remove", "medium", rinfo))
            elif dep in ("forge", "neoforge"):
                add(Finding("loader_too_old", f"'{req}' needs {dep} {exp}, but this server has {act}. Use a newer "
                            f"{dep} version for this server (or remove '{req}').", "manual", "high"))
            else:
                add(Finding("version_mismatch", f"'{req}' needs {dep} {exp}, but {act} is installed. "
                            f"Update or replace '{dep}'.", "manual", "low"))
        m = RX_FABRIC_DEP.search(line)
        if m:
            req, dep, kind = m.group(2), m.group("id") or m.group("id2"), m.group("kind")
            rinfo = idx.by_modid(req)
            if kind == "which is missing":
                if dep not in BUILTIN_IDS:
                    add(Finding("missing_dep", f"'{req}' needs the mod '{dep}', which is missing", "install",
                                "high", rinfo, modid=dep, requester=req))
            elif dep == "minecraft" and rinfo:
                add(Finding("wrong_version", f"'{rinfo.name}' doesn't match this server's Minecraft version",
                            "remove", "medium", rinfo))
            elif dep in ("fabricloader", "fabric-loader", "quilt_loader"):
                add(Finding("loader_too_old", f"'{req}' needs a newer {dep}. Use a newer loader version for this "
                            f"server (or remove '{req}').", "manual", "high"))
            else:
                add(Finding("version_mismatch", f"'{req}' needs a different version of '{dep}'. Update or "
                            f"replace '{dep}'.", "manual", "low"))
        m = RX_NEO_DEP.search(line)
        if m and i + 1 < len(lines):
            m2 = RX_NEO_MISSING.search(lines[i + 1])
            if m2 and m2.group(1) == m.group(2) and m.group(2) not in BUILTIN_IDS:
                add(Finding("missing_dep", f"'{m.group(1)}' needs the mod '{m.group(2)}', which is missing",
                            "install", "high", idx.by_modid(m.group(1)), modid=m.group(2), requester=m.group(1)))
        m = RX_BROKEN_JAR.search(line)
        if m:
            info = idx.by_jarname(Path(m.group(1)).name)
            if info:
                add(Finding("wrong_loader", f"'{info.name}' isn't a valid mod for this loader", "remove",
                            "medium", info))
        # --- environment problems ---------------------------------------------------------------
        m = RX_JAVA_VER.search(line) or RX_JAVA_VER2.search(line)
        if m and ("UnsupportedClassVersion" in line or "Unsupported class file" in line or
                  "more recent version of the Java" in line):
            need = int(m.group(1)) - 44
            add(Finding("java_version", f"This server needs Java {need} or newer", "java", "high", java=need))
        if RX_OOM_UP.search(line):
            add(Finding("oom", "The server ran out of memory; giving it more RAM", "ram_up", "high"))
        if RX_OOM_DOWN.search(line):
            add(Finding("oom_low", "The RAM setting is higher than this computer can give; lowering it",
                        "ram_down", "high"))
        if RX_EULA.search(line):
            add(Finding("eula", "The Minecraft EULA hasn't been accepted (eula.txt)", "eula", "high"))
        if RX_PORT.search(line):
            add(Finding("port", "Port 25565 is already in use - another server is probably still running. "
                        "Close it, or change server-port in server.properties.", "manual", "high"))

    suspects = []
    for m in RX_SUSPECT.finditer(text):
        for mid in re.findall(r"\(([\w\-.]+)\)", m.group(1)):
            i = idx.by_modid(mid)
            if i and mid not in BUILTIN_IDS and i not in suspects:
                suspects.append(i)
    if unresolved_client and not any(f.kind == "client_mod" for f in found.values()):
        for i in suspects[:3]:
            add(Finding("client_mod", f"'{i.name}' is suspected of using client-only code", "remove", "medium", i))
        if not suspects:
            add(Finding("client_unknown", "A client-only mod is crashing the server but I couldn't tell which one "
                        "from this log.", "manual", "low"))

    actionable = [f for f in found.values() if f.action != "manual"]
    if not actionable and RX_FAILURE.search(text):
        causes = [i for i, l in enumerate(lines) if l.lstrip().startswith("Caused by:")]
        info = idx.blame(lines[causes[-1]: causes[-1] + 40]) if causes else None
        if info:
            add(Finding("mod_crash", f"'{info.name}' is the most likely cause of the crash", "remove", "low", info))
        elif suspects:
            for i in suspects[:2]:
                add(Finding("mod_crash", f"'{i.name}' is listed as a suspect in the crash report", "remove",
                            "low", i))
    if not [f for f in found.values() if f.action != "manual" or f.confidence != "low"] and RX_FAILURE.search(text):
        ex = [l.strip() for l in lines if re.search(r"\bERROR\b|FATAL|Caused by:|Exception", l)][:6]
        add(Finding("unknown", "I couldn't identify the problem automatically." +
                    ("\n      First errors in the log:\n        " + "\n        ".join(e[:160] for e in ex) if ex else ""),
                    "manual", "low"))
    if started_ok:
        # The server got to "Done!", so nothing in this log stopped it from starting. Don't remove mods
        # automatically (or learn from it) on the strength of messages printed along the way.
        for f in found.values():
            if f.action == "remove" and f.kind != "duplicate":
                f.confidence = "low"
                f.data["learn"] = False
    return sorted(found.values(), key=lambda f: -CONF_RANK[f.confidence])


def apply_findings(findings, server_dir: Path, ctx: Ctx):
    """Apply fixes. Returns (changed: bool, manual: list[str])."""
    changed, manual = False, []
    min_conf = 0 if ctx.aggressive else 1
    for f in findings:
        if f.action == "remove":
            if CONF_RANK[f.confidence] < min_conf:
                manual.append(f"{f.message} (not removed automatically - rerun with aggressive mode to remove it)")
                continue
            if not f.info or not f.info.jar.exists():
                continue
            bucket = {"client_mod": "client_only", "duplicate": "duplicates"}.get(f.kind, "problem")
            if not move_mod(f.info.jar, server_dir, bucket, f.message):
                manual.append(f"Couldn't move {f.info.name} out of the mods folder. Close the server if it's "
                              f"running, then move it yourself.")
                continue
            ctx.note(f"Removed {f.info.name}: {f.message}")
            if f.kind == "client_mod" and f.confidence == "high" and f.data.get("learn"):
                learn_client_modids(f.info.own_ids)
            changed = True
        elif f.action == "install":
            modid = f.data["modid"]
            if not ctx.online:
                manual.append(f"{f.message}. Download '{modid}' manually (offline mode).")
                continue
            try:
                res, val = install_dependency(modid, ctx, server_dir / "mods")
            except Exception as e:  # noqa: BLE001
                res, val = "error", str(e)
            if res == "installed":
                ctx.note(f"Installed missing dependency '{modid}' ({val})")
                changed = True
            elif res == "client_only" and f.info and f.info.jar.exists():
                if move_mod(f.info.jar, server_dir, "client_only", f"it needs '{modid}', which is client-only"):
                    ctx.note(f"Removed {f.info.name}: it needs '{modid}', which is client-only")
                    changed = True
            elif ctx.aggressive and f.info and f.info.jar.exists():
                if move_mod(f.info.jar, server_dir, "problem", f"it needs '{modid}', which couldn't be found"):
                    ctx.note(f"Removed {f.info.name}: it needs '{modid}', which couldn't be found")
                    changed = True
            else:
                manual.append(f"{f.message}. I couldn't find it automatically - download '{modid}' for "
                              f"Minecraft {ctx.mc} ({ctx.loader}) and put it in the mods folder.")
        elif f.action == "java":
            need = f.data.get("java", 0)
            j = get_java(need, ctx.log, ctx.online) if need > 0 else None
            if j and j[1] >= need and j[0] != ctx.java:
                ctx.java = j[0]
                pj = shutil.which("java")
                ctx.java_script = "java" if pj and java_major(pj) == j[1] else j[0]
                set_ram(server_dir, ctx, ctx.ram_mb)          # rewrites the start scripts with the new Java
                ctx.note(f"Switched to Java {j[1]} ({j[0]})")
                changed = True
            else:
                manual.append(f"This server needs Java {need} or newer. Install it from https://adoptium.net "
                              f"and start the server again.")
        elif f.action == "ram_up":
            cap = int((system_ram_mb() or 16384) * 0.75)
            new = min(ctx.ram_mb + 2048, cap // 512 * 512)
            if new > ctx.ram_mb:
                set_ram(server_dir, ctx, new)
                ctx.note(f"Raised server RAM to {fmt_ram(new)}")
                changed = True
            else:
                manual.append("The server needs more RAM than this computer can spare. Close other programs, "
                              "add RAM, or run it on a bigger machine.")
        elif f.action == "ram_down":
            new = max(2048, int((system_ram_mb() or 8192) * 0.6))
            if new < ctx.ram_mb:
                set_ram(server_dir, ctx, new)
                ctx.note(f"Lowered server RAM to {fmt_ram(new)}")
                changed = True
            else:
                manual.append("Memory could not be reserved. Close other programs or install 64-bit Java.")
        elif f.action == "eula":
            if ctx.accept_eula:
                write_file(server_dir / "eula.txt", "# https://aka.ms/MinecraftEULA\neula=true\n")
                ctx.note("Set eula=true (you accepted the EULA)")
                changed = True
            else:
                manual.append("Accept the Minecraft EULA (https://aka.ms/MinecraftEULA) by setting eula=true in eula.txt.")
        else:
            manual.append(f.message)
    return changed, manual


def print_findings(findings, ctx):
    if not findings:
        ctx.log("  Nothing wrong found in this log.")
    for f in findings:
        ctx.log(f"  [{f.confidence}] {f.message}")


# ============================================================================
# Test launch + automatic fix loop
# ============================================================================

RX_ECHO = re.compile(r"ERROR|FATAL|Exception|Caused by|Done \(|Starting minecraft|Loading Minecraft|Preparing")


def run_server_once(server_dir: Path, ctx: Ctx, timeout=1200):
    """Start the server, wait for 'Done!' (then stop it) or a failure. -> (ok, output_text)"""
    cmd = launch_cmd(server_dir, ctx)
    ctx.log("  $ " + " ".join(cmd))
    start = time.time()
    proc = subprocess.Popen(cmd, cwd=server_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
                            **NOWIN)
    q, lines = queue.Queue(), []

    def reader():
        for line in proc.stdout:
            q.put(line.rstrip("\n"))
        q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    ok, echoed, last_beat, deadline = False, 0, time.time(), start + timeout
    while True:
        if CANCEL.is_set() or SKIP_TEST.is_set():
            proc.kill()
            proc.wait(timeout=30)
            if SKIP_TEST.is_set():
                raise TestSkipped()
            raise Cancelled("Stopped by you.")
        try:
            item = q.get(timeout=1)
        except queue.Empty:
            now = time.time()
            if now - last_beat > 30:
                ctx.log(f"  ...still starting ({int(now - start)}s)")
                last_beat = now
            if now > deadline:
                ctx.log("  Timed out waiting for the server; stopping it.")
                proc.kill()
                lines.append("TIMEOUT waiting for server start")
                break
            continue
        if item is None:
            break
        lines.append(item)
        if time.time() > deadline:
            ctx.log("  Timed out waiting for the server; stopping it.")
            proc.kill()
            lines.append("TIMEOUT waiting for server start")
            break
        if RX_DONE.search(item) and not ok:
            ok = True
            deadline = time.time() + 120
            try:
                proc.stdin.write("stop\n")
                proc.stdin.flush()
            except OSError:
                pass
        if RX_ECHO.search(item) and echoed < 60:
            echoed += 1
            ctx.log("    | " + item[:180])
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
    return ok, "\n".join(lines), start


def collect_failure_text(server_dir: Path, output: str, since: float) -> str:
    parts = [output]
    try:
        ll = server_dir / "logs/latest.log"
        if ll.stat().st_mtime >= since - 2:
            parts.append(ll.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        pass
    cr = server_dir / "crash-reports"
    if cr.is_dir():
        reports = [p for p in cr.glob("crash-*.txt") if p.stat().st_mtime >= since - 5]
        if reports:
            parts.append(max(reports, key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def _fix_key(f):
    """What a fix does, so the same fix isn't applied over and over when it doesn't help."""
    return (f.action, f.kind, f.data.get("modid"), f.info.name if f.info else None, f.data.get("java"))


def server_port(server_dir: Path) -> int:
    try:
        for line in (server_dir / "server.properties").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("server-port="):
                return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return 25565


def port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if not IS_WIN:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def test_and_fix(server_dir: Path, ctx: Ctx, max_attempts=8, timeout=1200):
    """Launch, read the crash, fix, repeat. -> True when the server reached 'Done!', None when skipped.

    Stops early when a crash only calls for fixes that were already tried, so it never goes round in circles."""
    all_manual, tried = [], set()
    SKIP_TEST.clear()
    port = server_port(server_dir)
    for attempt in range(1, max_attempts + 1):
        ctx.log(f"\nTest launch {attempt}/{max_attempts} ...")
        remove_duplicates(ModIndex(server_dir / "mods"), server_dir, ctx)
        check_cancel()
        if port_in_use(port):
            msg = (f"Port {port} is already in use, so the server can't start. Another Minecraft server (or a copy "
                   f"of this one) is probably running. Close it, then start this server from its page.")
            ctx.log("  " + msg)
            ctx.notes.append(msg)
            return False
        try:
            ok, out, started = run_server_once(server_dir, ctx, timeout)
        except TestSkipped:
            ctx.log("  Skipped the test start (you can start the server yourself from its page).")
            ctx.note("Test start skipped")
            return None
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            ctx.log(f"  Could not start the server: {e}")
            return False
        if ok:
            ctx.log("  The server started successfully!")
            ctx.note("Test launch succeeded: the server reaches 'Done!' with the current mods")
            for m in all_manual:
                ctx.log("  NOTE: " + m)
            return True
        ctx.log("  The server did not start. Reading the crash...")
        findings = analyze_text(collect_failure_text(server_dir, out, started), server_dir, ctx)
        print_findings(findings, ctx)
        fresh = [f for f in findings if _fix_key(f) not in tried or f.action in ("manual",)]
        for f in findings:
            if f not in fresh:
                m = f"Already tried: {f.message}. It didn't solve the problem."
                if m not in all_manual:
                    all_manual.append(m)
        tried.update(_fix_key(f) for f in fresh if f.action != "manual")
        changed, manual = apply_findings(fresh, server_dir, ctx)
        all_manual += [m for m in manual if m not in all_manual]
        if not changed:
            ctx.log("\nI couldn't fix the remaining problem automatically:")
            for m in all_manual:
                ctx.log("  - " + m)
            ctx.notes.extend(all_manual)
            return False
    ctx.log(f"\nStopped after {max_attempts} attempts. Open the server's page and press 'Fix it for me', "
            f"or start it to see what happens.")
    return False


# ============================================================================
# Fix an existing server from a log file
# ============================================================================


def fix_from_log(log_paths, server_dir, accept_eula=False, aggressive=False, report_only=False,
                 retest=False, online=True, log=print, result=None):
    server_dir = Path(server_dir)
    if not (server_dir / "mods").is_dir():
        raise FileNotFoundError(f"No 'mods' folder in {server_dir}. Pick the server folder (the one that contains mods/).")
    if isinstance(log_paths, (str, Path)):
        log_paths = [log_paths]
    text = "\n".join(Path(p).read_text(encoding="utf-8", errors="ignore") for p in log_paths)
    ctx = ctx_for_server(server_dir, text, accept_eula, aggressive, log, online)
    log(f"Server: Minecraft {ctx.mc or '?'} / {ctx.loader or '?'}")
    log("Reading the log...")
    findings = analyze_text(text, server_dir, ctx)
    print_findings(findings, ctx)
    if report_only:
        log("\n(Report only - nothing was changed.)")
        return False
    changed, manual = apply_findings(findings, server_dir, ctx)
    if manual:
        log("\nThings you need to do yourself:")
        for m in manual:
            log("  - " + m)
    ok = None
    if retest:
        if not accept_eula:
            log("\nSkipping the test launch: tick/accept the Minecraft EULA first.")
        elif changed or not manual:
            if ctx.mc and not find_java(recommended_java(ctx.mc)):
                j = get_java(recommended_java(ctx.mc), log, online)
                if j:
                    ctx.java = j[0]
            ok = test_and_fix(server_dir, ctx)
    elif changed:
        log("\nFixes applied. Start the server again to check.")
    write_report(server_dir, ctx, "Fix from log")
    remember_server(server_dir)
    if result is not None:
        result.update(tested=ok is not None, changed=bool(changed), manual=len(manual))
    return ok if ok is not None else changed


def write_report(server_dir: Path, ctx: Ctx, title):
    try:
        with open(server_dir / REPORT_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n=== {title} - {datetime.now():%Y-%m-%d %H:%M} ===\n")
            f.write(f"Minecraft {ctx.mc}, {ctx.loader} {ctx.loader_ver}, RAM {fmt_ram(ctx.ram_mb)}\n")
            for n in ctx.notes:
                f.write(f"- {n}\n")
            f.write("Removed mods are in the mods_removed_* folders (see REMOVED.txt in each). "
                    "Move a jar back into mods/ to undo.\n")
    except OSError:
        pass


# ============================================================================
# Main build routine
# ============================================================================


def build_server(pack_path, out_dir, ram="6G", accept_eula=False, curse_key=None, excludes=(),
                 keep_client_mods=False, test_run=True, aggressive=False, max_attempts=8,
                 online=True, log=print):
    pack_path = Path(pack_path)
    if not pack_path.is_file():
        raise FileNotFoundError(f"Modpack file not found: {pack_path}")
    ram_mb = parse_ram_mb(ram)
    with zipfile.ZipFile(pack_path) as zf:
        pack = parse_modpack(zf)
        log(f"Pack: {pack.name}  [{pack.kind}]")
        log(f"Minecraft {pack.mc}  |  loader: {pack.loader} {pack.loader_ver}  |  {len(pack.files)} mod files")

        want = recommended_java(pack.mc)
        found = get_java(want, log, online)
        need_java = pack.loader in ("forge", "neoforge", "quilt")
        if not found:
            msg = (f"Java {want} isn't installed and couldn't be downloaded. Install it from "
                   f"https://adoptium.net and try again.")
            if need_java:
                raise RuntimeError(msg)
            log("WARNING: " + msg + " The server will be built but not test-started.")
            test_run = False
            java_exec = "java"
        else:
            java_exec, jmajor, exact = found
            log(f"Using Java {jmajor} ({java_exec})")
            if not exact:
                log(f"WARNING: Minecraft {pack.mc} is meant for Java {want}; Java {jmajor} may cause trouble.")
        path_java = shutil.which("java")
        script_java = "java" if path_java and java_major(path_java) == java_major(java_exec) else java_exec
        if script_java != "java":
            log("NOTE: the start scripts use a specific Java path because 'java' on your PATH is a different version.")

        server_dir = Path(out_dir).resolve()
        server_dir.mkdir(parents=True, exist_ok=True)
        (server_dir / "mods").mkdir(exist_ok=True)
        mods_dir = server_dir / "mods"

        log(f"\nDownloading {len(pack.files)} mods...")
        failures, done = [], 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            if pack.kind == "modrinth":
                futs = {ex.submit(fetch_modrinth_file, f, server_dir): f["path"] for f in pack.files}
            else:
                futs = {ex.submit(fetch_curseforge_file, f, mods_dir, curse_key): f"{f['pid']}/{f['fid']}"
                        for f in pack.files}
            for fut in as_completed(futs):
                if CANCEL.is_set():
                    ex.shutdown(wait=False, cancel_futures=True)
                    raise Cancelled("Stopped by you.")
                done += 1
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001
                    failures.append((futs[fut], str(e)))
                    log(f"  ! FAILED {futs[fut]}: {e}")
                if done % 10 == 0 or done == len(futs):
                    log(f"  {done}/{len(futs)} done")
        for prefix, filt in pack.override_prefixes:
            n = extract_overrides(zf, prefix, server_dir, filt, log)
            if n:
                log(f"Copied {n} files from {prefix}")

    ctx = Ctx(pack.mc, pack.loader, pack.loader_ver, accept_eula, aggressive, java_exec, script_java,
              ram_mb, log, online)

    check_cancel()
    if not keep_client_mods:
        log("\nChecking which mods are client-only...")
        n = prune_client_mods(server_dir, ctx, excludes)
        log(f"  {n} client-only mod(s) moved to {BUCKETS['client_only']}/")

    check_cancel()
    log("")
    install_loader(pack, server_dir, java_exec, log)
    write_start_scripts(server_dir, ram_mb, script_java, log)
    write_file(server_dir / "eula.txt", "# https://aka.ms/MinecraftEULA\n"
               f"eula={'true' if accept_eula else 'false'}\n")
    save_state(server_dir, ctx, pack.name)

    ok = None
    if test_run:
        if accept_eula:
            ok = test_and_fix(server_dir, ctx, max_attempts)
        else:
            log("\nSkipping the test launch because the Minecraft EULA wasn't accepted "
                "(https://aka.ms/MinecraftEULA). Accept it to let the app test and auto-fix the server.")
    elif not accept_eula:
        log("NOTE: eula.txt is false. Read https://aka.ms/MinecraftEULA and change it to true to run the server.")

    if failures:
        ctx.notes.append(f"{len(failures)} mod(s) failed to download:")
        for ident, err in failures:
            if pack.kind == "curseforge":
                pid, fid = ident.split("/")
                ctx.notes.append(f"  project {pid} file {fid}: https://www.curseforge.com/api/v1/mods/{pid}/files/{fid}/download")
            else:
                ctx.notes.append(f"  {ident}")
    write_report(server_dir, ctx, f"Build of {pack.name}")
    remember_server(server_dir, pack.name)

    log("\n" + "=" * 60)
    log(f"Server folder: {server_dir}")
    log("Start it with start.bat (Windows) or ./start.sh (Linux/macOS).")
    if ok is True:
        log("It was test-launched successfully.")
    elif ok is False:
        log("The test launch did not succeed - see the messages above. Use 'Fix from log' with "
            "logs/latest.log after another try.")
    if failures:
        log(f"{len(failures)} mod(s) failed to download (listed in {REPORT_FILE}). Put them in mods/ by hand "
            "or retry with a CurseForge API key.")
    log(f"Removed mods are kept in mods_removed_*/ folders; a full report is in {REPORT_FILE}.")
    return BuildResult(server_dir, ok, len(failures))


# ============================================================================
# Running a server (the "control room"), network info, server.properties
# ============================================================================

import atexit
import ipaddress
import socket
from collections import deque

DEFAULT_PORT = 25565
CONSOLE_KEEP = 3000          # console lines kept for the page
CAPTURE_KEEP = 20000         # lines of the current run kept for crash diagnosis
LINE_MAX = 4000              # longest console line passed on (characters)


class EulaRequired(RuntimeError):
    """The server can't start until the person accepts Minecraft's EULA."""


EULA_MESSAGE = ("Before the server can start, you need to accept Minecraft's rules for running a server "
                "(the EULA: https://aka.ms/MinecraftEULA).")

RX_EULA_TRUE = re.compile(r"^\s*eula\s*=\s*true\s*$", re.I | re.M)
RX_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
RX_CON_LEVEL = re.compile(r"[/\s\[](INFO|WARN|WARNING|ERROR|FATAL|SEVERE|DEBUG|TRACE)\]")
RX_CON_STACK = re.compile(r"\s+at |\s*Caused by:|\s+\.\.\. \d+ more|\s*Suppressed:")
RX_CON_EXC = re.compile(r"[a-z][\w$]*(?:\.[\w$]+)+(?:Exception|Error)\b")
RX_CON_JOIN = re.compile(r"^([\w.\-*]{1,40}) (?:\(formerly known as [\w.\-*]+\) )?joined the game\s*$")
RX_CON_LEAVE = re.compile(r"^([\w.\-*]{1,40}) left the game\s*$")
RX_CON_CHAT = re.compile(r"^(?:\[Not Secure\]\s*)?(?:<[^>\s]{1,40}>\s|\[(?:Server|Rcon)\]\s|\* [\w.\-*]{1,40}\s)")
RX_CON_LIST = re.compile(r"There are (\d+) of a max(?: of)? (\d+) players online:?\s*(.*)$")
RX_CON_LIST_OLD = re.compile(r"There are (\d+)/(\d+) players online:?\s*(.*)$")
RX_CON_PORT = re.compile(r"Starting Minecraft server on \S*?:(\d+)")


def _pretty_mod(name):
    return f'"{name}"' if name else "a mod"


def _win_kill_with_app(proc):
    """Windows: put the server in a 'job' that Windows closes together with this app, so a crash of the app
    (or ending it in Task Manager) can never leave a hidden server running."""
    if not IS_WIN:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        global _WIN_JOB
        if _WIN_JOB is None:
            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IOC(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

            class EXT(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IOC),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return
            info = EXT()
            info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(job)
                return
            _WIN_JOB = job                      # deliberately never closed: it closes when the app exits
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject(_WIN_JOB, int(proc._handle))
    except Exception:  # noqa: BLE001
        pass


_WIN_JOB = None


class ServerRunner:
    """Runs one Minecraft server at a time. Every public method is safe to call from any thread."""

    STATES = ("offline", "starting", "online", "stopping", "crashed")

    def __init__(self):
        self._lock = threading.RLock()
        self._state = "offline"
        self._proc = None
        self._dir = None
        self._console = deque(maxlen=CONSOLE_KEEP)
        self._seq = 0
        self._capture = deque(maxlen=CAPTURE_KEEP)
        self._players = {}
        self._expect_names = 0
        self._max_players = None
        self._port = None
        self._started_at = None
        self._online_at = None
        self._exit_code = None
        self._error = None
        self._stop_requested = False
        self._busy = False                 # fix_and_restart is working
        self._run_id = 0
        self._last_kind = "info"
        self._java_for = {}                # server dir -> Java chosen by a fix (used on the next start)
        self._diag_cache = None
        self._launch_failed = False
        atexit.register(self._atexit)

    # ---- small internals -------------------------------------------------------------------
    @property
    def state(self):
        return self._state

    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _add(self, text, kind):
        with self._lock:
            self._seq += 1
            self._console.append({"seq": self._seq, "t": text[:LINE_MAX], "kind": kind})

    def _app_log(self, text, kind="info"):
        text = str(text).strip()
        if text:
            self._add(text, kind)

    def _read_props_safe(self, d):
        try:
            return read_properties(d)
        except Exception:  # noqa: BLE001
            return {}

    # ---- start -----------------------------------------------------------------------------
    def start(self, server_dir, accept_eula=False):
        d = Path(server_dir)
        with self._lock:
            if self._busy:
                raise RuntimeError("The server is being repaired right now. Please wait a moment.")
            if self._state in ("starting", "online", "stopping") or self._alive():
                raise RuntimeError("A server is already running. Stop it before starting another one.")
            if not d.is_dir():
                raise RuntimeError("That folder doesn't exist any more. Pick the folder your server is in.")
            if not launch_spec(d):
                raise RuntimeError("There's no Minecraft server in this folder yet. Build the server first, "
                                   "or pick the folder that has your server in it.")
            try:
                eula = (d / "eula.txt").read_text(encoding="utf-8", errors="ignore")
            except OSError:
                eula = ""
            if not RX_EULA_TRUE.search(eula):
                if not accept_eula:
                    raise EulaRequired(EULA_MESSAGE)
                try:
                    write_file(d / "eula.txt", "# https://aka.ms/MinecraftEULA\neula=true\n")
                except OSError as e:
                    raise RuntimeError(f"I couldn't save your EULA answer in the server folder ({e}). "
                                       f"Check the folder isn't read-only.") from e
            props = self._read_props_safe(d)
            self._dir = d
            self._state = "starting"
            self._stop_requested = False
            self._players = {}
            self._expect_names = 0
            self._max_players = _int_or(props.get("max-players"), 20)
            self._port = _int_or(props.get("server-port"), DEFAULT_PORT)
            self._started_at = time.time()
            self._online_at = None
            self._exit_code = None
            self._error = None
            self._capture.clear()
            self._diag_cache = None
            self._launch_failed = False
            self._last_kind = "info"
            self._run_id += 1
            run_id = self._run_id
        self._app_log("Starting the server...")
        threading.Thread(target=self._launch, args=(d, run_id), daemon=True, name="server-launch").start()

    def _launch(self, d, run_id):
        """Background: pick Java + memory, start the process, then read its output until it ends."""
        try:
            ctx = ctx_for_server(d, log=lambda s: None)
            ctx.log = self._app_log
            fixed = self._java_for.get(str(d))
            if fixed and Path(fixed).exists():
                ctx.java = fixed
            elif ctx.java == "java":
                want = recommended_java(ctx.mc)
                self._app_log(f"Looking for Java {want} (the free program Minecraft servers run on)...")
                j = get_java(want, self._app_log, True)
                if j:
                    ctx.java = j[0]
            cmd = launch_cmd(d, ctx)
        except Cancelled:
            return self._launch_fail(run_id, "Starting the server was cancelled.")
        except Exception as e:  # noqa: BLE001
            return self._launch_fail(run_id, f"The server couldn't be started: {e}")
        with self._lock:
            if run_id != self._run_id:
                return
            if self._stop_requested:
                self._state = "offline"
                self._app_log("The server was stopped before it started.")
                return
            try:
                proc = subprocess.Popen(cmd, cwd=str(d), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                        bufsize=1, **NOWIN)
            except FileNotFoundError:
                want = recommended_java(ctx.mc)
                return self._launch_fail(run_id, f"Java couldn't be found on this computer. Java is the free program "
                                         f"Minecraft servers run on. Install Java {want} from https://adoptium.net "
                                         f"and press Start again.")
            except OSError as e:
                return self._launch_fail(run_id, f"The server couldn't be started: {e}")
            _win_kill_with_app(proc)
            self._proc = proc
            self._started_at = time.time()
        self._read(proc, d, run_id)

    def _launch_fail(self, run_id, msg):
        with self._lock:
            if run_id != self._run_id:
                return
            stopped = self._stop_requested
            self._state = "offline" if stopped else "crashed"
            self._error = None if stopped else msg
            self._launch_failed = not stopped
        self._app_log(msg, "error")

    def _read(self, proc, d, run_id):
        try:
            for raw in proc.stdout:
                line = RX_ANSI.sub("", raw.rstrip("\r\n"))
                with self._lock:
                    self._capture.append(line)
                    self._add(line, self._classify(line))
        except (OSError, ValueError):
            pass
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
        try:
            proc.stdout.close()
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            if run_id != self._run_id:
                return
            self._exit_code = code
            self._players = {}
            self._expect_names = 0
            if self._stop_requested:
                self._state = "offline"
                msg = "The server has stopped."
            else:
                was = self._state
                self._state = "crashed"
                self._error = ("The server stopped unexpectedly while it was starting up." if was == "starting"
                               else "The server stopped unexpectedly.")
                msg = self._error
        self._app_log(msg, "info" if self._state == "offline" else "error")

    # ---- console line classification (Forge / NeoForge / Fabric / vanilla / Paper) ---------
    def _classify(self, line):
        """Classify one console line (lock held). Also tracks players / port / 'Done'."""
        cut = line.find("]: ")
        prefix, msg = (line[:cut + 1], line[cut + 3:]) if cut >= 0 else (line[:60], line)
        m = RX_CON_LEVEL.search(prefix)
        level = m.group(1) if m else None
        if level is None and cut < 0:
            if line.startswith("Exception in thread") or RX_CON_EXC.match(line):
                self._last_kind = "error"
                return "error"
            if RX_CON_STACK.match(line) or line[:1] in (" ", "\t"):
                # stack-trace / continuation line: belongs to the message above it
                return self._last_kind if self._last_kind in ("warn", "error") else "info"
        if self._expect_names and level in (None, "INFO"):
            names = [n.strip() for n in msg.split(",") if n.strip()]
            self._expect_names = 0
            if names:
                self._players = {n: True for n in names}
                return "info"
        if RX_DONE.search(line):
            if self._state == "starting":
                self._state = "online"
                self._online_at = time.time()
            self._last_kind = "info"
            return "done"
        if level in ("ERROR", "FATAL", "SEVERE"):
            self._last_kind = "error"
            return "error"
        if level in ("WARN", "WARNING"):
            self._last_kind = "warn"
            return "warn"
        self._last_kind = "info"
        m = RX_CON_JOIN.match(msg)
        if m:
            self._players[m.group(1)] = True
            return "join"
        m = RX_CON_LEAVE.match(msg)
        if m:
            self._players.pop(m.group(1), None)
            return "leave"
        m = RX_CON_LIST.search(msg) or RX_CON_LIST_OLD.search(msg)
        if m:
            n, mx, rest = int(m.group(1)), int(m.group(2)), m.group(3).strip()
            self._max_players = mx
            names = [x.strip() for x in rest.split(",") if x.strip()]
            if names or n == 0:
                self._players = {x: True for x in names}
            else:
                self._expect_names = n             # old servers print the names on the next line
            return "info"
        m = RX_CON_PORT.search(msg)
        if m:
            self._port = int(m.group(1))
        if RX_CON_CHAT.match(msg):
            return "chat"
        return "info"

    # ---- stop / send / shutdown ------------------------------------------------------------
    def stop(self, timeout=60):
        """Ask the server to save and close. Returns at once; `state` is 'stopping' until it has closed.
        If it hasn't closed after `timeout` seconds it is forced to close."""
        with self._lock:
            if self._state not in ("starting", "online", "stopping"):
                return
            self._stop_requested = True
            already = self._state == "stopping"
            self._state = "stopping"
            proc = self._proc if self._alive() else None
            if not proc or already:
                return                        # not launched yet (e.g. still downloading Java): it won't launch
            self._add("> stop", "command")
            try:
                proc.stdin.write("stop\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                proc.kill()                   # the console is gone; the reader thread finishes up
                return
        threading.Thread(target=self._watchdog, args=(proc, timeout), daemon=True, name="server-stop").start()

    def _watchdog(self, proc, timeout):
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._app_log("The server didn't close in time, so it was forced to close.", "warn")
            try:
                proc.kill()
                proc.wait(timeout=30)
            except Exception:  # noqa: BLE001
                pass

    def send(self, command):
        """Type a command into the server console (a leading '/' is fine)."""
        cmd = str(command or "").strip()
        if "\n" in cmd or "\r" in cmd:
            raise ValueError("Please type one command at a time.")
        cmd = cmd.lstrip("/").strip()
        if not cmd:
            raise ValueError("Type a command first.")
        with self._lock:
            if self._state not in ("starting", "online") or not self._alive():
                raise RuntimeError("The server isn't running, so it can't take commands right now.")
            if cmd.split()[0].lower() == "stop":
                self._stop_requested = True
                self._state = "stopping"
                proc = self._proc
            else:
                proc = None
            self._add("> " + cmd, "command")
            try:
                self._proc.stdin.write(cmd + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError) as e:
                raise RuntimeError("The server isn't accepting commands (it may be closing).") from e
        if proc:
            threading.Thread(target=self._watchdog, args=(proc, 60), daemon=True, name="server-stop").start()

    def wait(self, timeout=None):
        """Block until the server process has ended (True) or `timeout` passes (False)."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            with self._lock:
                if self._state not in ("starting", "online", "stopping"):
                    return True
            if deadline is not None and time.time() >= deadline:
                return False
            time.sleep(0.05)

    def shutdown(self, timeout=60):
        """Stop the server and wait for it to close (forcing it after `timeout`). Used when the app quits."""
        self.stop(timeout)
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=timeout + 5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
        self.wait(10)

    def _atexit(self):
        try:
            if self._alive():
                self.shutdown(timeout=20)
        except Exception:  # noqa: BLE001
            pass

    # ---- status / console ----------------------------------------------------------------
    def status(self):
        with self._lock:
            port = self._port
            if port is None and self._dir is not None:
                port = _int_or(self._read_props_safe(self._dir).get("server-port"), DEFAULT_PORT)
            return {"state": self._state, "dir": str(self._dir) if self._dir else None,
                    "players": list(self._players), "max_players": self._max_players,
                    "started_at": self._started_at, "online_at": self._online_at, "exit_code": self._exit_code,
                    "port": port, "error": self._error}

    def lines(self, since=0):
        """Console lines with seq >= since -> (items, next_seq). Pass next_seq back next time."""
        with self._lock:
            try:
                since = int(since or 0)
            except (TypeError, ValueError):
                since = 0
            items = [dict(x) for x in self._console if x["seq"] >= since]
            return items, self._seq + 1

    # ---- diagnosis / repair --------------------------------------------------------------
    def _failure_text(self):
        with self._lock:
            d, out, since = self._dir, "\n".join(self._capture), self._started_at or 0
        return d, collect_failure_text(d, out, since)

    def diagnose(self):
        """Explain the last crash in plain English. Reads the mods folder, so it can take a few seconds."""
        with self._lock:
            if self._dir is None:
                return {"summary": "The server hasn't been started yet, so there's nothing to check.",
                        "findings": [], "fixable": False}
            if self._state in ("starting", "online", "stopping"):
                return {"summary": "The server is running right now, so there's no crash to look at.",
                        "findings": [], "fixable": False}
            if self._launch_failed:
                return {"summary": self._error, "findings": [], "fixable": False}
            if self._diag_cache is not None:
                return self._diag_cache
            run_id = self._run_id
        d, text = self._failure_text()
        ctx = ctx_for_server(d, text, log=lambda s: None)
        findings = analyze_text(text, d, ctx)
        out_f, fixable, top = [], False, None
        for f in findings:
            acts = _finding_would_act(f, ctx)
            fixable = fixable or acts
            if top is None and acts:
                top = f
            out_f.append({"kind": f.kind, "message": f.message.strip(), "confidence": f.confidence,
                          "action": f.action, "mod": f.info.name if f.info else None, "auto": acts})
        if top is None and findings:
            top = findings[0]
        with self._lock:
            crashed = self._state == "crashed"
        if top is not None:
            summary = _plain_summary(top, self._port or DEFAULT_PORT)
        elif crashed:
            summary = ("The server stopped unexpectedly and I couldn't work out why. The last messages in the "
                       "console may give a clue.")
        else:
            summary = "The server closed normally - nothing went wrong."
        res = {"summary": summary, "findings": out_f, "fixable": fixable}
        with self._lock:
            if run_id == self._run_id:
                self._diag_cache = res
        return res

    def fix_and_restart(self, accept_eula=False, aggressive=False):
        """Repair what the last crash points at, then start again if anything changed.
        Blocks while fixing (it may download a missing mod or Java)."""
        with self._lock:
            if self._state in ("starting", "online", "stopping") or self._alive():
                raise RuntimeError("Stop the server before repairing it.")
            if self._busy:
                raise RuntimeError("A repair is already in progress.")
            if self._dir is None:
                raise RuntimeError("Start the server once first, so I can see what goes wrong.")
            self._busy = True
        notes, manual, changed, d = [], [], False, self._dir
        try:
            d, text = self._failure_text()
            ctx = ctx_for_server(d, text, accept_eula, aggressive, log=lambda s: None)
            ctx.log = self._app_log
            fixed = self._java_for.get(str(d))
            if fixed:
                ctx.java = fixed
            java_before = ctx.java
            findings = analyze_text(text, d, ctx)
            if self._launch_failed and not findings:
                manual.append(self._error or "The server couldn't be started.")
            else:
                changed, manual = apply_findings(findings, d, ctx)
            notes = list(ctx.notes)
            if ctx.java != java_before:
                self._java_for[str(d)] = ctx.java
        except Cancelled:
            manual.append("The repair was cancelled.")
        except Exception as e:  # noqa: BLE001
            manual.append(f"Something went wrong while repairing the server: {e}")
        finally:
            with self._lock:
                self._busy = False
        res = {"changed": bool(changed), "manual": manual, "notes": notes, "restarted": False}
        if changed:
            try:
                self.start(d, accept_eula=accept_eula)
                res["restarted"] = True
            except EulaRequired as e:
                res["eula_required"] = True
                res["manual"].append(str(e))
            except RuntimeError as e:
                res["manual"].append(str(e))
        return res


def _int_or(v, default):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _finding_would_act(f, ctx):
    """Would apply_findings change something for this finding at default settings?"""
    if f.action == "remove":
        return CONF_RANK[f.confidence] >= 1 and bool(f.info) and f.info.jar.exists()
    if f.action == "install":
        return bool(ctx.online)
    if f.action == "java":
        return f.data.get("java", 0) > 0
    if f.action == "ram_up":
        cap = int((system_ram_mb() or 16384) * 0.75)
        return min(ctx.ram_mb + 2048, cap // 512 * 512) > ctx.ram_mb
    if f.action == "ram_down":
        return max(2048, int((system_ram_mb() or 8192) * 0.6)) < ctx.ram_mb
    return False


def _plain_summary(f, port):
    mod = _pretty_mod(f.info.name if f.info else "")
    k = f.kind
    if k == "client_mod":
        return (f"The mod {mod} only works inside the game on a player's computer, not on a server, "
                f"so the server crashed while starting.")
    if k == "duplicate":
        return f"Two copies of the same mod are installed ({mod} is the extra one), and the server refuses to start."
    if k in ("mod_failed", "mod_crash"):
        return f"The mod {mod} broke while the server was starting, which made the server crash."
    if k == "wrong_version":
        return f"The mod {mod} was made for a different version of Minecraft than this server."
    if k == "wrong_loader":
        return f"The mod {mod} is made for a different kind of modded server, so it can't be used here."
    if k == "missing_dep":
        dep = f.data.get("modid", "another mod")
        req = f.data.get("requester", "One of your mods")
        return f"The mod \"{req}\" needs another mod called \"{dep}\" that isn't installed."
    if k == "version_mismatch":
        return "Two of your mods don't work together, because one needs a different version of the other."
    if k == "loader_too_old":
        return ("A mod needs a newer version of the mod loader (the add-on that lets Minecraft run mods) "
                "than this server has.")
    if k == "java_version":
        need = f.data.get("java")
        return (f"This server needs a newer version of Java{f' ({need} or later)' if need else ''}, the free "
                f"program Minecraft servers run on.")
    if k == "oom":
        return "The server ran out of memory (RAM) and needs to be given more."
    if k == "oom_low":
        return "The server asked for more memory (RAM) than this computer can give it."
    if k == "eula":
        return "Minecraft's server rules (the EULA) haven't been accepted yet."
    if k == "port":
        return (f"Something else on this computer - probably another copy of this server - is already using "
                f"network port {port} (the 'door number' players connect through).")
    if k == "client_unknown":
        return ("One of the mods only works inside the game on a player's computer and is crashing the server, "
                "but I couldn't tell which one.")
    return "The server stopped unexpectedly and I couldn't work out exactly why."


# ============================================================================
# Network helpers: addresses people use to join, and an outside reachability check
# ============================================================================

_PUBLIC_IP_LOCK = threading.Lock()
_PUBLIC_IP = {"ip": None, "ts": 0.0}
PUBLIC_IP_TTL = 600          # remember a found address for 10 minutes
PUBLIC_IP_FAIL_TTL = 60      # after a failure, wait a minute before asking again


def _usable_ipv4(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and not (a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast)


def lan_ip():
    """This computer's address on the home network (e.g. 192.168.1.20), or None. Sends nothing."""
    for target in ("10.254.254.254", "192.168.254.254", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 1))                 # UDP connect only picks a route; no packet is sent
            ip = s.getsockname()[0]
            if _usable_ipv4(ip):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if _usable_ipv4(ip):
                return ip
    except OSError:
        pass
    return None


def public_ip(timeout=5):
    """This home's internet address as the outside world sees it, or None (cached ~10 minutes)."""
    now = time.time()
    with _PUBLIC_IP_LOCK:
        ip, ts = _PUBLIC_IP["ip"], _PUBLIC_IP["ts"]
        if ts and now - ts < (PUBLIC_IP_TTL if ip else PUBLIC_IP_FAIL_TTL):
            return ip
    found = None
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with http_open(url, {"Accept": "text/plain"}, timeout=timeout) as r:
                txt = r.read(100).decode("ascii", "ignore").strip()
            a = ipaddress.ip_address(txt)
            if a.is_global:
                found = str(a)
                break
        except Exception:  # noqa: BLE001
            continue
    with _PUBLIC_IP_LOCK:
        _PUBLIC_IP["ip"], _PUBLIC_IP["ts"] = found, time.time()
    return found


def _hostport(host, port):
    host = str(host)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host if int(port) == DEFAULT_PORT else f"{host}:{port}"


def check_reachable(host, port, timeout=10):
    """Ask an outside service (mcsrvstat.us) whether the server can be reached from the internet.
    -> {"reachable": True/False/None, "detail": plain English}. None means it couldn't be checked."""
    port = _int_or(port, -1)
    host = str(host or "").strip()
    if not host or not re.fullmatch(r"[\w.\-:\[\]]{1,255}", host) or not 0 < port < 65536:
        return {"reachable": None, "detail": "That address doesn't look right, so I couldn't check it."}
    target = f"{host}:{port}"
    url = "https://api.mcsrvstat.us/3/" + urllib.parse.quote(target, safe=".-:[]")
    try:
        with http_open(url, {"Accept": "application/json"}, timeout=timeout) as r:
            d = json.loads(r.read(1 << 20).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {"reachable": None, "detail": "I couldn't check right now (the checking website didn't answer). "
                                             "That doesn't mean anything is wrong - try again in a minute."}
    if not isinstance(d, dict) or "online" not in d:
        return {"reachable": None, "detail": "The checking website gave an answer I didn't understand. "
                                             "Try again in a minute."}
    old = " (This result can be a few minutes old.)" if (d.get("debug") or {}).get("cachehit") else ""
    if d.get("online"):
        return {"reachable": True,
                "detail": f"Yes! People on the internet can reach your server at {_hostport(host, port)}."}
    return {"reachable": False,
            "detail": f"People outside your home can't reach the server yet. Make sure the server is running. "
                      f"If it is, your router probably needs a 'port forward' - a setting that lets players "
                      f"through to this computer - for port {port} (TCP), or Windows Firewall is blocking "
                      f"Java.{old}"}


def join_info(server_dir, fetch_public=True):
    """Addresses people type into Minecraft to join. Looking up the public address may take a few seconds
    the first time (it is cached afterwards); pass fetch_public=False to skip it."""
    port = _int_or(read_properties(server_dir).get("server-port"), DEFAULT_PORT)
    if not 0 < port < 65536:
        port = DEFAULT_PORT
    lan = lan_ip()
    pub = public_ip() if fetch_public else None
    return {"lan": _hostport(lan, port) if lan else None, "public": _hostport(pub, port) if pub else None,
            "port": port, "local": _hostport("localhost", port)}


# ============================================================================
# server.properties
# ============================================================================


def _props_path(server_dir) -> Path:
    return Path(server_dir) / "server.properties"


def _props_text(p: Path) -> str:
    raw = p.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _odd_backslashes(s):
    n = len(s) - len(s.rstrip("\\"))
    return n % 2 == 1


def _prop_entries(lines):
    """-> list of (first_line, last_line, key, value) for each key/value entry (continuations joined)."""
    out, i = [], 0
    while i < len(lines):
        start = i
        s = lines[i].lstrip(" \t\f")
        if not s or s[0] in "#!":
            i += 1
            continue
        buf = s
        while _odd_backslashes(buf) and i + 1 < len(lines):
            i += 1
            buf = buf[:-1] + lines[i].lstrip(" \t\f")
        if _odd_backslashes(buf):
            buf = buf[:-1]
        k, v = _prop_split(buf)
        out.append((start, i, k, v))
        i += 1
    return out


def _prop_split(s):
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c in "=: \t\f":
            break
        i += 1
    key = s[:i]
    while i < n and s[i] in " \t\f":
        i += 1
    if i < n and s[i] in "=:":
        i += 1
    while i < n and s[i] in " \t\f":
        i += 1
    return _prop_unescape(key), _prop_unescape(s[i:])


def _prop_unescape(s):
    if "\\" not in s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        e = s[i + 1]
        if e == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6]):
            out.append(chr(int(s[i + 2:i + 6], 16)))
            i += 6
            continue
        out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(e, e))
        i += 2
    txt = "".join(out)
    try:
        return txt.encode("utf-16", "surrogatepass").decode("utf-16")     # join 😀 pairs
    except UnicodeError:
        return txt


def _prop_escape(s, is_key=False):
    out = []
    for i, c in enumerate(str(s)):
        o = ord(c)
        if c == "\\":
            out.append("\\\\")
        elif c in "\t\n\r\f":
            out.append({"\t": "\\t", "\n": "\\n", "\r": "\\r", "\f": "\\f"}[c])
        elif c in "=:#!":
            out.append("\\" + c)
        elif c == " " and (is_key or i == 0):
            out.append("\\ ")
        elif o < 0x20 or o > 0x7E:
            if o > 0xFFFF:                        # write as a UTF-16 surrogate pair, like Java does
                v = o - 0x10000
                out.append(f"\\u{0xD800 + (v >> 10):04X}\\u{0xDC00 + (v & 0x3FF):04X}")
            else:
                out.append(f"\\u{o:04X}")
        else:
            out.append(c)
    return "".join(out)


def read_properties(server_dir) -> dict:
    """server.properties as {key: value} (strings). Missing file -> {}."""
    p = _props_path(server_dir)
    try:
        text = _props_text(p)
    except OSError:
        return {}
    return {k: v for _, _, k, v in _prop_entries(text.splitlines())}


def _prop_str(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return "" if v is None else str(v)


def write_properties(server_dir, updates: dict):
    """Change some keys in server.properties, keeping comments, order and every other line as it was.
    Creates the file if it doesn't exist."""
    p = _props_path(server_dir)
    try:
        text = _props_text(p)
    except FileNotFoundError:
        text = f"#Minecraft server properties\n#{datetime.now():%a %b %d %H:%M:%S %Y}\n"
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    upd = {str(k): _prop_str(v) for k, v in (updates or {}).items()}
    done, drop, repl = set(), set(), {}
    for start, end, k, _v in _prop_entries(lines):
        if k in upd:
            if k in done:
                drop.update(range(start, end + 1))          # later duplicate of a key we set: remove
            else:
                repl[start] = f"{_prop_escape(k, True)}={_prop_escape(upd[k])}"
                drop.update(range(start + 1, end + 1))
                done.add(k)
    out = []
    for i, line in enumerate(lines):
        if i in repl:
            out.append(repl[i])
        elif i not in drop:
            out.append(line)
    for k, v in upd.items():
        if k not in done:
            out.append(f"{_prop_escape(k, True)}={_prop_escape(v)}")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(nl.join(out) + nl)
    os.replace(tmp, p)


PROPERTY_SCHEMA = [
    {"key": "motd", "label": "Server name in the list", "type": "text", "default": "A Minecraft Server",
     "max_length": 150,
     "help": "The message players see under your server's name in their multiplayer list."},
    {"key": "max-players", "label": "Most players at once", "type": "int", "default": 20, "min": 1, "max": 500,
     "help": "How many people can be on the server at the same time. More players need more memory."},
    {"key": "difficulty", "label": "Difficulty", "type": "choice", "default": "easy",
     "choices": [["peaceful", "Peaceful (no monsters)"], ["easy", "Easy"], ["normal", "Normal"], ["hard", "Hard"]],
     "help": "How dangerous the world is. Peaceful has no monsters at all."},
    {"key": "gamemode", "label": "Game mode for new players", "type": "choice", "default": "survival",
     "choices": [["survival", "Survival (gather and build)"], ["creative", "Creative (unlimited blocks, can fly)"],
                 ["adventure", "Adventure (can't break blocks)"], ["spectator", "Spectator (only watch)"]],
     "help": "The mode people play in when they first join."},
    {"key": "pvp", "label": "Players can hurt each other", "type": "bool", "default": True,
     "help": "Turn this off if you don't want players to be able to attack each other."},
    {"key": "white-list", "label": "Only let people I approve join", "type": "bool", "default": False,
     "help": "When this is on, only players on your approved list can join. Add someone by typing "
             "'whitelist add TheirName' in the console."},
    {"key": "online-mode", "label": "Check that players own Minecraft", "type": "bool", "default": True,
     "help": "Keep this on. It checks with Minecraft that every player really owns the game and really is who "
             "they say they are. Turning it off is unsafe: anyone could join using anybody's name, "
             "including yours."},
    {"key": "allow-flight", "label": "Don't kick players for flying", "type": "bool", "default": False,
     "help": "Some modpacks have jetpacks, wings or magic that let players fly. With this off, the server may "
             "think they're cheating and kick them for 'flying'. Turn it on if your modpack has any way to fly."},
    {"key": "view-distance", "label": "How far players can see (in chunks)", "type": "int", "default": 10,
     "min": 3, "max": 32,
     "help": "How much of the world the server sends to each player. One chunk is 16 blocks. Lower numbers "
             "make the server run faster; 8 to 10 is a good choice for modpacks."},
    {"key": "spawn-protection", "label": "Protected area around spawn (blocks)", "type": "int", "default": 16,
     "min": 0, "max": 1000,
     "help": "Only server operators can build or break blocks this close to where players first appear. "
             "Set it to 0 to let everyone build there."},
    {"key": "server-port", "label": "Port (door number players connect to)", "type": "int",
     "default": DEFAULT_PORT, "min": 1024, "max": 65535,
     "help": "Leave this at 25565 unless you know you need something else. If you change it, players have to "
             "add ':' and the number to the address, and any router port forward has to use the new number."},
    {"key": "level-seed", "label": "World seed", "type": "text", "default": "", "max_length": 100,
     "help": "A word or number that decides what a new world looks like. Leave it empty for a random world. "
             "It only matters when a new world is created, not for a world that already exists."},
]
_SCHEMA_BY_KEY = {s["key"]: s for s in PROPERTY_SCHEMA}
_NUMERIC_CHOICES = {"difficulty": ["peaceful", "easy", "normal", "hard"],
                    "gamemode": ["survival", "creative", "adventure", "spectator"]}


def _parse_bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off", ""):
        return False
    return None


def _setting_value(spec, raw):
    """Current value from the file (string or None) -> typed value for the page."""
    if raw is None:
        return spec["default"]
    t = spec["type"]
    if t == "bool":
        b = _parse_bool(raw)
        return spec["default"] if b is None else b
    if t == "int":
        return _int_or(raw, spec["default"])
    if t == "choice":
        s = str(raw).strip().lower()
        nums = _NUMERIC_CHOICES.get(spec["key"])
        if nums and s.isdigit() and int(s) < len(nums):
            s = nums[int(s)]
        return s if s in [c[0] for c in spec["choices"]] else spec["default"]
    return raw


def get_settings(server_dir) -> list:
    """PROPERTY_SCHEMA entries, each with a "value" (the current setting, or the default)."""
    cur = read_properties(server_dir)
    out = []
    for spec in PROPERTY_SCHEMA:
        item = {k: (list(map(list, v)) if k == "choices" else v) for k, v in spec.items()}
        item["value"] = _setting_value(spec, cur.get(spec["key"]))
        out.append(item)
    return out


def apply_settings(server_dir, values: dict) -> dict:
    """Check and save settings from the page. Unknown keys are ignored. Raises ValueError (plain English)
    if a value isn't allowed; nothing is saved in that case. -> the {key: text} written.
    Changes take effect the next time the server starts."""
    if not isinstance(values, dict):
        raise ValueError("The settings weren't sent in a form I understand.")
    cur = read_properties(server_dir)
    upd = {}
    for key, v in values.items():
        spec = _SCHEMA_BY_KEY.get(key)
        if not spec:
            continue
        label, t = spec["label"], spec["type"]
        if t == "bool":
            b = _parse_bool(v)
            if b is None:
                raise ValueError(f"'{label}' must be either on or off.")
            upd[key] = "true" if b else "false"
        elif t == "int":
            if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
                n = None
            elif isinstance(v, float):
                n = int(v)
            else:
                n = _int_or(v, None)
            lo, hi = spec.get("min"), spec.get("max")
            if n is None or (lo is not None and n < lo) or (hi is not None and n > hi):
                raise ValueError(f"'{label}' must be a whole number from {lo} to {hi}.")
            upd[key] = str(n)
        elif t == "choice":
            s = str(v).strip().lower()
            allowed = [c[0] for c in spec["choices"]]
            if s not in allowed:
                raise ValueError(f"'{label}' must be one of: " + ", ".join(c[1] for c in spec["choices"]) + ".")
            nums = _NUMERIC_CHOICES.get(key)
            old = str(cur.get(key, "")).strip()
            # very old servers (Minecraft 1.13 and earlier) only understand numbers here
            upd[key] = str(nums.index(s)) if nums and old.isdigit() else s
        else:
            s = "" if v is None else str(v)
            if any(ord(c) < 0x20 for c in s):
                raise ValueError(f"'{label}' can't contain line breaks or special control characters.")
            if len(s) > spec.get("max_length", 200):
                raise ValueError(f"'{label}' is too long (at most {spec.get('max_length', 200)} characters).")
            upd[key] = s.strip() if key == "level-seed" else s
    if upd:
        write_properties(server_dir, upd)
    return upd


# ============================================================================
# Memory recommendation
# ============================================================================


def recommended_ram_mb(server_dir_or_mod_count, system_mb) -> int:
    """How much memory (MB) to give the server: 2-3 GB vanilla, ~4 GB up to 50 mods, 6 GB up to 150,
    8 GB above that; never more than 70% of the computer's memory, never under 2 GB; multiples of 512 MB."""
    x = server_dir_or_mod_count
    if isinstance(x, int) and not isinstance(x, bool):
        mods = max(0, x)
    else:
        try:
            md = Path(x) / "mods"
            mods = sum(1 for _ in md.glob("*.jar")) if md.is_dir() else 0
        except (TypeError, OSError):
            mods = 0
    system = system_mb if isinstance(system_mb, (int, float)) and system_mb > 0 else (system_ram_mb() or 8192)
    if mods == 0:
        want = 3072 if system >= 8192 else 2048
    elif mods <= 50:
        want = 4096
    elif mods <= 150:
        want = 6144
    else:
        want = 8192
    cap = int(system * 0.7) // 512 * 512
    return max(2048, min(want, cap)) // 512 * 512



# ============================================================================
# Self-update: signed releases from GitHub
# ============================================================================
#
# A release (tag vX.Y.Z) carries ModpackServerBuilder-Setup.exe, ModpackServerBuilder.exe, SHA256SUMS
# and SHA256SUMS.sig (hex Ed25519 signature over the exact bytes of SHA256SUMS). The first line of
# SHA256SUMS is "# ModpackServerBuilder X.Y.Z" so an old signed list can't be replayed under a newer
# tag (sha256sum -c ignores "#" lines). Nothing is trusted until the signature checks out against
# UPDATE_PUBKEY and the exe matches its signed hash.
#
# Flow: AutoUpdater checks + downloads into app_data_dir()/updates/<version>/ -> install_now()
# -> apply_update() spawns the new exe with --finish-update <target> and the app exits -> the new
# exe (handle_update_args at the top of main) waits for the old file to be released, swaps itself
# in, re-registers Installed apps and relaunches the target with --updated <version>.

UPDATE_REPO = "ricoism/modpack-server-builder"
GITHUB_API = "https://api.github.com"
UPDATE_PUBKEY = "bc5632337815cc7be10d669f2a77a72cf14ca90256608db0a429b4bbbbe1f9ef"   # Ed25519, release signing
UPDATE_SETUP_ASSET = "ModpackServerBuilder-Setup.exe"
UPDATE_SUMS_ASSET = "SHA256SUMS"
UPDATE_SIG_ASSET = "SHA256SUMS.sig"
UPDATE_CHECK_INTERVAL = 6 * 3600
UPDATE_MAX_EXE = 512 << 20
_UPDATE_RETRY_DELAY = 2.0        # seconds between download retries (tests shrink it)
_FINISH_RETRY_DELAY = 0.5        # seconds between attempts to replace the old exe
JUST_UPDATED = None              # set by handle_update_args when started with --updated <version>


class UpdateError(Exception):
    """Anything that stops an update; the message is meant for the user."""


# ---- Ed25519 (RFC 8032), pure Python ------------------------------------------------------
# Small and slow-ish (a verify is a few ms); points are extended coordinates (X, Y, Z, T).

_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_SQRT_M1 = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_recover_x(y, sign):
    p = _ED_P
    if y >= p:
        return None
    x2 = (y * y - 1) * pow(_ED_D * y * y + 1, p - 2, p) % p
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p:
        x = x * _ED_SQRT_M1 % p
    if (x * x - x2) % p:
        return None
    if (x & 1) != sign:
        x = p - x
    return x


_ED_GY = 4 * pow(5, _ED_P - 2, _ED_P) % _ED_P
_ED_GX = _ed_recover_x(_ED_GY, 0)
_ED_G = (_ED_GX, _ED_GY, 1, _ED_GX * _ED_GY % _ED_P)
_ED_ZERO = (0, 1, 1, 0)


def _ed_add(P, Q):
    p = _ED_P
    a = (P[1] - P[0]) * (Q[1] - Q[0]) % p
    b = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    c = 2 * P[3] * Q[3] * _ED_D % p
    d = 2 * P[2] * Q[2] % p
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % p, g * h % p, f * g % p, e * h % p)


def _ed_mul(s, P):
    Q = _ED_ZERO
    while s > 0:
        if s & 1:
            Q = _ed_add(Q, P)
        P = _ed_add(P, P)
        s >>= 1
    return Q


def _ed_equal(P, Q):
    p = _ED_P
    return (P[0] * Q[2] - Q[0] * P[2]) % p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % p == 0


def _ed_compress(P):
    zinv = pow(P[2], _ED_P - 2, _ED_P)
    x, y = P[0] * zinv % _ED_P, P[1] * zinv % _ED_P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _ed_decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _ed_recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _ED_P)


def _ed_hint(data):
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _ED_L


def _ed_expand(seed):
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def ed25519_public_key(seed: bytes) -> bytes:
    a, _ = _ed_expand(seed)
    return _ed_compress(_ed_mul(a, _ED_G))


def ed25519_sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _ed_expand(seed)
    pub = _ed_compress(_ed_mul(a, _ED_G))
    r = _ed_hint(prefix + msg)
    R = _ed_compress(_ed_mul(r, _ED_G))
    s = (r + _ed_hint(R + pub + msg) * a) % _ED_L
    return R + int.to_bytes(s, 32, "little")


def ed25519_verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    try:
        if len(pub) != 32 or len(sig) != 64:
            return False
        A = _ed_decompress(pub)
        R = _ed_decompress(sig[:32])
        s = int.from_bytes(sig[32:], "little")
        if A is None or R is None or s >= _ED_L:
            return False
        h = _ed_hint(sig[:32] + pub + msg)
        return _ed_equal(_ed_mul(s, _ED_G), _ed_add(R, _ed_mul(h, A)))
    except Exception:  # noqa: BLE001
        return False


# ---- helpers -------------------------------------------------------------------------------

def _ulog(msg):
    """Updater/installer diary: stderr, plus update.log in the app's data folder (the .exe has no console)."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        if sys.stderr:
            print(f"[update] {line}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        f = app_data_dir() / "update.log"
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.is_file() and f.stat().st_size > 256 * 1024:
            f.replace(f.with_suffix(".log.old"))
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def version_tuple(s) -> tuple:
    """'v3.1.0' -> (3, 1, 0). Always at least 3 parts so '3.1' == '3.1.0'. () when unparseable."""
    m = re.match(r"\s*[vV]?(\d+(?:\.\d+)*)", str(s or ""))
    if not m:
        return ()
    t = [int(x) for x in m.group(1).split(".")]
    while len(t) < 3:
        t.append(0)
    while len(t) > 3 and t[-1] == 0:
        t.pop()
    return tuple(t)


def _version_str(t) -> str:
    return ".".join(str(x) for x in t)


def updates_dir() -> Path:
    return app_data_dir() / "updates"


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _updates_enabled() -> bool:
    return is_frozen() or os.environ.get("MSB_UPDATE_TEST") == "1"


def _same_path(a, b) -> bool:
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)          # same file even when spelled differently (8.3 names, drives)
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return False


def verify_signed_sums(sums: bytes, sig_text, pubkey_hex=None) -> dict:
    """Check SHA256SUMS against its signature. Returns {"version": str|None, "files": {name: sha256}}."""
    try:
        sig = bytes.fromhex(sig_text.decode("ascii") if isinstance(sig_text, bytes) else str(sig_text))
        pub = bytes.fromhex(pubkey_hex or UPDATE_PUBKEY)
    except (ValueError, UnicodeDecodeError):
        raise UpdateError("The update's signature is malformed.") from None
    if not ed25519_verify(pub, sums, sig):
        raise UpdateError("The update's signature is not valid; it was not installed.")
    version, files = None, {}
    for line in sums.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = re.match(r"#\s*ModpackServerBuilder\s+v?(\d+(?:\.\d+)*)\s*$", line)
            if m:
                version = m.group(1)
            continue
        m = re.match(r"([0-9a-fA-F]{64}) [ *](.+)$", line)
        if m:
            files[m.group(2).strip()] = m.group(1).lower()
    return {"version": version, "files": files}


def _check_signed_version(signed, version):
    if not signed["version"] or version_tuple(signed["version"]) != version_tuple(version):
        raise UpdateError(f"The signed file list is for version {signed['version'] or '?'}, not {version}.")


def _fetch_bytes(url, limit, timeout=30) -> bytes:
    with http_open(url, {"Accept": "application/octet-stream"}, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateError(f"{url}: file is larger than expected")
    return data


def _fetch_file(url, dest: Path, sha256: str, size=0, progress=None, retries=3):
    tmp = dest.with_name(dest.name + ".part")
    cap = size or UPDATE_MAX_EXE
    last = None
    for attempt in range(retries):
        try:
            h, n = hashlib.sha256(), 0
            with http_open(url, {"Accept": "application/octet-stream"}, timeout=60) as r, open(tmp, "wb") as f:
                total = size or int(r.headers.get("Content-Length") or 0)
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > cap:
                        raise UpdateError("The update download is larger than the release says.")
                    f.write(chunk)
                    h.update(chunk)
                    if progress and total:
                        try:
                            progress(min(1.0, n / total))
                        except Exception:  # noqa: BLE001
                            pass
            if h.hexdigest() != sha256.lower():
                raise UpdateError("The downloaded update doesn't match its signed checksum; it was deleted.")
            os.replace(tmp, dest)
            return
        except UpdateError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            tmp.unlink(missing_ok=True)
            if attempt + 1 < retries:
                time.sleep(_UPDATE_RETRY_DELAY * (attempt + 1))
    raise UpdateError(f"Couldn't download the update: {last}")


# ---- public API ----------------------------------------------------------------------------

def check_for_update(timeout=10):
    """The latest GitHub release if it is newer than this build, else None.

    Returns {"version", "notes", "setup_url", "sums_url", "sig_url", "size"} (+ "tag", "page").
    No release yet (404), rate limits (403/429), drafts, prereleases and releases missing an asset
    give None. Network/parse failures raise UpdateError.
    """
    url = f"{GITHUB_API}/repos/{UPDATE_REPO}/releases/latest"
    try:
        with http_open(url, {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                       timeout=timeout) as r:
            rel = json.loads(r.read(4 << 20).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (403, 404, 429):
            _ulog(f"update check: HTTP {e.code}")
            return None
        raise UpdateError(f"Update check failed: HTTP {e.code}") from None
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"Update check failed: {e}") from None
    if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
        return None
    tag = str(rel.get("tag_name") or "")
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
        return None
    ver = version_tuple(tag)
    if ver <= version_tuple(APP_VERSION):
        return None
    assets = {a.get("name"): a for a in rel.get("assets") or [] if isinstance(a, dict)}
    need = (UPDATE_SETUP_ASSET, UPDATE_SUMS_ASSET, UPDATE_SIG_ASSET)
    if not all(n in assets and assets[n].get("browser_download_url") for n in need):
        _ulog(f"release {tag} is missing assets")
        return None
    return {"version": _version_str(ver), "notes": str(rel.get("body") or "")[:20000],
            "setup_url": assets[UPDATE_SETUP_ASSET]["browser_download_url"],
            "sums_url": assets[UPDATE_SUMS_ASSET]["browser_download_url"],
            "sig_url": assets[UPDATE_SIG_ASSET]["browser_download_url"],
            "size": int(assets[UPDATE_SETUP_ASSET].get("size") or 0),
            "tag": tag, "page": str(rel.get("html_url") or "")}


def download_update(info: dict, progress=None) -> Path:
    """Download and verify the update described by check_for_update(). Returns the verified exe path.

    Only verified files are kept: on any failure the version folder is removed and UpdateError raised.
    """
    ver = version_tuple(info.get("version"))
    if not ver:
        raise UpdateError("Bad update version.")
    version = _version_str(ver)
    d = updates_dir() / version
    exe = d / UPDATE_SETUP_ASSET
    try:
        d.mkdir(parents=True, exist_ok=True)
        for leftover in d.glob("*.part"):
            leftover.unlink(missing_ok=True)
        sums = _fetch_bytes(info["sums_url"], 64 << 10)
        sig = _fetch_bytes(info["sig_url"], 1 << 10)
        signed = verify_signed_sums(sums, sig.strip())
        _check_signed_version(signed, version)
        want = signed["files"].get(UPDATE_SETUP_ASSET)
        if not want:
            raise UpdateError(f"{UPDATE_SETUP_ASSET} is not in the signed file list.")
        if not (exe.is_file() and _sha256_file(exe) == want):
            exe.unlink(missing_ok=True)
            _fetch_file(info["setup_url"], exe, want, int(info.get("size") or 0), progress)
        if not IS_WIN:
            os.chmod(exe, 0o755)
        (d / UPDATE_SUMS_ASSET).write_bytes(sums)
        (d / UPDATE_SIG_ASSET).write_bytes(sig)
        write_file(d / "update.json", json.dumps({"version": version, "notes": info.get("notes") or ""}, indent=1))
        if progress:
            try:
                progress(1.0)
            except Exception:  # noqa: BLE001
                pass
        return exe
    except BaseException as e:
        shutil.rmtree(d, ignore_errors=True)
        if isinstance(e, UpdateError) or not isinstance(e, Exception):
            raise
        raise UpdateError(f"Couldn't download the update: {e}") from None


def _verify_update_dir(d: Path) -> dict:
    """Re-check a downloaded update folder. Returns {"version", "path", "notes"} or raises UpdateError."""
    version = _version_str(version_tuple(d.name))
    exe = d / UPDATE_SETUP_ASSET
    try:
        sums = (d / UPDATE_SUMS_ASSET).read_bytes()
        sig = (d / UPDATE_SIG_ASSET).read_bytes().strip()
    except OSError:
        raise UpdateError("The downloaded update is incomplete.") from None
    signed = verify_signed_sums(sums, sig)
    _check_signed_version(signed, version)
    want = signed["files"].get(UPDATE_SETUP_ASSET)
    if not want or not exe.is_file() or _sha256_file(exe) != want:
        raise UpdateError("The downloaded update is damaged; it will be downloaded again.")
    notes = ""
    try:
        notes = json.loads((d / "update.json").read_text(encoding="utf-8")).get("notes") or ""
    except Exception:  # noqa: BLE001
        pass
    return {"version": version, "path": exe, "notes": notes}


def pending_update():
    """A downloaded, still-valid update newer than this build: {"version", "path", "notes"} or None."""
    try:
        base = updates_dir()
        dirs = [p for p in base.iterdir() if p.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+", p.name)] \
            if base.is_dir() else []
    except OSError:
        return None
    cur = version_tuple(APP_VERSION)
    for d in sorted(dirs, key=lambda p: version_tuple(p.name), reverse=True):
        if version_tuple(d.name) <= cur:
            continue
        try:
            return _verify_update_dir(d)
        except UpdateError as e:
            _ulog(f"discarding {d}: {e}")
            shutil.rmtree(d, ignore_errors=True)
    return None


def _spawn_detached(args, cwd=None):
    """Start a program that outlives us. PYINSTALLER_RESET_ENVIRONMENT makes a onefile exe started
    from a onefile exe unpack its own files instead of reusing ours (which vanish when we exit)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    kw = {"creationflags": 0x00000008 | 0x00000200} if IS_WIN else {"start_new_session": True}
    return subprocess.Popen([str(a) for a in args], cwd=str(cwd) if cwd else None, env=env, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def update_target() -> Path:
    """The exe an update replaces: the installed copy if we run from the install folder, else ourselves."""
    me = Path(sys.executable)
    if _same_path(me.parent, install_dir()):
        return installed_exe()
    return me


def apply_update(path, target=None) -> Path:
    """Hand off to the downloaded exe; the caller must exit promptly afterwards. Returns the target."""
    if not _updates_enabled():
        raise UpdateError("Updates are only available in the packaged app.")
    path = Path(path)
    _verify_update_dir(path.parent)                  # last check right before running it
    target = Path(target) if target else update_target()
    _spawn_detached([path, "--finish-update", target], cwd=path.parent)
    _ulog(f"handed off to {path} for {target}")
    return target


def _cleanup_updates():
    """Remove downloaded updates that are not newer than this build (the running one may survive)."""
    base = updates_dir()
    if not base.is_dir():
        return
    cur = version_tuple(APP_VERSION)
    for d in base.iterdir():
        try:
            if d.is_dir() and version_tuple(d.name) <= cur:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _cleanup_leftovers(folder=None, min_age=300):
    """Delete our own stale "<exe>.new"/"<exe>.old" files next to the exe (not other programs' files)."""
    if folder is None:
        if not is_frozen():
            return
        folder = Path(sys.executable).parent
    names = {Path(sys.executable).name, f"{APP_NAME}.exe", "ModpackServerBuilder.exe", UPDATE_SETUP_ASSET}
    now = time.time()
    try:
        for f in Path(folder).iterdir():
            base, ext = os.path.splitext(f.name)
            if ext.lower() in (".new", ".old") and base in names and f.is_file():
                try:
                    if now - f.stat().st_mtime >= min_age:
                        f.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def finish_update_mode(target, source=None, wait=90.0) -> bool:
    """Run by the downloaded exe: replace `target` with ourselves, then relaunch it with --updated."""
    target = Path(target)
    src = Path(source or sys.executable)
    if not target.is_file() or _same_path(target, src):
        _ulog(f"finish-update: bad target {target}")
        return False
    new = target.with_name(target.name + ".new")
    try:
        shutil.copyfile(src, new)
        if not IS_WIN:
            os.chmod(new, 0o755)
        if _sha256_file(new) != _sha256_file(src):
            raise UpdateError("copy check failed")
    except Exception as e:  # noqa: BLE001
        new.unlink(missing_ok=True)
        _ulog(f"finish-update: couldn't copy the new version: {e}")
        message_box(f"The update couldn't be installed:\n{e}", 0x10)
        return False
    start = time.monotonic()
    asked_quit = False
    while True:
        try:
            os.replace(new, target)
            break
        except FileNotFoundError:
            try:                                     # someone cleaned our .new away; copy again
                shutil.copyfile(src, new)
            except OSError:
                pass
        except OSError as e:
            waited = time.monotonic() - start
            if waited >= wait:
                new.unlink(missing_ok=True)
                _ulog(f"finish-update: {target} stayed locked: {e}")
                message_box("The update couldn't be installed because Modpack Server Builder is still "
                            "running.\n\nClose it and it will update next time.", 0x30)
                try:
                    _spawn_detached([target], cwd=target.parent)
                except Exception:  # noqa: BLE001
                    pass
                return False
            if not asked_quit and waited >= 20:
                asked_quit = True
                try:
                    quit_existing_instance()         # an old window is somehow still open
                except Exception:  # noqa: BLE001
                    pass
        time.sleep(_FINISH_RETRY_DELAY)
    _ulog(f"finish-update: replaced {target} (installed copy: {_same_path(target, installed_exe())})")
    if _same_path(target, installed_exe()):
        try:
            if IS_WIN:
                refresh_shell_icons(target)          # new icon everywhere + Installed apps shows the new version
            info = target.parent / "install.json"
            if info.is_file():
                d = json.loads(info.read_text(encoding="utf-8"))
                d["version"] = APP_VERSION
                write_file(info, json.dumps(d, indent=1))
        except Exception as e:  # noqa: BLE001
            _ulog(f"finish-update: couldn't update the install record: {e}")
    _cleanup_updates()
    try:
        _spawn_detached([target, "--updated", APP_VERSION], cwd=target.parent)
    except Exception as e:  # noqa: BLE001
        _ulog(f"finish-update: couldn't relaunch {target}: {e}")
    return True


def handle_update_args(argv) -> bool:
    """Call first thing in main() with sys.argv[1:]. True means: we were the update helper, exit now.

    Also records `--updated <version>` in JUST_UPDATED (argparse must accept/ignore --updated)."""
    global JUST_UPDATED
    args = [str(a) for a in (argv or [])]
    try:
        _cleanup_leftovers()
        if "--updated" in args:
            i = args.index("--updated")
            JUST_UPDATED = args[i + 1] if i + 1 < len(args) else APP_VERSION
            _cleanup_updates()
    except Exception as e:  # noqa: BLE001
        _ulog(f"cleanup: {e}")
    if "--finish-update" not in args:
        return False
    i = args.index("--finish-update")
    try:
        if i + 1 < len(args):
            finish_update_mode(args[i + 1])
        else:
            _ulog("--finish-update needs a target")
    except Exception as e:  # noqa: BLE001
        _ulog(f"finish-update failed: {e}")
    return True


class AutoUpdater:
    """Checks GitHub shortly after start and every 6 hours, downloads updates silently.

    status() -> {"state", "version", "notes", "progress", "error", "current"}; install_now() -> bool.
    Never raises into the app.
    """

    def __init__(self, first_delay=15.0, interval=UPDATE_CHECK_INTERVAL, retry=3600.0):
        self.first_delay, self.interval, self.retry = first_delay, interval, retry
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._path = None
        self.enabled = _updates_enabled()
        self._st = {"state": "idle" if self.enabled else "disabled", "version": None, "notes": "",
                    "progress": 0.0, "error": None, "current": APP_VERSION}

    def start(self):
        try:
            if self.enabled and not (self._thread and self._thread.is_alive()):
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="auto-updater", daemon=True)
                self._thread.start()
        except Exception as e:  # noqa: BLE001
            self._set(state="error", error=str(e))
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()

    def check_now(self):
        """Ask for a check right away (e.g. from a "Check for updates" button)."""
        self._wake.set()

    def status(self) -> dict:
        with self._lock:
            return dict(self._st)

    def install_now(self) -> bool:
        """Start installing the ready update. True means the caller should exit the app now."""
        try:
            with self._lock:
                path = self._path if self._st["state"] == "ready" else None
            if not path:
                return False
            apply_update(path)
            return True
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._path = None
            self._set(state="error", error=str(e), progress=0.0)
            return False

    def _set(self, **kw):
        with self._lock:
            self._st.update(kw)

    def _run(self):
        try:
            p = pending_update()
            if p:
                with self._lock:
                    self._path = p["path"]
                self._set(state="ready", version=p["version"], notes=p["notes"], progress=1.0, error=None)
            delay = self.first_delay
            while not self._stop.is_set():
                self._wake.wait(delay)
                self._wake.clear()
                if self._stop.is_set():
                    break
                delay = self.interval if self._cycle() else self.retry
        except BaseException as e:  # noqa: BLE001
            self._set(state="error", error=str(e))

    def _cycle(self) -> bool:
        with self._lock:
            ready = self._st["version"] if self._st["state"] == "ready" else None
        try:
            if not ready:
                self._set(state="checking", error=None)
            info = check_for_update()
            if not info or (ready and version_tuple(info["version"]) <= version_tuple(ready)):
                if not ready:
                    self._set(state="up_to_date", progress=0.0)
                return True
            self._set(state="downloading", version=info["version"], notes=info["notes"], progress=0.0, error=None)
            path = download_update(info, progress=lambda f: self._set(progress=round(float(f), 4)))
            with self._lock:
                self._path = path
            self._set(state="ready", progress=1.0)
            return True
        except Exception as e:  # noqa: BLE001
            _ulog(f"auto-update: {e}")
            p = None
            try:
                p = pending_update()
            except Exception:  # noqa: BLE001
                pass
            if p:                                    # a newer download failed but an older one is fine
                with self._lock:
                    self._path = p["path"]
                self._set(state="ready", version=p["version"], notes=p["notes"], progress=1.0, error=str(e))
            else:
                self._set(state="error", error=str(e), progress=0.0)
            return False


# ============================================================================
# App layer: the servers you've made, finding modpacks, plain Minecraft, creating a server
# ============================================================================

import uuid as _uuid

FOLDER_DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"


def documents_dir() -> Path:
    if IS_WIN:
        try:
            d = known_folder(FOLDER_DOCUMENTS)
            if d:
                return Path(d)
        except Exception:  # noqa: BLE001
            pass
    d = Path.home() / "Documents"
    return d if d.is_dir() else Path.home()


def default_server_dir(name: str) -> Path:
    base = documents_dir() / "Minecraft Servers"
    stem = _safe_name(name, "My server")[:60] or "My server"
    p, n = base / stem, 2
    while p.exists() and any(p.iterdir()):
        p, n = base / f"{stem} ({n})", n + 1
    return p


# ---- registry ------------------------------------------------------------------------------

_SERVERS_LOCK = threading.Lock()


def _looks_like_server(d: Path) -> bool:
    return d.is_dir() and ((d / "mods").is_dir() or launch_spec(d) is not None or (d / "server.properties").exists())


def list_servers() -> list:
    st = load_settings()
    servers = st.get("servers")
    if servers is None:                     # first run of this version: bring over servers from "recent"
        servers = []
        for r in st.get("recent", []):
            d = Path(r.get("dir", ""))
            if _looks_like_server(d):
                servers.append(_server_record(d, r.get("name") or d.name))
        save_settings({"servers": servers})
    out = []
    for s in servers:
        s = dict(s)
        s["missing"] = not Path(s.get("dir", "")).is_dir()
        out.append(s)
    return out


def _server_record(d: Path, name: str, extra=None) -> dict:
    env = detect_server_env(d)
    rec = {"id": _uuid.uuid4().hex[:10], "name": name, "dir": str(d), "mc": env.get("mc", ""),
           "loader": env.get("loader", "vanilla" if (d / "server.jar").exists() else ""),
           "created": int(time.time()), "icon": "", "pack": env.get("pack", "")}
    rec.update(extra or {})
    return rec


def add_server(d: Path, name: str, extra=None) -> dict:
    with _SERVERS_LOCK:
        servers = [s for s in list_servers() if Path(s["dir"]) != Path(d)]
        rec = _server_record(Path(d), name, extra)
        servers.insert(0, {k: v for k, v in rec.items() if k != "missing"})
        save_settings({"servers": [{k: v for k, v in s.items() if k != "missing"} for s in servers]})
        return rec


def update_server(sid: str, **fields) -> dict:
    with _SERVERS_LOCK:
        servers = list_servers()
        for s in servers:
            if s["id"] == sid:
                s.update({k: v for k, v in fields.items() if k in ("name", "icon", "mc", "loader", "pack", "setup")})
                save_settings({"servers": [{k: v for k, v in x.items() if k != "missing"} for x in servers]})
                return s
    raise ValueError("That server isn't in your list any more.")


def remove_server(sid: str):
    with _SERVERS_LOCK:
        servers = [s for s in list_servers() if s["id"] != sid]
        save_settings({"servers": [{k: v for k, v in x.items() if k != "missing"} for x in servers]})


def get_server(sid: str) -> dict:
    for s in list_servers():
        if s["id"] == sid:
            if s["missing"]:
                raise ValueError(f"The folder for '{s['name']}' can't be found. It may have been moved or deleted.")
            return s
    raise ValueError("That server isn't in your list any more.")


def import_server(d: Path, name="") -> dict:
    if not _looks_like_server(d):
        raise ValueError("That folder doesn't look like a Minecraft server. Pick the folder that contains "
                         "the 'mods' folder or the server's .jar file.")
    return add_server(d, name or d.name)


# ---- finding modpacks (Modrinth) -------------------------------------------------------------

LOADER_NAMES = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric", "quilt": "Quilt"}


def modrinth_search_packs(query="", offset=0, limit=18) -> dict:
    facets = [["project_type:modpack"], ["server_side:required", "server_side:optional"]]
    params = {"query": query.strip(), "limit": max(1, min(int(limit), 40)), "offset": max(0, int(offset)),
              "index": "relevance" if query.strip() else "downloads", "facets": json.dumps(facets)}
    d = get_json(f"{MODRINTH}/search?{urllib.parse.urlencode(params)}")
    hits = []
    for h in d.get("hits", []):
        vers = [v for v in h.get("versions", []) if re.fullmatch(r"\d+(\.\d+)+", v)]
        hits.append({"id": h.get("project_id"), "slug": h.get("slug"), "title": h.get("title", ""),
                     "description": h.get("description", ""), "icon": h.get("icon_url") or "",
                     "downloads": h.get("downloads", 0), "author": h.get("author", ""),
                     "mc": sorted(vers, key=vkey)[-1] if vers else "",
                     "loaders": [LOADER_NAMES[c] for c in h.get("categories", []) if c in LOADER_NAMES]})
    return {"total": d.get("total_hits", len(hits)), "hits": hits}


def modrinth_pack_download(project_id: str, log=print):
    """Download the newest release of a Modrinth modpack. -> (path to .mrpack, title, icon)"""
    proj = get_json(f"{MODRINTH}/project/{urllib.parse.quote(project_id)}")
    versions = get_json(f"{MODRINTH}/project/{urllib.parse.quote(project_id)}/version")
    if not versions:
        raise RuntimeError("That modpack has no downloadable versions.")
    versions.sort(key=lambda v: (v.get("version_type") == "release", v.get("date_published", "")), reverse=True)
    v = versions[0]
    f = next((x for x in v.get("files", []) if x.get("primary")), (v.get("files") or [None])[0])
    if not f or not f.get("filename", "").endswith(".mrpack"):
        raise RuntimeError("That modpack's download isn't in a format the app understands.")
    dest = app_data_dir() / "downloads" / f["filename"]
    hashes = f.get("hashes") or {}
    if not dest.exists():
        log(f"Downloading the modpack {proj.get('title', '')} {v.get('version_number', '')}...")
        download(f["url"], dest, sha1=hashes.get("sha1"), sha512=hashes.get("sha512"))
    return dest, proj.get("title") or dest.stem, proj.get("icon_url") or ""


# ---- plain Minecraft -----------------------------------------------------------------------

_MANIFEST_CACHE = {}


def vanilla_versions(limit=12) -> list:
    if "m" not in _MANIFEST_CACHE or time.time() - _MANIFEST_CACHE["t"] > 3600:
        _MANIFEST_CACHE["m"] = get_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        _MANIFEST_CACHE["t"] = time.time()
    rel = [v["id"] for v in _MANIFEST_CACHE["m"]["versions"] if v.get("type") == "release"]
    return rel[:limit]


def build_vanilla_server(version, out_dir, ram="4G", accept_eula=False, test_run=True, online=True, log=print):
    mc = version or vanilla_versions(1)[0]
    log(f"Pack: Minecraft {mc}  [vanilla]")
    ram_mb = parse_ram_mb(ram)
    want = recommended_java(mc)
    found = get_java(want, log, online)
    if not found:
        raise RuntimeError(f"Java {want} isn't installed and couldn't be downloaded. Install it from "
                           f"https://adoptium.net and try again.")
    java_exec = found[0]
    pj = shutil.which("java")
    script_java = "java" if pj and java_major(pj) == found[1] else java_exec
    server_dir = Path(out_dir).resolve()
    server_dir.mkdir(parents=True, exist_ok=True)
    check_cancel()
    install_vanilla(mc, server_dir, log)
    write_start_scripts(server_dir, ram_mb, script_java, log)
    write_file(server_dir / "eula.txt", f"# https://aka.ms/MinecraftEULA\neula={'true' if accept_eula else 'false'}\n")
    ctx = Ctx(mc, "vanilla", "", accept_eula, False, java_exec, script_java, ram_mb, log, online)
    save_state(server_dir, ctx, f"Minecraft {mc}")
    ok = test_and_fix(server_dir, ctx, 3) if (test_run and accept_eula) else None
    write_report(server_dir, ctx, f"New Minecraft {mc} server")
    log(f"Server folder: {server_dir}")
    return BuildResult(server_dir, ok, 0)


def create_server(job, b: dict):
    """The 'Create a server' wizard, start to finish."""
    src = b.get("source") or {}
    name = str(b.get("name") or "").strip()[:80] or "My server"
    out = user_path(b.get("dir"))
    ram = f"{int(b.get('ram_mb') or 4096)}M"
    eula, test = bool(b.get("eula")), bool(b.get("test", True))
    if eula:
        save_settings({"eula": True})       # agreed once in the app; don't ask again on Start
    kind = src.get("type")
    if kind not in ("modrinth", "file", "vanilla"):
        raise ValueError("Choose what you want to play first.")
    # Put it in "Your servers" straight away, so it can be found (and its progress reopened) while it's set up.
    out.mkdir(parents=True, exist_ok=True)
    early = {"setup": "running", "mc": str(src.get("mc") or src.get("version") or ""),
             "loader": "vanilla" if kind == "vanilla" else str(src.get("loader") or "").lower(),
             "pack": "" if kind == "vanilla" else str(src.get("title") or ""), "icon": str(src.get("icon") or "")}
    rec = add_server(out, name, early)
    job.server_id = rec["id"]
    try:
        if kind == "modrinth":
            pack, title, icon = modrinth_pack_download(str(src.get("id") or ""), job.log)
            update_server(rec["id"], icon=icon, pack=title)
            res = build_server(pack, out, ram, eula, None, (), False, test, bool(b.get("aggressive")), 8, True, job.log)
        elif kind == "file":
            res = build_server(user_path(src.get("path"), "file"), out, ram, eula, (b.get("cfkey") or None), (), False,
                               test, bool(b.get("aggressive")), 8, True, job.log)
        else:
            res = build_vanilla_server(str(src.get("version") or ""), out, ram, eula, test, True, job.log)
    except BaseException:
        # Keep it in the list only if there's a server that can actually be started.
        try:
            if launch_spec(out) is None:
                remove_server(rec["id"])
            else:
                update_server(rec["id"], setup="")
        except Exception:  # noqa: BLE001
            pass
        raise
    env = detect_server_env(Path(res.server_dir))
    upd = {"setup": ""}
    if env.get("mc"):
        upd["mc"] = env["mc"]
    if env.get("loader"):
        upd["loader"] = env["loader"]
    update_server(rec["id"], **upd)
    job.finish(dir=str(res.server_dir), ok=res.test_ok, failures=res.failures, server_id=rec["id"],
               skipped=res.test_ok is None and test)


# ---- extra API routes for the app screens -------------------------------------------------------

def _server_state(app, s):
    job = app.job
    if s.get("setup") == "running":
        if job is not None and job.state == "running" and getattr(job, "server_id", None) == s["id"]:
            return {"state": "setting_up", "players": [], "error": None}
        s["setup"] = ""                      # the app was closed mid-setup; treat it like any other server
    r = app.runner
    if r is not None and r.status().get("dir") and Path(r.status()["dir"]) == Path(s["dir"]):
        st = r.status()
        return {"state": st.get("state", "offline"), "players": st.get("players", []), "error": st.get("error")}
    return {"state": "offline", "players": [], "error": None}


def _whats_new(app, force=False):
    """Release notes for the version the user is now running, shown once after an install/update."""
    st = load_settings()
    if st.get("seen_version") == APP_VERSION and not force:
        return None
    cache = app_data_dir() / f"notes-{APP_VERSION}.md"
    try:
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        rel = get_json(f"https://api.github.com/repos/{UPDATE_REPO}/releases/tags/v{APP_VERSION}")
        notes = (rel.get("body") or "").strip()
        if notes:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(notes, encoding="utf-8")
        return notes or None
    except Exception:  # noqa: BLE001
        return None


def api_get_extra(h, app, path, q):
    arg = lambda k, d="": (q.get(k) or [d])[0]
    if path == "/api/servers":
        out = []
        for s in list_servers():
            out.append(dict(s, **_server_state(app, s)))
        return h._json({"servers": out})
    if path == "/api/modpacks":
        return h._json(modrinth_search_packs(arg("q"), int(arg("offset", "0") or 0)))
    if path == "/api/vanilla":
        return h._json({"versions": vanilla_versions()})
    if path == "/api/defaults":
        sysmb = system_ram_mb() or 8192
        return h._json({"dir": str(default_server_dir(arg("name", "My server"))), "system_mb": sysmb,
                        "ram_mb": recommended_ram_mb(int(arg("mods", "0") or 0), sysmb)})
    if path == "/api/server/status":
        s = get_server(arg("id"))
        st = app.runner.status() if app.runner and Path(app.runner.status().get("dir") or "/nonexistent") == Path(s["dir"]) \
            else {"state": "offline", "players": []}
        return h._json({"server": s, "status": st, "join": join_info(Path(s["dir"]), fetch_public=False),
                        "other_running": bool(app.runner and app.runner.status().get("dir") and
                                              Path(app.runner.status()["dir"]) != Path(s["dir"]) and
                                              app.runner.state in ("starting", "online", "stopping"))})
    if path == "/api/server/public":
        s = get_server(arg("id"))
        ip = public_ip()
        port = join_info(Path(s["dir"]), fetch_public=False).get("port", 25565)
        return h._json({"ip": ip, "address": (f"{ip}:{port}" if port != 25565 else ip) if ip else None})
    if path == "/api/server/reach":
        s = get_server(arg("id"))
        ip, port = public_ip(), join_info(Path(s["dir"]), fetch_public=False).get("port", 25565)
        return h._json(check_reachable(ip, port) if ip else {"reachable": None, "detail": "Couldn't find your internet address."})
    if path == "/api/server/console":
        s = get_server(arg("id"))
        if app.runner and app.runner.status().get("dir") and Path(app.runner.status()["dir"]) == Path(s["dir"]):
            lines, nxt = app.runner.lines(int(arg("since", "0") or 0))
            return h._json({"lines": lines, "next": nxt})
        return h._json({"lines": [], "next": int(arg("since", "0") or 0)})
    if path == "/api/server/settings":
        s = get_server(arg("id"))
        return h._json({"fields": get_settings(Path(s["dir"])), "ram_mb": read_ram_mb(Path(s["dir"])),
                        "system_mb": system_ram_mb() or 8192})
    if path == "/api/server/diagnose":
        if not app.runner:
            return h._json({"summary": "", "findings": [], "fixable": False})
        return h._json(app.runner.diagnose())
    if path == "/api/server/removed":
        return h._json(summarize_server(Path(get_server(arg("id"))["dir"])))
    if path == "/api/update":
        return h._json(app.updater.status() if app.updater else {"state": "disabled"})
    if path == "/api/whatsnew":
        return h._json({"version": APP_VERSION, "notes": _whats_new(app, arg("force") == "1")})
    return None


def api_post_extra(h, app, path, body):
    if path == "/api/create":
        app.start_job("build", lambda job: create_server(job, body))
        return h._json({"ok": True})
    if path == "/api/servers/import":
        return h._json(import_server(user_path(body.get("dir")), str(body.get("name") or "")))
    if path == "/api/servers/remove":
        sid = str(body.get("id") or "")
        s = next((x for x in list_servers() if x["id"] == sid), None)
        if s and app.runner and app.runner.status().get("dir") and Path(app.runner.status()["dir"]) == Path(s["dir"]) \
                and app.runner.state in ("starting", "online", "stopping"):
            raise ValueError("Stop the server before removing it from the list.")
        remove_server(sid)
        return h._json({"ok": True})
    if path == "/api/servers/rename":
        name = str(body.get("name") or "").strip()[:80]
        if not name:
            raise ValueError("Give the server a name.")
        return h._json(update_server(str(body.get("id") or ""), name=name))
    if path in ("/api/server/start", "/api/server/restart"):
        s = get_server(str(body.get("id") or ""))
        if app.busy():
            raise ValueError("Wait for the current task to finish first.")
        r = app.runner
        if path.endswith("restart") and r.state in ("starting", "online"):
            r.stop()
            for _ in range(240):
                if r.state in ("offline", "crashed"):
                    break
                time.sleep(0.25)
        elif r.state in ("starting", "online", "stopping"):
            if r.status().get("dir") and Path(r.status()["dir"]) != Path(s["dir"]):
                raise ValueError("Another server is running. Stop it first, then start this one.")
            return h._json({"ok": True})
        try:
            r.start(Path(s["dir"]), accept_eula=bool(body.get("eula") or load_settings().get("eula")))
        except EulaRequired as e:
            return h._json({"error": str(e), "eula": True}, 409)
        if body.get("eula"):
            save_settings({"eula": True})
        return h._json({"ok": True})
    if path == "/api/server/stop":
        get_server(str(body.get("id") or ""))
        app.runner.stop()
        return h._json({"ok": True})
    if path == "/api/server/command":
        get_server(str(body.get("id") or ""))
        app.runner.send(str(body.get("command") or ""))
        return h._json({"ok": True})
    if path == "/api/server/settings":
        s = get_server(str(body.get("id") or ""))
        apply_settings(Path(s["dir"]), body.get("values") or {})
        if body.get("ram_mb"):
            mb = max(1024, min(int(body["ram_mb"]), int((system_ram_mb() or 65536) * 0.9)))
            c = ctx_for_server(Path(s["dir"]))
            set_ram(Path(s["dir"]), c, mb // 256 * 256)
        return h._json({"ok": True})
    if path == "/api/server/fix":
        get_server(str(body.get("id") or ""))
        res = app.runner.fix_and_restart(accept_eula=bool(body.get("eula", load_settings().get("eula"))),
                                         aggressive=bool(body.get("aggressive")))
        return h._json(res)
    if path == "/api/server/fixlog":
        s = get_server(str(body.get("id") or ""))
        log_path = user_path(body.get("log"), "file")
        eula = bool(load_settings().get("eula"))

        def fn(job):
            job.server_id = s["id"]
            info = {}
            ok = fix_from_log(log_path, Path(s["dir"]), eula, bool(body.get("aggressive")), False, True, True,
                              job.log, info)
            job.finish(dir=s["dir"], ok=ok, retest=True, report_only=False, eula=eula, server_id=s["id"], **info)
        app.start_job("fix", fn)
        return h._json({"ok": True})
    if path == "/api/update/check":
        if app.updater:
            app.updater.check_now()
        return h._json(app.updater.status() if app.updater else {"state": "disabled"})
    if path == "/api/update/install":
        if app.runner and app.runner.state in ("starting", "online", "stopping"):
            if not body.get("stop_server"):
                raise ValueError("Stop your server first, then install the update.")
            app.runner.shutdown()                # saves the world and stops the server, then we update
        if app.busy():
            raise ValueError("Wait for the current task to finish, then install the update.")
        if not (app.updater and app.updater.install_now()):
            raise ValueError("The update isn't ready yet.")
        h._json({"ok": True})
        app.request_quit()
        return True
    if path == "/api/whatsnew/seen":
        save_settings({"seen_version": APP_VERSION})
        return h._json({"ok": True})
    return None


# ============================================================================
# Web interface (local only): the app serves a glass-styled page on 127.0.0.1
# ============================================================================

import hmac
import secrets
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

IDLE_LIMIT = 180          # seconds without the page talking to us (and no job running) before we exit

BUILD_RULES = [
    (re.compile(r"^Pack: "), "read"),
    (re.compile(r"^Downloading \d+ mods"), "mods"),
    (re.compile(r"^Checking which mods are client-only"), "detect"),
    (re.compile(r"^(Installing (Forge|NeoForge|Quilt)|Downloading (Fabric server|vanilla server))"), "loader"),
    (re.compile(r"^Test launch \d+/\d+"), "test"),
]
FIX_RULES = [
    (re.compile(r"^Reading the log"), "read"),
    (re.compile(r"^\s+\[(high|medium|low)\]|Nothing wrong found"), "diagnose"),
    (re.compile(r"^\s+(Removed|Installed|Raised|Lowered|Set eula)|^Things you need|^Fixes applied|^\(Report only"), "repair"),
    (re.compile(r"^Test launch \d+/\d+"), "test"),
]
STAGE_ORDER = {"build": ["read", "mods", "detect", "loader", "test"],
               "fix": ["read", "diagnose", "repair", "test"]}


class Job:
    def __init__(self, kind):
        self.kind = kind
        self.lines = []
        self.state = "running"          # running | done | error
        self.stage = ""
        self.dl = None                  # (done, total) while downloading mods
        self.result = {}
        self.server_id = None           # the server this job is making or fixing
        self.lock = threading.Lock()
        self.rules = BUILD_RULES if kind == "build" else FIX_RULES

    def log(self, msg):
        with self.lock:
            for line in str(msg).split("\n"):
                line = line.rstrip("\r")
                self.lines.append(line)
                for rx, stage in self.rules:
                    if rx.search(line):
                        order = STAGE_ORDER[self.kind]
                        if not self.stage or order.index(stage) >= order.index(self.stage):
                            self.stage = stage
                        break
                mm = re.match(r"^\s+(\d+)/(\d+) done$", line)
                if mm and self.stage == "mods":
                    self.dl = (int(mm.group(1)), int(mm.group(2)))

    def finish(self, **result):
        with self.lock:
            self.result, self.state = result, "done"

    def fail(self, message, cancelled=False):
        with self.lock:
            self.result, self.state = {"error": message, "cancelled": cancelled}, "error"
            self.lines.append(("STOPPED: " if cancelled else "ERROR: ") + message)


def _safe_name(name, fallback="modpack"):
    return re.sub(r"[^\w\- ]+", "", name).strip() or fallback


def windows_drives():
    try:      # bitmask from the OS: no disk access, so a disconnected network drive can't stall it
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [f"{chr(65 + i)}:\\" for i in range(26) if mask >> i & 1]
    except Exception:  # noqa: BLE001
        return [f"{c}:\\" for c in "CDEFGHIJ" if os.path.exists(f"{c}:\\")]


def user_path(s, what="folder") -> Path:
    """A path typed or chosen by the user. Must be absolute (relative paths would land in odd places)."""
    s = str(s or "").strip().strip('"')
    if not s:
        raise ValueError(f"Choose a {what} first.")
    if IS_WIN and re.fullmatch(r"[A-Za-z]:", s):
        s += "\\"
    p = Path(s).expanduser()
    if not p.is_absolute():
        ex = "C:\\Servers\\MyPack" if IS_WIN else "/home/you/servers/mypack"
        raise ValueError(f"Use a full {what} path, like {ex}.")
    return p


def list_dir(path, want_files=False, exts=()):
    path = str(path or "")
    if IS_WIN and path == "":
        return {"path": "", "parent": None, "crumbs": [], "dirs": windows_drives(), "files": [], "sep": os.sep}
    if IS_WIN and re.fullmatch(r"[A-Za-z]:", path):
        path += "\\"
    p = Path(path).expanduser() if path else Path.home()
    if not p.is_absolute():
        p = Path.home()
    while not p.is_dir() and p != p.parent:
        p = p.parent
    dirs, files, error = [], [], None
    try:
        for e in os.scandir(p):
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    dirs.append(e.name)
                elif want_files and (not exts or e.name.lower().rsplit(".", 1)[-1] in exts):
                    files.append(e.name)
            except OSError:
                pass
    except OSError as ex:
        error = str(ex)
    dirs.sort(key=str.casefold)
    files.sort(key=str.casefold)
    acc = Path(p.anchor)
    crumbs = [{"name": p.anchor or os.sep, "path": str(acc)}]
    for part in p.parts[1:]:
        acc = acc / part
        crumbs.append({"name": part, "path": str(acc)})
    parent = str(p.parent) if p.parent != p else ("" if IS_WIN else None)
    return {"path": str(p), "parent": parent, "crumbs": crumbs, "dirs": dirs[:800], "files": files[:800],
            "sep": os.sep, "error": error}


def summarize_server(d: Path):
    removed = []
    for bucket, folder in BUCKETS.items():
        f = d / folder / "REMOVED.txt"
        if not f.exists():
            continue
        seen = set()
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            name, _, reason = line.partition("\t")
            if name and name not in seen and (d / folder / name).exists():
                seen.add(name)
                removed.append({"bucket": bucket, "jar": name, "reason": reason})
    mods = len(list((d / "mods").glob("*.jar"))) if (d / "mods").is_dir() else 0
    return {"mods": mods, "removed": removed,
            "eula": "eula=true" in (d / "eula.txt").read_text(encoding="utf-8", errors="ignore") if (d / "eula.txt").exists() else False,
            "scripts": [n for n in ("start.bat", "start.sh") if (d / n).exists()]}


def restore_mod(d: Path, bucket, name):
    if bucket not in BUCKETS or Path(name).name != name or not (d / "mods").is_dir():
        raise ValueError("Can't restore that file.")
    src = d / BUCKETS[bucket] / name
    if not src.is_file():
        raise ValueError("That mod is no longer in the removed folder.")
    shutil.move(str(src), str(d / "mods" / name))
    rf = d / BUCKETS[bucket] / "REMOVED.txt"
    if rf.exists():
        keep = [l for l in rf.read_text(encoding="utf-8", errors="ignore").splitlines()
                if l.partition("\t")[0] != name]
        write_file(rf, "\n".join(keep) + ("\n" if keep else ""))


def open_in_file_manager(path: Path):
    if IS_WIN:
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class WebApp:
    def __init__(self, mode="app"):
        self.token = secrets.token_urlsafe(24)
        self.mode = mode                 # "app" or "install"
        self.job = None
        self.port = 0
        self.url = ""
        self.last_seen = time.time() + 60
        self.httpd = None
        self.window = None               # NativeWindow while the native window is open
        self.window_closed = False
        self.native_failed = False
        self.uploads = {}
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.runner = ServerRunner() if mode == "app" else None
        self.updater = None

    def busy(self):
        return self.job is not None and self.job.state == "running"

    def request_quit(self):
        self.stop_evt.set()
        w = self.window
        if w:
            w.close()

    def start_job(self, kind, fn):
        with self.lock:
            if self.busy():
                raise RuntimeError("Another job is already running.")
            CANCEL.clear()
            job = self.job = Job(kind)

        def work():
            try:
                fn(job)
            except Cancelled as e:
                job.fail(str(e), cancelled=True)
            except Exception as e:  # noqa: BLE001
                job.fail(str(e))
        threading.Thread(target=work, daemon=True).start()


FONTS = {
    "brico": base64.b64decode(
        "d09GRgABAAAAAVoYABQAAAACJUgAAQBCAAAAAAAAAAAAAAAAAAAAAAAAAABHREVGAAA2wAAAKZ0AADSXbM75V0dQT1MAAGBgAACJVwAA+uDok1w9R1NVQgAA6bgAAADRAAABTDwsRzFIVkFSAADqjAAAA2QAAARO69/+sk1WQVIAAO3wAAAANgAAAFIg+Ag1T1MvMgAAAjQAAABUAAAAYGcqeSRTVEFUAADuKAAAAKMAAAECdMJlqGNtYXAAAASsAAAB6gAAAnhANIkgZnZhcgAA7swAAABdAAAAjAP93hFnYXNwAAA2uAAAAAgAAAAIAAAAEGdseWYAAAicAAArWAAARZYrt+lqZ3ZhcgAA7ywAAGrpAACXLLp8FrpoZWFkAAABvAAAADYAAAA2JaYP/2hoZWEAAAH0AAAAIAAAACQIMAO1aG10eAAAAogAAAIkAAAD9OHYEd9sb2NhAAAGoAAAAfwAAAH8i3GdOW1heHAAAAIUAAAAHQAAACABDgDAbmFtZQAAM/QAAAKuAAAGygJcBm9wb3N0AAA2pAAAABMAAAAg/58AMnByZXAAAAaYAAAABwAAAAdoBoyFAAEAAAABAELrBG+AXw889QADA+gAAAAA2w1LowAAAADm9oCc/zb/MgVVA2kAAAAGAAIAAAAAAAB4nGNgZGBgXvTvEwMDa/J/s39JrKEMQBEU8BcAmgEG7HicY2BkYGD4y5DOwM4QzsDGAOIhADMDIwAtTgHPAAAAeJxjYGESZ1ZgYGVgYOpiimBgYPCG0IxxDEaMtkBRbjZWFhYmBiYWoBwzAxJwDPEJYjjAoKAkxLzo3ycGBpYOxglAYUaQHJMA0xQgpcDACwCvUAomeJx1kjFoU1EUhv97b2rT2CZNSCP6Yt6SmjiIUGhEh1pRELWIWOmg1hZFnERB0S5mEBwKDmIRnES3TlW0FB27CKKTOOmmUEWFgq0dtPr83kuU0Jjh47/33HPPPfecY0dUtlOKtcKNoeP4XNWmiPOsbyhl8+gazG90Up69prKW/iEtBR/hndmruL2A3yuVXQXfuTp3uLOWezV1M+hBfA5FeXr2HHpAPTampL2k3iYmyHlKnbyz3g7LRmzRBusr3Qp3HB1U0XrKRQzCLtjYjJkgbi//6FNRn9UDxRrBT3gf2VaDT1rmz2A3187dSeL31dlBrBa4++gAPjVy5hbaxn+rKpqLarPb5DeRkXM+Ofn0ylP2L+YDnFKHuUvOR+WZCvaSum2/EnaMHvbz1hHs36jXOOsT9HefClCGTuiCBOQb7KlwrWWliJshv6SZVdokgstuWCk3BG+UbDzXS2YgZEVZzQcr+qGcvgQr9jv5dcqPuEmc6+oOfcwovFCc+2V7Fvto8DVEz4NVKaiYncQmvmsnl5TUuDfblYe42RORMiXmraRM+K6phlCPqqzm1QHWvFYB8qYL7UKPke+Mkm6amXlGDSaZs3XKutvU5wH2R3JmBJ8nYV7oW2YczBz7p/iG87MbpunVQ3qynzcWOF/Q1gZtJLS1h+uwj7Yg380q2XaGeItqt4cVM7+Ii1LB7H9Ir7WZx+DVOY1tEb1Cj4bQAWX/AO5pogt4nE2SR0zUQRSHv/8f7AUFREDcfbsqKjZY1gJWEOyiQjT2iooFe+PiwXjQxIMGFRU0WKMEjQfF3hJF1JhYgwVhBIklXtUDZteXdWOcZOblvck38+b3G8AGQnRG6moFYqidq9GLEKrZAFLYzknK8VtJVo5dab+zayRSoiRWHOKWeEmUZEmVDCl3uV2F7nC/X2khkVRKOc0FpbKDVIRS0RInEqA8khKk9gapCQSH3wk+j8/pc5his+9vzRSYClNidpssk1BXBrVV2q9XN6byhlq+g7WHf8Paae36Pw/UdgTCYe3rPmd5TSMPecAlLnOGt5RwnmLOcZRj1PFBzzxBpd7Rita0pT0RqkYnouhCHF1x4MRFb/rQl370V528DGQQg1Wvq5RxjQYu8lNVSGc0mYxlEpPJYgozmMksZjOHeSxlOXmsYBWrWc8GNrKJrVTwjCt85AjHeUW1vu85j/jBY7Zxl3qe0MRTDEUc4hefqOIUvzlIAffYTyEH1K9m6mwILdTD5rSkDeGE0YGOxNKZaGJopx71wE03etKd98STrJ4l4WEICaxkBEMZxiiGM5I0MpjIOMarQ9OZRjY5jGEui5jPApawkBoWs4581rCWzSxjC73I5TZ3uM5NbnGDF7zU3/CNz3zh6x+1nIihAAC4Af+FsASNAAAAAEQAXQBpAHUAgQCiAK4AugDqASEBWwFnAZEBwwHiAe4B+gIGAiwCRQKDAp0CqQK1AsECzQLgAv8DLAM+A1sDcgN+A7QDwAPMA9gEFQRTBF8EvATiBQ8FYwWZBecF+gYfBisGNwZDBm8GggahBrwG0QbdBvUHRwdTB14HaQfCB84H2QhdCKEI2gjmCSkJegm4CcQJzwnaCh8KTgrACusK9gsCCw0LGAsjCy4LQQtMC2cLlAugC+EMBQwQDEAMTAxXDGIMmQzRDNwNRA2IDdMOGA46DoQO1g79DyIPLg85D0QPcA+DD6IPvQ/4EAQQDxAlEHcQphDcEO4RIhFZEXkRsxIFEiASiRLeExUTLxNkE5wTvBP3FEYUYRTKFR8VMRVgFY4VqxW5FcoV2xXsFfUV/hYHFgcWBxYbFi8WOxZHFlcWcxaPFswXChcTFykXTBdYF3sXiReWF58XqBe0F8AXzBfYF+AX6BfwGBEYMRhzGLYYzhjlGO0Y+RkcGSkZPhlHGVMZhBmgGbsZxxnVGloayxr2G20b0hwrHFAcfRyLHJccpBy4HNcdDB1THa4d9x58HqQetx7DHtsfBx8aHy0fQR9cH4Uflx+qH9ggQSDWIP8hJiFOIXUhmyGxIb4hziH0Ih4iKyJRInYihiKOIpsioyKrIrMiuyLDIst4nLV7CWAcxZV2V/WcGk3P2dNzXz3TPfeM5tSMpNF9WpYsS5YtyYdkbGOMwQcG4xNswOYwxtjmdjCIY9klu2SBJCyYhLDLJmF3yUFCyCZ/siELya4XMIGE7IJHW9XdGsnYhr1+wXhm3lS9eu/Vq++9V1VNyIjQzGswAV8mSEJJ6IgoQTAGnyHoM/hM/lw2k6bNPFcmc1mO9SsUSj6fz6QtFtpMkUwIKiorQbny8nUtLS1XyvRON80YtF6vVsbkXe5SkPF7zWQafnS2dg2MVdz16O9WLuE0mfUGnUqnXqmjjMmoP+xgPHaCgIQM/XMHPI6k0BBExpQxsSRL8jkfLft/P/z5U8cVd6XAY/D42Y3QcPb9q7dunZkRe5BlyBGAIFC/d4m3iZuq9IEq/T3iPuLGKn1BlX6a2ETchPSW+Agj684dG/AZZt74lbcnn22cJwRcsHcvUeV8WZXz+8QPiZur9NYq/QxxGI2owHSyHekqJ9SEljAQNGFD44KMUhyVQS+fIWMg0XsQWwC437rU8kYKNAyBfGXs/jBQVv4d5ODxG5AUmzdfffAglgnod+w4e/AgYk2E0LiDiL+JcBJBxNngS7shbUYTSOPJUyhYPpPO40llc7MfQqCmaVU76+ttWDvReNWSfTsz9c3Nly4tNubzk/C4JdoSDbXwcrV8pLPUz+8P+rnglVyQZZFm9pnfwcvhNMERhNyfgLlsGWbQeEplnkNeQ6GBLRY0Sp5nLBQES0au7WcDC64Z7lnfbFXcBW0Z3pdhjd62dCJnkd0nt7VeQeUnd3V2712VD3cvTxujZb89uzDTNpWL9CW0hbWLEsiy9pk0HhNZtkaw7O+IrxHYi7DmI0hzK9KcyGDlkHYJKOgp+i3LIstufZJrDVnCDdz9x64+3sWWB5ZF/2P6oHd4oL4/bj44cOfaga0DCTO2KrIm8gm4EfEkEU8vQZjQ5JguylkH9JUzuy7EHpzYW3nrwmPIBLkJYYxZL0S8WOyBkg+EpsNgVeVhUFf5HliFOx08KE04sobQW1gJlLQSviKsBJE+UKW/R6wRVoJIX1ClnyZKyC/ls3yqUpjOlyOIVsQ5slTekNZEVaBn0KIQfBCcrmok8OElPqHpIbCo8hWwFXc7feONwI/cFhAOyY+i2I847jN+pFDQZjcUzI5MTtNsjkXeFB/d1RocuHa4a22LXX4PtGcC0RbWHGrguLxr/R83qeV1S6jR+9b0Xbcqx3WMZYzxojOxvDMzVHA5kp6tu3/0dHk0NrtilsxJi1ff7AoEz0yffR1pfKmw8PFCU918M1IO9yH+TugjaBd6BP8oWhfYhNmwSLNBC7OB/z4UZkOkfzTzb5g+8xvUvpbsrdI/nHmduJGAMz/D/KXZEEYgse0fOTKLQS8hOwNCNfMxcCMpDAi5eXGh5VBbvMj9nN1Yy0A3S02nJnt0RsUxElpSHHiosmpkcz2UdICXoN4ucYQyWchQpJLOuyHDsDx2bzRbqd6sX/212njHeLFpUdpVk3uWU/uyoSI8XnlH39C/NFZcO9JKq+S2VJPn0WOBuPmP4koEr0gWrXpRaPoA2IeFv/VWaXSSQm1Ef0e4i1oZMPayhtCHB+GN79wN7zp8DKJxouDHyPAfVHT4Jcl9GPXUSD0xf9zrp6fhQ3//R9yjFXwT9Xitkpb8/bCAwx4Jh08if4eCx+1EHmck2FnswgEOYyUFeWGFz7ke6OnaPBBeeNtE16aBcHRoS3O8weVpSsQa3QY2S8VGrx++5r7+6Oi+JaP7R6M3xSc6eyfrkiu7cotydhwjhLEEr/BJXvGO4BUifaBKf4+4X1ijIn1BlX6a2CxEK4mPIDP/xVJjn/lcyT3InT5fetiLHE0Y+Qy8AT6GPlnwyAzIgMIXjO4AZ66tGJ77nOH18Nmz/fLzxl/ecSHrtVatcYa4Q0Asx8x75FF4knATfqJANBDNs9ghJipMBsmXw76HCAUEGAUMYWmU0CDsJkUpLZaghG3A3bG9p/Oq9vySnLIGKsj77oAyqFblR94El549ZY36rVqtzR+xRss+XxmoQH3lO2CdccGJ7dvu7k1OLvC0tToNZzeyxkyju2dlwnjw4BUGf6YjomZbskG6cXWptLr0iBSjxUiF1jFhRrGKKOTmxWMxVLNK5NMP72pqLNTfdLi4ujfcfWDqnekX9tcV87n9jtKy8uCG3AtC9BB5NSFedmQHgRfO2JB+n+V2NeVjnUaD0+NQHbgjP7Ug2nVg6rfTq/fDGibsQ4rVwH3OxuWtA+tzqyXOjpmF8Fq0QsJEEVlWoUiQypwFoUQOfRIBWoh9UMlYLOl8IXjO/FsscGvHZF/X4s2LnANjS3ssg3tWNXv4/qv6Jw905lfu6gINE/3dwco/sg1BT2PM18gFyl/SKVU6nUp+EwTKWmOtwiJPDG1s7d8zkV5038bJ6xeF1Uq1pla3P7hi8cBUglsxPDLJSxaYQBZwEAEp3yEFseh5QGaYS3WMscHWlJ2Od9e1dLFq9hmbwpbgFq8slPIFpLnOnWgOeQoxl0JFxtr5Qzf4I+Zfg8lINJnAqEUjX6xHNsmJq69QkMIS0pvHVhFdD4UtNKpkBwaLo1CAaNdlXT54N2Qbg+6EI9Acii7pirZe1Tews015HFCumNsTpr2N4ejSvnjnDsrbuCTrSxQdOofR6rFoal2xrmzLeDK3rs/qTngNeqeRdtKInOwrti2LYa9C8Rz8YT7q4nj7l9OVF8BvhZhRg4AXEEGkwQBqZRE0IIV4MT90gGJxvDVKT7szPn/GPX2IsqfaR+rAkcq1mSa7vTELbq5s3n8Er0uBk4BqSQnVfiSgmkgfqNLfI24WUE2kL6jSTxMrBSSW+CCJ7BeRSQCyz8j1HYxdF5QNvoYxCwgZ+DahqiAypBBhyB99/ys3kzd95fvIHr+DFPJyQNQieDsoxSGxldASR6KjB54aIld9czU5+PiBt0bIURRZFOA/8AtFlzMVPeqNYhBcg3prce1AZphMIUOaWJ5Var71rZc2wEtfeelbf7sOXg66wcKPP658tfL1s2dRLxR+4RDqpcZ5qiQa/dOfvLQeXvqNN34Onqv8I+AqPT9HNhNaCjYuSDZ+BdkMEAyi66QYmEPzjBGMRtWDgEsfVr4LGm8Fe8hbb6vcSN6KfcOAbAygG+VXjQi9pQyrUIZSccegQk9cz7QT0mw2ixOsQhr9nitQcuTFT4V6ElxLfzCbNUVZS8wJjsBI79Lo4Lq6QwpZZlhr4FmLQxVMJEO2IOVsztU1BU3Kmlo95XLrw1xN83DS3LBpcKWvvc0X6AvHF8Sv0bpzvJXU6tVqnc1idyBdRRmfR7qWJF2/RoAq/VWJriDe20BUqd+uUk+3AOT5Eg9Bz5b/kaYm5Gv/HW1l2A//JxqDH+3dW9Xj7arW7xOPELIq/cWqfmdWAXEeyYVoHgfQ2vmMdlKVLibOTKZMCgm0PBecy6HxmipDoRZDCSLHsxSJVS8UGEqGfn2aXZAJNnT72IJX67G7olaACth1xUAx5lbJVZF8PRMfanIrKteDeLtSH3IxUac77TZ7aTV5WGFvWJovrSxrlMp4uc2p8/usspokXSrnfEHK1VpIlHwmhVJno7U0ZY54tQPLQzp3HRvKODS+ri1DqjF5JFD02ZsKhYUhvSNgUicW5F1sXzHezJu217qy/JIIbaX0dsYm7BgEkHUi8BTC/KQY9XkWVQWGjFDoZmZjk0Ixl5RQJC1kclPxjrhGefu0XK1QqxM9iYalqdzEVnfEgqSiXDZzrcEZoEKDpUeeh/ABR7nN37aYa7x2YmpveR/fyvMdkRral3Qrg/mAAa1DC5LDgOYjOL/+FQLAhepfX/e6Fre7eW33yKVh9SFo4hzWoE3LJANBrlZ2l8JcXEVF+1cX6tcOJZrXlDSBJGMM1vPpHi5QcChjw20c8gsLqn7RiMgv2qXq96BQ/WJJnLMWYS6QB4lVk+jq8xKgXxYncqllDYnuhFotr5FP367UxNs1poDTUGu2uyhPxGyJUOW9kxM7GrnFyBplxwNnK88/UhoM7TME8kGlO+mjayLtPN/G4+xw5gMYgK+iWjmDEWsuQxXkkPEFt1zMFUUpqkGSLDBKYCtPtvszYwWus2BQyqyJ3o19wWU3Tbbyen/HmnaDz67XO1ijK2RmIod+XXpb529Z0TBweb2nv5Pr727OGKf0hZ6R8eWx9vWdgX06V9KjcERden/B46v3g2cea3ickGasBs1Y6DM7FuculOo6AZ4Fa8sOV9NUR8dUyaCp7AMkrdCF2GDMmK+3es0qcJw0ZVdQ0QWTueL6oWRy5MoWxTJ5JtHs4zqTQ321jI9W+boaOUKYOzw2RrhuCeH+XEA4kf6qREcINzXX+ttV6ukCkOYZ8UDyx/5rGgjB87+mxTGMZV+oCewT4yuKPWAjmmsPzjbYHJOrzjWjFMQQnD9Ds4aR3oebynTd0kK826V1pO1uXs+612wD7ds0KmUMwUp7wMocN5nsTp1dt7vyIq44VDOrIQP1RJboJBZXPbqQ8+O3TE7ybx/Nz25r+TlewrjZxJev7tOIP5jEHFAU7Pdj62Nd7ZlSrjHfklvXC5RfHW6Ir+yxxPUGk0pX8xDd3Bxpvv6S9haz3sooD3oZR13aywa8nqDe86CzzhvL1chZjyuh1PY1ZPujuxmVJhcMlnwymRKlsHIotyHW/cHDan0NpVcCjamW0soAF2LZaJRl/EZsQZSvQhNas3i/yoDBC2WtInjRqBgWAxZNBxBWqUm1OtafX7J42u13p1yAAY/dSHcOdFuHL6vbsbPyYxCoz8ZXDzyD/YZG3vIj+D3kN4sEv/k3AMSxiL9F9pR2LALTUH/2DGqN6MAs+KTY+l098siZT9Dn9wSPFKkf/QZT30JtlfCVKvXD1y42Hpz5Lfr8JvLTc3Yw8tPCDgYaGTbi6Dfzm5nlxC+EvkvFvhokq0D9PpKVEuad5y1iHkjBiNZBaXnvtC3R+CgJb4EOHrxTMeQXJGnJmnpkzc/Zzwg86K/z08rnNeHGwVR5MGFTMk9pZFQomoSnKrcbE7mCI7SgNWtQ1ejCZW7PMm89+3rVdqfm2e7U2Q6RTjYhLElgei5DS4FHnEPkg8IkSrM472PgFpUy3JlKl90AuX9PXbgxbJ7W2s32mONBrcNsizpA8PRPRtbE69YODq9JuBsnmio/Am4uY0uu7HkB+LmUNbxy4IU5D3LjfHCeBOd4zy0qRaivOLp02hdwJ+wg+vZPBtamt26pvAK4fD6ycvAFQvQCxAdnGxNitrF2Fmt8iLsO15bza+tzQ6uwuoC9/5q2zu0LBq5u7dze7YlYrHEviqo5qmnXiqldDU27Vk7tKm1nO5LZbo7tSPWPSAjnE3xvhYSHX6/ioU/wvhUiHl5OVKnfrlJPtwIh4gg8BBn9XyilAIYXkdSI0e9i0oLXhZ1Ny8ybaLQi+mTCo4n7IJ874hpgH6u8s+LCQ4bg1NmT9i+00ItVnc9MSvNCHkXzkif6PrvnUY0FmfMjgZTzidFXCL44QUE5kbz5iva2K9pCQ51mhb55cbRlLG9QVY7+EkZ4V8TSEjO4aA24U8a1D4UzY2W1XN08FmT81tpaq5+hfZbaWouPato9uWpnialPJ4r2uiWbyoqJWM7bml62qJZhGXVhYZL2dRZyXf7tZq4+wDeGaTNXDPANYVrI5jbCENJHzF3mrSQJ2JHUaDXRprlkDuJkjgwcVmpinXXlpfHMsvp4ZxxlMCr5am/YRIfdUgbjPA5+e+qRhkG+edf41O4GbrDR1dzk3A80odZAqJU3sHlO4Y55zWoxq7wSyXEKZS9ZLMf56aQkC3mOLGJiGZhWqmVInK7UPHFIpZyU5NG5bSaN0c0eBe+A9V82FFvsjYJQq7FQTZZkIk5fD1Sh1iCSSmNhk25FMOulxMxuI7TPWkc+u5vFnjPtyGQGIO1tVZMqcK1cLUNJbme8sCwTX1qu64xpFHdUPqGcdhqnuSZ3mDaFju93NjW7mga4ht1T47ua+cGGR16oMECjNnvibgWXZw18ayjQGhLRBtwAnRjx56GNFO0x1hxSKdicz8ibVJaIyRtDeccHPxyZCtdontVqA13pH4g7KOD3SJv0OTso52+gVDOI2f0TTW6inSVvh/aEzxPXR8OZxSV3w/qOpg1NqltJS8TtDzJ1vL+vJZRfT9GJhYVaX9RM27QGXY3WHsjxhYVhbrRb6wrTNpOO1qmVlmBjoq4vJKDozIfwCvgQ0SCuJlEmaY3k5k80xlVcWEneQIt5BvhSaTyh2A8oK0WZKYU91R7OtXvZgscfsWrgtCXeubJxybZGT1dD62g0vFzL9ebMJlttjYHRulizcmety+4KGIxOn7e2MgkOZUfbE9bEcInriCjkke5MclEKySif+Qgsg7fgPRIUE3P4IAQ5Ap4EmhUWeb5AKxQfr5gokeWsYtWOHUEH5dZ6UrWr1vxsnaq9A+7Y/x3Oq1LerpYjbh6EK2oUYa0iN2HDRVC5Wih8Z3R42uexBJ266VuVikgfde1uEKm8WUowyb7s31d+8d5rC6fwaZzICWP4RgnDnxIwXKS/KtERhl9CVKnfrlJPFzGeSTzEk7uLyIOR+7MyhTFiX0guIUXF0QHvkujn7wBNn/zyhGzV9Emor1wDbj17Bp+ioCiOM80L7ADdtOfLTfKOZ9rkbQ/vej0vr0e9rgU349fZM2BN5YS0y+RAvc/bAZI9/uiphfIFz598+Ju9siGgBsonn6z8e+XjJ55Avcwza8AnqFdYzGz5grTdlZtd2Whi5yxgpshfuXt6PMHAGFvX7lfDcFeiuIB7OMCbPeaahy2x9okTNRbDI34nvyC/vGssUtq29I0vLxmgo63RRyqH8qsW1rux9cVR8Vxtk+bqCWGuRPq3JTqalTTOGM1oaXwPySjsMZly+AyHRN5m/uQP4JlPKjcDQrH/lsqMbD/GKDUKhINoBcXwGjKdvy9B81KVIm27IBRVKAqCxgVh7+EKa8Jt4ZI2j0+rNtbqGOoKb6Zki5ZY3aUQkDVFmdpu1sutbofRDDLGCOsJWSm5Qq2pUdUqamiDPJJ3a2yptnCGjoRpKmMOFpeqXS4rqFErVTpKRyEZNUjG2+HDaKZcgowS2FwgZlteXbIhs6ywbH1mWdZsp3ROi9mupVwgs2xF6xC/fHnLUHCFMeBwh2hz0OEKmQjpPKkApwnDuedJ4ha5GHHnnSdxrWs6/M07ly3d3sR2ris7OKuGjgfZOF1r56nksj0Dm08sHnrgysG9Y6l9tsxAtu+SdGqyNzeYcxDSzOCzM5VYeaGlnzFsXBYu1hwGX7vNlRf2NfVIKCOSx49nryBc4xACJ8Nzc3IplXSQdn9Eey21Ft7hDdNy8l51/bWjC68oOepauetA6fE/oTx+1oQKFisft+gGNjUkx5sTfY1Z65AM4pPbGqR3Ct41q3WhoJjbElfmCsLWJErCRcQEdNuaDh+8F2jdCa+3zpHZsvylytvg52+Mbi4076HsmYVZhg469SbOFU7ra1bdKDtw3d1yeWai2DiWxprj8/geNJoRn3WgDN+HDw3xXr+P1qXaU0ETUD0sM6TqfeCu7ffkV69YlTl7OXTmLhkLyw7OVv9O1J+bRXoBOWeTISWLD/iVmblkDmhHr8zAe9OJbKrW4qWRJ+hlj3rBqcrfG2Waga7mJdHyDiq5vJUKx0tZZ8RFqfRWPbj+wD+UuovrOhZtyEqekUczwQvnKLN+hw+3s9VNIibDzR2onOctTYeP9+xbkhhpYcl7nLzRGXPpFDIZ9KAVUpiotxUn+2mfTWsJOZwRq5bxU089su2BQWtmqKjnwnp3vjdiq3fUd/FUfLyNX9QW2qe1+c2usMUadZu9TC0h7oKD3yC7YAzEK1eYNhTwpnzlsWLlefCz4IJysOvQgd0F2YED/LZD9w8QCPmwNfuQZiYiiHLR3rlctJArMPRsEX6uMqbZonz2eITPzV8rCiUNUiPXdQcHmjZsaBjgu/cOm0Ms7cn5/TkXzYZNjojV4PTp6IjTGrFtyHZ427b1D25r6RqmQsMt8Y6oXGmzKeXhjmTLcOhGldFjRY11PifjMamAgfI5LG6DivK5nD5qVUCtZJtiLYtD4eFyuU+jRnOF77O0II0iYi6A/dk8mwzg1M8ym3jN5YTk/KwUZ1221PJuTnGP0e3X+VI+PSkjFaQr2+zJLS/aSqt7Jm/q6L1p3B2zMlGkkI1iwpQ1s7iodwVptSvfHdFxIVO+M6RPjLdxA23h4Qc2XnXvAnCtNeaycxatzWd2RixIUjeSlBPwJvB5eCNl+J6myTZ/+ZrhxVub2PbVJcZv0ZjDXguLcncrexHAceSHCiLiiGdsTuHEQjuLORh0sJf87PpUo19xuPIk+AihTy0bOLvxwAHJpxAySAgU/DwEsgOZJejQ2eJef8KulN+vKV2/fPG2sjvfyd8JJi4IQckFTWnLYgxBaCStYIu7Zi1xMQySsjVj0+oOP7wH1DrjHlQR110x/lzlDPjB98rLm9xNu9BkLMxfAIRIR6E3IqFQDdIsemEUqgnWJ8I0UDwk00fSTrB3+z3JkbH17RiFCmuW8SIKOSV5+c9FoTkXA/ToZoRCmWSuDu/C6TEKPeYFL1W+a5IrQ23J5tFY84VhiC95qzg06zG8uDP738IhwbHzt9zRtXtxbKjZR95tD+jtIQclRzDkrssz2WV5a35Fn9Fj0ZqDNj5l5j8DQoWeiCt7IRDKxewhWrrVwV0AgW5mO6bKlftBJTrcEZpDoNvvX4gRSId0yv3fI5B/8e7OQH/D+vXFfq5z15CR95ldaZ+vzmn2hQw2ntHbPZSZtzOcdX2mzdOytW/hlua2/xkCBRolBGrsFxBodpb+NwhExUY7goq79XaX1hV16SAJFaQzVXSmlhaYworOiT0t3XuHHBzOXoxuptYcnEMgd647YgjyX4xAKBbPvEm8AdbjLITJSVlIb8nFyveHfrrL7q9sRm2MM2+C/eAqvH/E5M45hZuPAAXa9lXKoLKaKEYDbwGXj6QXJWvtYefiF0M3TCg1lNpg0NAGqrff2ZhiQn5XLauw70TcqZnvg31IApe4TzhXSuJFr5QSD2Shl4ZXwOtILa2zW+3t5e3PgJ7rh/pXpkolN2VS05Teptb0RhXwymtyCk0p39SJ16gBSf4niLdeWOOwIFxPwrcofbTBG7UrPrxNoeUZYGZ2RLrbA5XNoBRoZVWXo3wa5dDgbvFuGItSzkPgwInKTul+GLIZ2QRPoTV4GLUaQe9HiV8Kufgd4AWCkFo0X7DFneBeoQWFWnxbaHFktgW4VGwBvSIPQBFvwAehUuCRRxQjoMB++KeIcodEoYAC7BPaYC55oSb5kHgLVQQ1Yg2WFZPjnRn0l81qY4lkLJYcR/r9YeY64svEfnx6rUQzmsF7JDVGHXnU4NTtywwvirdOdjmFvatPiH8GZ1Cu9QBUIekeICDomZkR+r8BPkD0E4LUD6DKtleoQj6Ev0Ljo/aIzhMP4Iuz+HcwI5xweZB87wp2pdCckBlSOSdlt+8vfXWipD8EL1fKv5wVV+i3C/Vziv3wBV9TGvkEK3Tu3uT7y1Xrs1nUEzRVyvDh8UQ8nkiI9ylmPkQ2e5TwoLVIFLiyrHoTWiplzJSMmSfDiMLgCwetAaclm6aV9kKDJ9DdwNojcd5CFkXZfpDpiLttLtpupxjdvXpzjZ5rzyaawiju+X8xKzIk9DOXgxuRxMLIwCIUSDieFRi8+IUjHxlfEEoXQQ1Qnhs6Y1baCg3e84YGZWFspwWNbaXOHxvrPY5ngThNvAUegkphFv4oVBs3gF5wFNeBhTltFX/ev3Bh/8DAwA1tnV1tbV2dbXjVqIANfAv5JiqJTHwhxxeYDJ9RZgqMkmaUPPvEriuPJfbIPfd45HsSRzfubCCBbWyK1B3pPYL+p2RTYwex/4ZmrgSb0UxzxPcFD/g+8Q0Coz3eDls6/6asD9Xp+PaWHL0L9zXND9/+UGUjTJ/9J/ThFpjGy+7xXbseF+5uHtq5E8k4828zT4Et8EW8PpWon/nde98dIqlPf/eZ3+QsmQFb7n33XvQj8tpPiX8hXgUPCv76W+IPiJIHakIGviFQ/EBAROKvQQQM4d74spkOOP768GGJ/vZ8+htVOvzjPDr8F5FOzCyCuplfYDqN6FD3FUQW1jWJ+N8JFcSfEy3C9xrE9170/S+IbuG7EvG7C31/iugQ8ozbAI9mAx82oVCKYx0rhroC4K08coSk98lAxGiMBOCp8GVbNsYajh4pth27LZ8/fHerMJ+Ig0nkYMpI98hZcWdMJfV80pewUnY0TOvdh/P52461FY8cbYht3HKZ2P8ISMMXiLiIolLUYmgO8cGHM26ScUO8cSW6NcepaI+RidT7DBxK+Gi5zMLlg8FC0CKT0z6rKWTw1ocZo2dmCDDlYuNI1qqU75SrY3WhcpSBCmiNlEOpuFq2Q6my5ZY0FstWIU4dAUVRhiAlXFXEZ4U5C7ICvs2WIPkEVOZmLzIiG4Gi2WtkwvVefchs9WEhkATBPCcJwRl89RHG6DXPACsSYknOplLukKnjqVA5YkVCMNFyqC6mRoIprdkRJASDd6JCMwcBLZwhCbeMGeFWbIYm0St07Ni77767g+Q//emuXfDUjh34/NM4g5bCvPas2AG/ZoT2UgfYukPA1CsRJm9BM3+CeF/4fjO4jLhNwFg14nCCeJFQC/s0fwV2I65qvEIBhbdixO1k8QPYjXHc6NC1S+/wVHpkUbxtVZdz9h37IURcrhYi0AmiFpYE/t9F7wDzJ74p8CdMs0y/eR4viQfxHGqpFHkgipsYBF8DLyOZXxZW/cvEKwLuOxH9WUTXEqgUCbKII8qDMmiesC/O/wb2nKA4E8pmTtS49HrX9875NphZHouNp9PjsdjyzLzP2EvRyMQHwggE5oZLG8Qb8VyCuhqcNSeooBkxOoeFkMejfu9L/c6R5LmLDi1E3udAF9KcE/RTon8fJzA3RCceEk8ZTSi6eY1HjailrPIpQj5m5mbyTvgAmjWWyBCrzz35P//2rcUye6UJhw7l+ftx0jUoYT+OpFkui8+FwOuXPba1sXHrY5etf2xbU9O2x9Zffqiz69CGy2/r6LhtVOuyGxhlIBqyozqtHPLny45AilFbzMagFR4gPaX+SGlRir4BAIUy1H5t+pJjU5PHL8mk1xybmjq+Or2nuH35xPaG0vbly7cX/0RtDjgslM6g0dJGg34jXRfmkw5KpqjRaVW6Go3VrIiVOaO3YThT0kcjTuBucIeb/dgbamc+gH8BHyHq8a0AZAXZ/LM3/JCSFKVRpi+bPVhAa0y8JkBB36wtmNkLUSiqIt2hK79+1WCDNzrR3T4aTS1vpwNJ2mawqBlsWcZAOcvZT/5s5SXuRLpyQ6o/6kvnzE7K6MujQkCjUanDTbr0yIatG2N1w9l4ZzDU12DMW+tR2aBHFX7vkpHOCF1rCPvJjdvsakN7K5eyqwOd6Xhb3KHImZKMN2lTKk3OCIOj3MwVcEq4Uyg9HSQ3VDN+ETRY4XJnhgZlcvJAd7h3dfFLXd/q31n56NNjYCC3YbA8VnR8+tOhIQwQ8I2hIWw388xq8HuUzXQQSy528lKYVwdd4ARGKdVT5Fz9JOwt1OQn2ljyELQlWV9MH4tmh+vd5raOjjZz24ZS+fKy6laSibrYoD3DsX3NnKm1s9TuzK1fZAg6KKvX6ItpaO+DdGJhfa03RltsWjtTU+vk8rw1n9LJNLEsne/0caM9Wk/YbDfrnYxaxQQb45Z8QiPTsHW+VBcPVtY4HEabQeVkDXaDWjin/Sq5G35DWC9d/4XVMu8wDWt+/tWt72yYvrK+ftP0ZeunN5dK6H3DwdbWgxs2HGxuPnB170Q0MtHbscituAbavHq7UW3wMW67jNwtNyZ6F9RNHZ2cunN1Xd3qO6cmj07V7SlcPbF8W33h6uUTVxdSpalS6ZJy3cKIyhWg7JzVn3G6eEoebMu4xJznHngM/gPKFVxEmmhCCHjx82YkcsEgZmf4aS987QHRWTo3uxnCsmBg7R39/XesXXfnwMCd6/qnksnVCzatXy/fnmoORCIOpW5ULaPRQq+x6HVGue+e5r0rV+5tFv9dww21tAxxo1NTo0lgCsXsvJwkQ9ZMkjGNHSe1OpOOxJGLBD1kAh4RIpcZYZn09A+KeAyuonJKlqcnd7wB0panOzue2LJ45RDoAT2V53581VU/Bj0PPlh57tlnn35ayMDB1aAHPoRqMOfcqcI8vau3ef402hVxJesdke5IXa/P7PZYTB4PuJptS8VKfsrTlsu1uVdbHC6rxYXvD2KsPUI8KdZoJpQDUjXH4uAIxloBn48A/DwQT7xG/JlwHvKagM8oKyWeFvvg6B269SjJffqPwu5Y9Rel+Bsp/H7rrUdBrvJ3UAb8lV+Io1rBE6QBZ64mZY4poNSYKdC8EpUkJ++8RxZ6hifvvUNuOWmHPvbhuPz+++Xx6YCichaQojWs4M+E3p7P9EcWVRYKGUb4wrAFXpn70wckfvfJrbxTprDzdvl995L8MyHZPfBS9va4HE16/HBA0dFxEr0UgcMi5XZCWD0/gAYp94jjTD6jJBF7/gtvOP7acIN+q361PvuFFx3hwk2bzt64adOKz7/wKEpzF0zATwRpms6V5r9x//7X+mOG/fqthpbOSzu8aEX5S0FXwh5o4iMIm1u29Czc3qq8E2hdMZc7RHtL4ciS3ljHdrh/x46zph07NrpLQ2lvrGCn7EbGTdfUOqLt6fJoPLO6m3HFPHq9Q087zDUaV7w73zISQbLKZ34HT8BpQeoEkrrgU8rRi/nCpyS1BrDFUDloAJMG3xc/LwkmxsdvHh//1Rc8NomRRIUi5wMoApSIMjEu3R+evbCXYWYBRHq4kZzbHZwNBOJjFShhZfBS5PmCm6zaviBGV5Iv0M8Oj7Elb7jHpgTOHGV0uy3msNObSHqbr+zKL210yu+GuoCbDTF+lrGkR058Hfwm1RFRaxiNhbHW0m49lJEnlXJbovED+BLVVw4X3SEUGCijhaZNCT7SVFfXFCkORR35RXnal3JwXnsimW+LBPrKPKgbGqj8XbDOoGblNSq5MehQGUwarSVsSPUkGFAagkPYEpBQwE54u5Rb02L1FcRn6TioVm/kl8CblfCLq05VXn791Dq49vkfPjE+vnMc/VX++U3wVOWfgLcy9Ka4O74dfEPYYUDrHyGwAdekT++vfATu3g4eq0wcF06jw8Qn4EVwy2y1FwbHP0FlHe69H/W2S715wJCgAPaPg8bxscpfj4Em9LFpovI3oHF55W/weggTDeBF2Cs8neoQefHz6nJy3mc0xqv+eNzPxmKs+B5HYz7LB7lQiAvylUOhAP4UEB7+DBP3Ifn+RZTDhKtqUcrKRnAcTBw+/LJQm4aJITR6lyBrLoj+k6M22sqH4PjQiZWyiRMnv06c2yqIGtFyWg5exIxQQ+3Qya+fPDEhWyk8ezmBdF82W+WYJNuBqv0qG8DdgAGtlW+u3ru38taNN2LuSuI98HNwEudGQb+w0hmWE90VeSTKwVEtsN/S2deoNSrIlXZ1nqfbehq0BvTFIot/UKurtRnSC+MancZqTA6mBN03It1vm9NdiTwB6X7oEJAdPHjvW2/h2wSoEtahuKYUYoZwxk+efO3k9CrZBDCBPZUbPv5YuFmxEfwH1GOMxpFgdu+2eo9BvELE0J4HSY2dwZeap29RKZwxD8Naa1S24/DRylsGPmBh0otLL1R+dvonw2vjRtZ+tJ2Q4/1O8nH4LRQPvUQd0Up0XjAqnpPZgC/4XQ4y4IfFsTTb0B0sjWUaV8RNbpPJSxu9JrMPWi72y9mKsKsJyuElrcX+mCm8pK1vlL1G63G6/HrK43Syuorz4r91CY+YIUsijeRL5mm0GNU26/4PtPr/ozV48WK/fDr4v7LHNV9kKuzzBH6OC9/0BAnIsyhnwEdOwta0GzLgt/7ukl5GHUEVX7HNHxtq1ncbm4YS4AGo9zsqFvCvzqCJfBSaQl4U36D4fJQf9kH8RCcRxAcucvQf4oy50SjbB92Q5h2VNeCEM2JFPW0xbyJP+tmuc4YpG7rtmzpxboNYvi/KV8DPn6AKNoO40vg8C3Glzu9nKA8l/h3qfU7wrxWLKyBJ5+HPl47FNRxWFJ+xBfGVBQiUJ8h8whu3wkdJa9gFTlTWODna37nJjvnG/G1FWhhNX+oSqh/YBB6Ef4swBttvHkL65n0GfXw0yoej0cpHfCzGh2Ix2OTmOI+H49yz73g/FOaI1+FrAl7N631Voq4ukayrgzkEr3woGAgJbRuADP6VkGeyhcyjRxqfhQ3C7XkzDIAB+Cpa1QhvCgzNm7/0pS/BwJZcbktWlDhN/Az+SpR4/jjz895ruXJjiGsqB2r0Bo1GZ4DpeDQWjcZisXKttlZH1Wq1wlkGLIA1KJ+zYd0vgpc2YzwW1OgVZLdJGfUbo9GARi8nu3VkABbUWrVB7S8F1Bq1vsbbyInyD4B6+HVBM4SXZhCAA5cJe37mmTSIECo8cwxyBaFWVBYySmktcmaGyuSCKmsqXLNGEci6imMrn1qiphRovFq1DPaEKY8jHNdpZ+etCF853wrsvM+/t4dCdhfPv+AIhRzOUAjPWtDjCeJZE98FeVmghN+dZ++dO3ZAdms2uzUr3I2SPERBnCaIz8wcn2G24XvrDcITW7O/KIh3CbGnOJMK4j3puzhzCuJ94fus9RXEGel30XIK4gPxu2QvBfE7gvhPeQIBG3icpZRLTxNRFMf/M73FAgXrIwYNiwkxRKMWWkjlsRF8xaRYlQYWxsVAp6WmD2WKVRd+ANeuXbv2A7gwrly49DO4dOnS/z097bQUDdFMpvc3533PubcAZpz3iMExowC+8O2wg1v86rCLSSehHMOWc07Z9NnEKa8pj+CG81k5gQuuqzxKnlYex5KbU05ixW0oTzgz7gflSeRiX5VTmDXd2k5h2swqn8dFs95hB5gyZWUHSfNG2cWYeascw1nzTtlQrrmceLRHZ4S+H5VPIGU+KSeQMN+UR3HafFceo/yH8jj5p3KS/Et5Aom41u9Mkru5TpJTyinylPIZJOMebqKJZ3iFfVRRwR5a8JDFPN8sqUhJwHVd9Lu0rcGnnZXdpaxJ+wAhnuNAZA9E9pS8K5HWKG8xRpPykN+XJEOLGUOsYI5PhXGtxQF2kJYMdUp9iVujLqBnS7L7rHKOVocruXzs6paRY2Zb1228lKg+fa1XCY9oUaGdjbCPDGuZ55PBKvdQRJ761SPyDGe51ssynGNLdhMyRhMNVtGfpYIyrZtiGeIxpWlGSmOBT5q+AV5gCVdoZ2ezzO8Mf8t48t9VrQ302s682+0NmcOeaA4GNIen2JYnTX1AfYm1Vnv7DPvm+ie/+kCmaN6RZ1G09gzdkah2ipsk27W2zMxOuGNRk34EkjtgNR5jNqSHdn8tPdObuMe5eiiwnkDmEUXOD0S4SslRs7Ov11fZYN5uNT674ZPtdHb4azVtPfVWa/Ou4aFwi33xhroUMqrtiJ2f7WYo0dJyqyrUFxgh/49ex7s7f7s5xzl/BfG2Vj6tPKnlNeXbfLv/O9ukknQlypHvaSPakJlWGbfOGQb8rfbV0lmHq8zw1tgaMliUNavrgu5skbfLrtfVrrvj+9Ivez5rvwE4dgbdAAB4nGNgZgCD/3MYjBiwAAAqgwHRAAABAAH//wAPeJxVummMJVl2HpZ332Lf3v5yrawlq6t6n5meRWpPc/MIJkDYFmVbNiHTsGADAmT/0C9B/iHYkP/YoCQvMgzYpGlBNE2YnhlzhsNZe5qc3ruqt+paM7Nyz7evsd5wvKLg5eWNg/siMuJF3HvOd77v3FgDa2jNW/t/P7+8Btf4Gli7Xm17a79U2V9Z+zcr+5tr/0Fl/8O1v1fZf7L2Tyv7ydpnlb1X/YHqb23t5pqo7L9ebX+n2v5Btf3h2hr4j9bW0H+9tkb+s7W1l1G1tarro5+s/WTt///5yz3/X/vsrqrr6hJARCjjQirDtDw/ePY71W/GaVbo6qC5duXZDjiZzhZxkmZ5oSHC/885jludFEa1eqPZXt9Z+7eqf4arExKYj/Km1hqWACxhBnOoIYQ+WqIMaVQihAyc4AIDDElMECH0jCa0oJoKxviML3jBNS855IQbPBBTMRcLUR0UQkh5LBNJZF0N1EQtVaGgkRjQ8MzEBFZspVZulVZg547lPnKXrnZLF3ljb+ot/cLXwTjg4VkII1rDrcV6tmFsiuvTWz1g/cu7h9//8v32HC3wchTHMU9YOk2TVKc0QxnJWMZzlOMCFqiw9FLHOtWZLjXUWEttlNWglgjkAAAIaPXkxbMnJ5BCBS3owwCGsFWNQlGNAkAYUcQQRwoZyEMh2kV/DedY4xJDjDDGBAssscIGNrGFI3wV/yrJSEE0KQmsxg0TQjgRxCAOCUhI9mhMU4oopoQqalGH+rRGW3SXJSxjmkGGGGGSKeaxgIVsh494zPNno4045ZK7POJd/jx/nf9mNeZLkYtClAIKJLAgQglb+KIltsSOXMhUZrKQpYQSSVzNCJWmtKQnQ/m8mlczo1WpgEIKK6qksoyZsTS0Aaq5IoY0lDk15+bSzMzSBCY1uanMHWtiLaoZLC1gQQtbxGIWt0zLsbatr9iZjW1hb9m7zsjRDnKkYzuOO3QnLnala7vX3Ve9gTf3Fp72pPea3/fH/tTXvu07/l8LpkEcJAGsZj8O7fBq+Gp0Fl1ERVRGuHZeS+qX9Vk9r9O60Thv6KbdHrXzNutk3Wwr3E62i6vZtdH15dr0mZdUIfE7/94//lv/9Df/29/87/7Gf/83fv9X/rdv/dHr/9e/+r1f/f7X//SX/+yrP7r501d/9o03b/38a7+48osv/+Kr77zw7ua719+99f71D+AHe3dqd974ZOfz7Xub93cfbj3afrzxJH/i70f7zQP/0D/sPm09XT+uHTdPaif1U+s0Or1yZp5FZ/Xz2vnW+XMX5EJeBBfRpXnpX4Y9rxf0Wr2v9mnf7Xt9vx/0awM2kANrYA+cgTe0h87QHXrDxoiOxMgcOSNvFI3WRy+M2dgee+PtiZhYE3fiT+qT7Sme0qmcmlNr2pzxmZwZ83Iu5mqBFnIRLTaXbMmXYqniIgYxinFMYxbzWCZJAhKW8MRMcUpTUTkGeBYttIoXkanMynZykMMc5zRXuZlb+VZRVBFEC1bwYkvPdaJzXawQQ6Mqkoim2tLdVSyVFKAqhkAVRQgKKKEJnSp6NIKI4+xZtFQoUkWKrGIkwG288SwmHBI9i4WyigWXtir/J0xVvv6X3szlrPJbS9qrwDNMs6g8zrTHlV8VDgsWQRHm9Ytmr7O0/3413+gvQfAfvf23/9FHf/ufPPib/827v/1HD775x/Ff/Xb6je9/940f4pd/NP3aT/7w134av/zmt7/65re//ua3f+PN73z9zfTFN+Wtn09ffeuP3njrzq//xXdefx9f+/izX/lYbn16//Uvys7j+39lP2weRI3Tw+bFvzu57PmX3yx6sdsPzX5zfTAIBr81Hc3tceXHY3vyOZkctiaT9cl1ND3anZ6as4/57OHO7OjKrC9nM2M2e352Hcyu0/mbt+ZHm/NBYz605yM1H6v5xJ7P1Tz15lvD+Q2y+PFLiyetxX5zcfDi4vj64qy+6DUW/eZi0FiMvrEY08WEL2ZkEcNFphZQLpS1iOhiDy/v7ywfN5dPouV+Yzn0lsP6cnxtWYX1nCyrgK9cY+kvE7bMzWXuLom7vNKPP78a79fj/UZ81oj7ZTx04kUZL3i8NOLldlx5UOVA8W6csLhQsTZiiuOtXvLTF5L9erK/m5x0ktNO0m8mwygZ5ckoTMZBMnGSaZ7MUDJXyTxK4jyJSZKgJKksSzKWFEVSgOR6nuyB9EdfSX/8Snq3m97dSu/fSO+/kD5qpUe1dPhKOrbSmZlWPls5bRqmaSdNn08zmeZFqueppqlWKb2W8sqly1QZaROme5PsJ89nb76cvfVSdhhmT8Ps0s96jWwQZkOVDa1spLJxPZt0sznOFiJbhNkyzJI4S1SWhFnqZGmQVVGR8SyrZwXKNMg0zjTJNMtAkUEnQyRDRoaijHoZr6LHyFQns3Dmhpnbyrx2tlfmP3g1/9mt/K0X8z/fzt+/kX+8lX+ymR+28mM7Pw7y40Z+2sxP1/NzlJ+Hed/PB2E+qOdDLx/6+TDKxzgfh/nEySedfOrmM57PZT5380WZL2i+EPlC5QsrXwR5TPNE5InKU5xXmTWt5ZnK8yKvQjnneS7yvJbnV/KC5RrlGuea5kDlkOewkeMqUVYBSnOCc0Jzhqtkkltufj3Jb6Pih88X798q7naKj68Vn94qPn2++Gyj+Gyz+Gyr+OzV4vMXi+NGcdYsLv2iL4t+ZcNi6BcjVYysYmwU41oxfrWYqGKaFnO3WKhi0SiWRpGYRQqLVBWpX1SpssptFc4UuChIUUFNYRRFs9Ck0GYBQAF4AUQBYUFYQXkhYXE9LvYK/aOv6J+8pP/8Of3Zjr7f0U+29UFNH9T1QVsfbOqD6uuX9NO6ftrQRzV9HOrjSJ8wfRLqkyv6QuvLSPeQ7gW619R9X/dDPSB64Oih1sNAj2w9cvUo0mNXjz09cfTE1dPHeir0zNJzoedKz029YHpJdCx17OkE6kTpxNBJoFOhU6lTT6e+zojOPJ35Oq/ws9QVzarwsEh0UeqC6ULoClcrerJCVrDC1gpaK2ytwFVzraujni4rxsJ0KVbHQakB0ABrwDRQGpIKZDWqoBavzsNMY1NjT7NSC6wNrt0rei8vv3O1/NkL5dtfLu8+Kg+j8qhZnvrlab082y0v/PIyKi/bZc8r+27Z98uBUw465dAuR1VzyrEox2459supLKf1cmaWy6p5ZczKuAq5uEyzMnXLtFlmpFx5py5zVuZOmXfKfKcs2hXxKst49RCrDyhLo2KcJcAlrFJHWmWPEpslWWWBksVldwH+q78Lfv9r4J/PwU9eBD+/Bj5sgI8egI9ycGcGPt8EDzvgUQs8aYF9H+w3waEPDhvgqAOOQ3DigVMDnHdAzwf9OhhaYOiAoQuGXTAKwNgEYwuMbTB2wDgCExNMXDBpgOkDMONgZoCZDWYumFMwZ2BBwcIGiwAsc7DkYOmBZRfEJYglSNdBugUyDjIP5FXzQd6q3BRoBKo8pyUoqy8UAA5gBlACUA5QCZAJcBsQCogERAHiALIJaAFoCSgBDAJWA8IDogmkBCoC1SBZAHRmoLMA14/B9RnYS8AbB/Af/ifwd34L/uN/H/6P34L/878C/5fX4R98E/7JTfi9l+APXoM/+Cb82TZ88yr8+Wvw56/Dt/fgO234zlX4wU34wYvwoxR+vAfvdeC92/BJHR6cw4MAHnTh0w488uCRD48CeOzAYx+eSHgSwdMAntbhWQDPHXheg+cNeN6CFxxe+PAihJch7Bmw58G+DwcmHDhwUHVuwqELh3U43IIjC45COKrBUR2OMzgWcKzg2IRjC45tOI7geANOGJwYcGLCiQUnNpw4cErhVMCpglMTziicSThz4RzCuYBzCecGnFtw7sAFhgsCFxQuOFxIuIRwKeBSwVjDGMIYw1jC2ISxD+MAxk0YPw8TDZMSJhAmGCYEJgwmCiYerBhnOoVpCVMMUwpTB6YeTOtwlUteg5mGWQkzBDMCMxfmFUXSFUuCxRIWeSUxYMFhpW8KqwKmlZDQFGoOtYJlDp+5fiWwYEkrZ4crwYEhWIer56nuI4WV6lqds2JOEOKVCKlUCGQQclgRqYpJwSaEL0L4MkQConWIlxDHEOewUjdYwyqGMIKYQexDkldgAEkJCYV0DimAFENaqRoBK0/kGKopVJvQwNAm0GlBF0DXhv4mbKTov/x19H9+A33/V9Cf3UY/dNEPa+itr6I//wZ6ewe9fR29B9F7HH20gT66ij7toE/X0b119CBAD+rowRZ62EBPWmg/RAdNdNBGB1vo0EeHTXTYQk8JelpHTxvo1EanHXQm0bmPzkN0AdCFjy4idInQpYt6EeoHqF/ZOhpANHDRwEMDHw0RGrpo7KNxgMY1NN5AEwNNTDSx0KSNpiGaGWhmoZmHZg00L9Cco7lCcxPNbTT30LyO5k20KNGCoYWo6DFauGgRosUuWhZoqdGyREuBlhItbbRsoVijGKGYopij2ERJgRKGkqpjoxSiSrKlCqUByqYoK1HGUNZZRfYqgzKUOyjfqigvKjAqOCoEKkykS6QBWqEzRloibaxgoCSo4uWgQCsNWtHmHFWkuRLdld5cnV/1CaoUZyU3kY0qwYmXFbQjXFY6FJEUkQKREpFKP+pKQiIqEW0jViDGEZOI+0jkSCAkKTIKZPaROUEmRg5BfopqIar30d4S/9E38Z/+VfzmTfyOid+x8Ts38bsd/N5z+IPn8N1tfPcm/ryG7zXwgw38uIEfb+EnVXsO7zfxoYcPW/iwi48cfPQGPvbxcYBPDHzq4jMfn3N8buJzD5/7+CLAFzV8qfClh3s13Gvj3jruE9znuO/joYuHAR6GeHgTD38Jj2w8cvCY4rGPxxEev4QnMZ5QPGF4auGpg2cGnpl4ZuGZjecmnrt4QfHCwAsTLwleUry08dLBywDHCMcGji0cezj2ccJx4uEkwGmBU43TEqcApwinFk43cIZwxnFu4xX5KHEBcEFwIXGxucqlVXhVSbRMcJWgQIFBZR28wo4KJqr415WiwZBgKDCs9l/Bq2kGq5JApfnROsYLjGOM0xW9wgXGJX5WLsCYYswxFhjbGHsYh5goTCJMLUw3VqHMEOYMcxOLEguIBcJyjtUYqzY2cmxUGb5KmB1sIexQHKW4GZN/8avkh98kP75J3vwGefc58uEOubtDPr5CHq+T/S457JDDLjnS5NghJyY5ccgZJ2cNcrZJziU5j8j5Bjl/nlx65LJJejbpV61NBjYZuGTQIENKhgEZUTJyyMgnY5OMbTIOycQhU0WmNpk6ZGaQOSVzm8ybZMHIUpClJEtFliZZuiQGJGYk5iS2SYJJpfMSk6Rg5c6pIukWyXKSlSQvSE5JrsiKFFb8j5KiRlb8qSDPiA7RmGhJtLWKgJIRUBBQEgAIkGSFzpqsALTqBwSGBM0ISkmlcFFBECUrR9EEl4TEhCSEZITkhBSEVHsAWVVp6EqTEoMQm5CIkA6hc0Ir1hkTllfckzBGeEJ4RkRGVLnK4YYmJiGOSVyT+CW5PqL/0y/Rf/41+oM9+sFNevd5+vFVeu/L9H6XPrxGH9Xpo+fo44g+rtMnTbpfp/sd+nSHHhv0eIue2PQkoKc2PW3RM0nPDHoW0bMX6LmkPYv2XTrw6CCgQ4MOX6cjQUcWHXl0nNIxpGNOxyYdW3Rs03GTTiCdMDox6MSiU4dOfTojdMbozKQzj85qdJ7TOaBzm84dOu/SBaILTheSLgy6sOiiXolymmiaIJpCmlaW0mf6imYpzQDNEM0IzSjNqj0uzQuaI/pMQtDcp3lIi3LFawpYDR4tFC08qkuqIdWcaoOWJQWrggEFmAJKgaDAojCmq9SbU4jpKoExCqudLoU+XSFvQVeQ59FV2OcVT6R4gxKT0imlC0qXdHXH1V0WdHVFQimjVFGWUL6gnFJuUpFS4VFZUgWpMqnapQai9pg6iDqUOnXqG7QG2e/eZr/3r7G3OuyTFnvgsyc1tt9iB3X2tMOOEDuS7MRiZw67cFjPZH3G+pL1r7EBYAPIBiYbOGxosFHCRpU12WidjSmbMDZRbGKwickm19g0ZVPFpgabBmxWshlgM8Rmis0TNs/ZnLO5YgvIFpwtqo7PVlqdsqVXUWMWK5YwlgiW1FiKWEpZGrGVKEUsIywzWA5YDlmOWe6w3GWFZkW5Sg6FYIXNii7TGVsph0onEKYF0zbTPisBKyErESsxKwkDKQM5A5oBxmDGYM6gXvFTiNgqIxYMlQxV/QqoGEMOQw2G00o4MgwYbjEyYqsIzxjRjEhGLba66epamDGbsYCxa4z3GeeMO0wkTFAmFFOCGQmzMLNtFnj8n/07/J/9Nv/jX+c//Tr/6V/hb77K37H4OzZ/7yb/cIPf2eF3rvJPdvn9Ln/g8IdN/ijgjxP+2OdPNvh+yvcjvl/n+22+v80Ptvkh5E9tfiT4kcmPJT9u8ZMmPzX5acDPavzc4Oc2vzD4ZcYvbX7p8Z7BezbvhXxI+NDiI85HBh/ZfKz4RPCJyaeKTxt8VvIZ4jPMZ4TPOJ9JPrP4zOcLxBecLwFfMh5jHleW81jx2OWJ5km5QpHKNxPGE8XTnKcFTwXPAM8QzwyeOTxzeQ54jnnu8LzDi4wXiBeCFy2uC64h15iv1J3JtcNLwEvCS8pLm4OYr5RJyVfyQ3FgcpjzFUMFHGIOfQ5DDhscXuUrlpNypDkiHKf8WSLkuKx4IseKE8AJ4sTlK2iogj/jVHFq8NVzVg+TcZ5zXnAOOSecKy6WXGpeERBjwU3BHYd7G9wveDWVkcd3z/j1Pt+biN/7lnjz6+IXV8QHe+JuV9xdF5/siM+2xOcdcW9bfNEUDzbEw23xaFs8DsTjmnjiin1P7F8VT11x7IoTS5wqcWqKMyHOAnG2J86puIjEZSD6SvRd0a+L/pbo74qBKQaeGCoxNMUoEuNcjJUY22JCxMQQ01JMsZhSMZViqsSMiLkp5l8XCyYWXCykWLhimYhlKpaZWJZiycTSFktPxFrEXZFokeCV5yZcJEokmyt0SYVIfZHlIhMiUyLzRQ5EDkVORC5EvicKKAomilAUkShqougKPRN6Wal6oaHQSGgsNBNlKcrKSgGmAiwEKATQApQCYAGYWImcUqy0ARErZi8E9AS8JZAWCFRMQSAqEBfoisCpwLlYMRggVhylIhxUYCawEnhT4B2BrwmSCSIEqQlahd9IrJCFCxYJVhM8FGIuxFKIUqx+BonVwo4pxIaQUqiZUEuhqh1KmB1hKWGZwitFxEQtFfVEdAxxvSe+MZV//IX8zmvyezvyx6b88br8i6vy7WvynevynS/L927I9/bkR115J5J3NuWdK/LjTfnprvwilPfb8sGpfHApH7wsH5nykSUfteVjTz4O5ONQHkTyoCYPOvIwlYeePIzk01AemfKoKY/W5YkjT4E8o/IskuczeW7Ic1OeO/JSyX4gB0wODDmkcoTlyJWjrhwrObbk2JcTKSeWnDI5teUMypkp50rOK9uSCyAXWC64XAi5kHKJ5FLIOJWxlnEpYyRjIhMg04VMoUyRTIXMkMywzIjMqr6SmSHzUuZS5q/LAsrClIUlC09qIHUkdU3q52QJZNmUAMhVjYBKYMiVssUS5RIVEhkSc4mVXFGrVK6YUMV7DEkiSeqSMskzybXkpeSuFKFc3WgsFZdGIQ0gLS1tKL2R9J/KIJNhV33nlvrhDfWzV9QvttX7VXtRfdBVHzbUZxvqsxvqQUc9bKpHkXrcVU88deCpw0A9tdSJq048dRKo00Kdeuo0VKctdWGpC0dd+uqyqXqG6lfNV4MraijUiKuRUGOlxoYa22ocqolUU6CmWE2JmnI1rbyJqWWsYqhirGKiYq7ipkpKlUCVSJUClWKVCpUaKvXVqkqrVQZUhtSqjolVbqq8oQqgCqgKoQpX6VJpqDRVmiltKm2rMldloQBQgClgKDhQMFYwUzBXECnI1Uo65goVCpUKAYWgQlzhucKJwoXCWpFMEVvRqaKlokBRqCiqSIZapfNEsUIxqBhVz5KZWqWKKkkUFZIq7qvVtEVKzZXZV+ZUmVUnUyZQJlPWpbISZc+VLZQzU06ooomqVU2opqP2hsaPfOMnm8ZPrxo/f8l4e8v4yDY+RsbH28YnrxufrRufbRn3XjXud42HTePhhrFfGgeRcWQZR45xbBsnpnFSN04N4wIYF9S4sIxLblxK47JhXHaNvmMMpDFQxsAwhsoYVrZljBJjJIxxYYyVMd4wxleMCTMm3JhSY0aMGTdmpjFrGLOmMUfGPDIWyFgwYyGMhTIWlrFwjKUylp6xjIxlzYhTIy6MODDia0aSGAkxEtNILCPNjVQbKTFSZqTCyICRQSOTRtY0csPIO0b+ZWNVKS6NAlZC1ii4UShDjw2NDG0Y2ja0a+jQKCdGmRslMEBsgMxY1ZK0AaEBK35XGAgaODewNnBpYN/AdYMcGWRokNgg1CDMIMIgmwYFBhUG6xusNBgwGDdWDKDKdswQc0NQQx4bcmTIWRXDxsoLM8OYGsbMsGaGPTdsbXgLo0aNTmx0lHGlaVydGNfPzd//ZfNPt8yPts07e+bdXfPjG+Ynkflpzfz0ZfPelnn/NfPRhvlEmE988yAyD+vmUd889s0TYZ6E5knDPL1mnhnmWd083zMvuHnRMS+FeWmZPbKS9ZUf9ZvmQJoD0xwuzaEwh1XHNoeRORLmWJiThTlh5sQ1p6k5ZeZUmLOlOZPmzDfnzJzLlQ/OTXPumPMXzEVhLohZpe6FMBfKXLjmwjdXS0TaXO6ZMTBjZMaBmQAzwWbCzCQ009hMczMtzZSYKTUzbWbAzEwzh2bOzNw2C2IWwiwsU89MnZi6NDUwNTJXFWpiamqW0CwNs/TMsmmC0gTQBMhcVf0SE1om7JpoaqLSRKaJ3Ernmzg2cWliahJgkorT04rWm9QwV1x8YTJqsqsmvzT52FwRI9dcYXdqSsdUxFQNc+W4c9Ngprkw7cJ0HNMJTI+YnQuzI81rY+sPnrO+/ar15+vWux9Y72bWR6H1+U3r8y9Z9yLrXtN6XFhPHOuwbj11rCNgHVW2bh3PrWPLOg6tE8M6Ca1TxzprWedz65xa5551Hlnn37QuTOvCtS4869K3etLq2Vbfsvp1a4CtgbAGjjW0rVFmjZQ1sq0xtcbCGhvW2LQm2poAa8KsCbemwpo2rZm2ZpY186xZYM1PrTm3Fpm1oNaCWQtprZYADWtZWEtgxbGVLKyEWIm00tJKsZVSK61bacPKoJURK/uWtVorAlYOrZxauWnljlUUVgGsAlkFswrb0tDSxNLU0sIqM6ssrRJYJbFKxwKZBQoLAGtVjV1aUFvQt9DQQrmFCguVFnIt7FjYt8jYIgtrJdqBRYRFDIusWzS2aGFRZTFiMdvifYtnFoeWOLeEtgSwhGdJYFkTyyosO7ds03Iyy51Zbm55wgrmVqitMLSioVWbWfWh1ZharadWe2x1h9aOaX/3efud9+13kP3eVftDan/8nH2vY9+7Zt9/0X54xX7csB937CPTPrLsI8c+Me0zaZ8p+8yxLwL7omVfNuy+a/e37AG1h9we2vaY2WNhj6U9NuwJsifcnip7GtizE3tW2jNpV0gwa9jzhT1P7Dmy58SeM3uR2svEXgo7Lu0ksZPUTqCdBHa6sFNgp8hOqZ0KO7XtLLGz0s6InUV2Hts5tnNq58IugF1gu7BtPbV1bOvC1tAuE7ss7RLYpW2Xjg3mNtA2KG1g27Bnw8yGukr19qpwWdgI2TixcWav6k11G9+wyZlNEptkNlE2Xdg0sym3qbTpLZslNmvbKzlU2FxXcsLmyOa+LVNb5rbktlT2KovPq4RqK2AbU9sc22ZlZ7aZ2pa0Lcu2e9V42U5iO9p2HNsXdsjszsDu9u11z967dL57y3lrw3mv6bzfce5cde4y5wvP+WLPeXDdebjjPHzReWQ7T247+3Xnacc5qjlHDef0unMWOheec+k7vcgZUGdoO6PSGTFnzJ2x40y0MwmdKXMWibOwnDh2YsOJHSdJnAQ5CXNS7aTQSZGTYidlTlY6GXNy4OTQybGTKyf3nGLiFMwplKO1o0unBA5IHFA6wHJg5sDCgdqB3IGhg/rOqi65cDBxsOmQM4cMHZI6hDmk7dDYoYlDU4eGDkscVim6kcPHzkqtSYdvOqIiqX1HThyVOebAMVPHhI6lHfvYqWbUGTlO4Tie4yMnSJ0wdWozp1Y4DeG0xs76yLl+4X6v6b51y333OffTlvv4ivuk7p7Y7hl3z5F7Tt1z5p5vuOdX3Yume3HT7TG3J9w+cvvUHSTuoHAHyh1G7th3J6Y7sd1J4E61OzXdGXNngTtrunPlLkx3idyl5cbajZEbSzduuPHX3GTkJpmbaDcp3YS7iXBT7WZLNwvcrOWu1qrnbl64OXJz4hbALbhbfMvV3NWGW87dErmgcIF0n62muFC7aOki28U9d1UXHrsYuLj66rhk7JLYJalLmEtn7qrYBFyWugy6rO1y5ArtyoErh66sOspVW64xd1fMALjGVdcculbVkgqJXPvIdaDrnbphz41it3HsNidui7sbQ3fv3PuTV7w7294n696nLe/zuvfE9PavewfUe3rDO2p6x653fNM7ueKdmt6p712se5fC6/leH3j9ujfwvaHrjXJvVFbk2htX7YY3KbxJ05spb96qqIG3hN6SeEvqxVMvtryEeWnmpZGXAS8jXia8nHr5dS//ildAr2h6OvFWi7impwNP17wSeKXlAeYB6QHTA1seHHiQeFB50PJgx0NTD8UeMrxVtZ16ZOmR3KNLjzoeO/VY7jHo8TOP9zwOPM49cexJ5EnmSd9T0DMnnpl6lYCzY8/peY7wvIHnAc+jXjT36j2vcek1et7mU+/2kf+9v+V/2vQ/ve5/ft2/t+U/eMV/eMV/+KL/8GX/gPqHyj9p+KeOf8b9C98fUH+o/EniTyJ/mvpTw58jf479BfWT1E8KP+F+4vpp7qfYz5Z+5vhZ6Gc3/Zz6Offzm/7qFQHtF8AvoK+FX8Z+Wfgl8kvfB7G/WnzVPqj66z4c+nDiQ9NHfR9lPqI+8n1c8+ncp8Sn1Gdjn2Gf2T5PfE587vri3BcTX1ZaZeiriW8c+EbqG5lvGL6JfSf3ncL3Dv2g54d9P0R+NPbrZ3595LdGfkf7O4f+7afBd28F3w+DD5rBR7eDTzvBPRY8jILHV4J9PzgWwYkbnBvBeRRcqKAXBr0rwWArGJXByA3GZTBmwZgH02Uw6wTzRTA3q8wWLJxgOQqWKlg2gxgGSREkZZDYQboIchjkZlCQoLADnQTlk6CkAZgGoAgACGAvgPMA5gEsAlgGEAYQBZAFkAerBbZlgLIARQHuBWQSEBisCsrTgA0DBgOGAmYEvBeIqvUDAQOhAlUERh7YZ4GzDLw0aBwE6/3gynn40c3wCxk+WA8f7oaP1sPHO+GTjfDACw+Pw6NaePIwPHXCMy88b4XnnfAShpcyvPTC3k7Yx2G/EQ6icLAeDlE41uHYDSdlOLHD6SScknDqhfM8jNMw1mFsh7EXpkmYkjA1w9QKMxxmRpijMKdhLsLcDYs81ItQ22GZhyUPQR4C+mxlehxCHUISQhGik5AchqQfkjwkIKSDcFWkRiEbhCwOGQ+ZGbJGyGch16HwQwlDJUJjPzROQyMLrST0nDA8C8NZGMqwsR/uHUef1KMH7ehxFD1uR0dOdPogOu1GZyo6p9HFc9HlUdS7jAYsGuxEgxvRCEUjFo3MaI6jhYzi3Si1omwWZcsoS6Msj/JxlGdRXnVklDtRkUWFFRUvRDCL0CBCLEKdCOuI9KLVyoQd0aOInUesF7F+xGDESMSPIn4W8UnEQSSKSGaRzCNpRcbTyEgjA0XWNPJGUTiKoidR7TBqnERNEXWOan9yo/a99dqfbtXe/HLtnWbtU177bKP2tF47hbVzVuuJWs+vDcza0K2NQG2c1Sa3atO0NlvUZvXaEtSWuBbDWoxqsaplupbBWhHVdFrTRk1btbJZA+MasGowrkFdQ0ZttRY4qOFhDeMaPanR8xqLa9yoif2ayGrCq4l6TZ7UJKzJZk0d1dSkZpxUMrJmT2vOcS38pBZe1sJxrTWrdU9q3dPa1kXtylnt+uPa3tP6Pb/+oF5/cKN+YNTPr9Qvvfplu94D9V5Y723W+3Z94NSHRn1o1ke4Ps/qMavHfj2F9dVrXk4926jnul6EdT2ra1jXql6Wddirw0F99ULDog7zOhR17NVJVidFnV7WWV4Xy7oUdfW0bs3r1rJun9ftpO5e1N1J3TXq0aJez+qt43r7rN6+qLcn9fVH9Y1JffuovtOv7x3WX/mi8YtXGh+1Go/DxkG9cdhpHG81LmWjv90YHDYGTmPIGiOjMVkx3MbUacyTxrzdWAaNRDeSqJHqRuo00rCR8UYmGzlsFHGjKBv6qFEWjVI0QL0B0wbMGihq4FqDFA0CG5Q2WL/BYIPrBi8bgjSE3ZCThowaK+WpGxWXDY8a4bAR5o3WvUbrSaN12uh+2uieNLYeNq49atzuNb503LznNh8ZzX3WPPaaF15zQJsD2RzbzbHfHK83p0ZzajZnk+Y8bC5qzRg3E9BM02ZaWdXMrGaRNXXe1GGz9Jq438Syia0mEU36qMniJk+a8qypDpqqaJr7TSttOkdNJ2l6T5pe0oz2m1ceNHdPm3v7zS8dtX631/r2ldZ7Tuvu1dZ9p/UAtR7B1iPZ6lVQHbTGx60pas3s1iJvLYLWst9aotaStlLYynQrs1uZ18o2Wjls6bSls5aOWrrdAvMWyFoAtpDVwrJFYYvhlkhbQreMs5YRt8xey2y27Kct97hVO241Dlu791u7x+0/a7R/rNp37PZDr/3Ibz9N2z2z3eu0B1F76LZHqD0r2nOnvcDtmLVT3c7Tdk7aetDWtA1QG8ZtaLQxbvOkLauWttWTtkrbRt62SNs+aNuHbfu47ah28Gk7mLXDSTuctdtn7c7j9tZ+++annR+pzttm57NG52mz0+t2+rLTb3VGvDOxOrNHnWXSWYpOrDqJ00lhJ9vt5GWnyDsl7JR+B/U6yOvgoEOSDj3p0LOO/KIjH3bkk45RdKyDjlWR8oOOe7/jzjr+ZcfvdWqzztUnnVced778SeeNT7oPm90nte6TTne/1d2/0T3R3TPePZ90z0m397jbp92+6vaN7lB2Z/3ugnQXtLuE3aXoZrKbH3Zz2M1xt+DdotXVQVc3u2XehbKLL7rY7bKkK0+65n7XTLrWZdcBXe9Bt3ne3X3Y3XvY/dLH6/vp+rC+PlLrSWs9v1gH6Tr015G5TuJ1+nCdJ+viYF3M1kW5Lsfran9dTdbNwbozWHcfrnvZ+ut3Nx5YGxdVizZ69kZvc6Nvb4x2NsbzjcLb0GCjbGzg5QY93GDJBrc25NmGHG4YhxtGf8O8v2GaG9Zow3664Y42/NONINmI3t5o3dnYONnYOdp4/s7GG3c2f8fZ/N9vbJ6Vm6nYTP3NnG+C5SbQm2i5SYab1uGm/3Qz2N8MDjfDp5vRF5vrB5tXf7B58+HmGx9t/Qu+9Yd0611j69DdOg62jttb53JryLYmza0MbYFoCxlbaGuLDbccuhXorRc/3P6Tne3RdHvKtsFyG/a3yWib+dv8ybbg23K+vfvWzu/8+s6br+289dWd9/nOh+2dntoZjnfGaCeBO96jne7bOxvDK2999cqZfWUxvoJnV+j0in9wJepf6VzuftjYvQ92L7zdi8buFO6ii12U7opi1xrtOtmu98WuN9gNH+xGn+02DnYbR7ubH+5ef/fqR/Dq7OOrN7579Y13rp3Vr122runTa6h3jaTXvNG1YP/6Z+y6qa+H+Prenesv/eIGOLqx/sFeUeyJh3vi0c36w5ubD29u/x83r7518+r7z3X3n+vqWz3/VnB6q/bF7bvx7WN4+2L/9uDR7fGj2/KL2/LstuzfNt993hDPb//hC3eXL+DPX7A/fqH96IXu4xe/g19MH7yoixejOy8t/uIl9fAldfEy+eRl8fbL7vjl6JNX8u+9anz/1Td+8KXf+oOvmD99jX/2Tf7WL7/7v/7a6Ge/NvroN+7+8K/v/d4aWgPPtrU1WP7dtX9Q/j3wG+V/vPFba6za+y9fTV9bK//62v+wRsp/Y+1318LK/hdr/+na1fLvrP39yv722j9ce638m2v/+do3yn/7/wZH1ANPAAAAeJzEvQlUVVeaL77vwOUyG8sYYyzL2DZtW5Zt25Zl2bbPsmzLZ1m2ZSyeZVu2bVuWrYhMIiAgAiKTyCQgw2U2Sgwao0SRICGIQxljDDGKqEgQEREBEZHhfu+3v329SLrWe73Wf/3Xu1lnc+4+++z9zdPe1wiNEMJeTBIzhHbhoqUrhbP7v/t6CGehR78gEvK5ZssfvT2EUd7xpRdafNMK+5+HCI1msliI3v3aEdoKrbe2EG2Ntk7botPqFulW6y7qbuju6lfo3fVtNgabUpsWwxpDvW2obbxtiW2vcZbdZntX+1X2MQ6NDj0OZscap1VOZc5hzvnOvS7LXNa4eLqEuBS6nHC569I2YsGIqjcc3xj7xvw3Qt44/sblNwZG5o7Sjzo5qmpU45tr3ox/s/LN2tHTRweOjhqdOvr46MtvTX0r462SMSfGXB/TMKb97VFvT3vb/e2kty+PnTG2/R23dw6Ncx23YVz+D1f88PD4iePdx7f9aNmP2iaMn1A+ofvduokRE9MnVvxV+qSVf33Ndbmrn2u+a9ffOP6N6+TKybV/O/JvZ08ZOWXhlEM/jptqmHr1J1d+0jxt5LSFfyemT5zu//f2f7/674tnLJ6R/g+rZx76aehP+2bF/Wzg5yNBLVdQzgaUsgW9bYQj/rMDnV3w7Q385yh+IN4UTuIt8bYYId4R4/H9Xfz3Ft5zFWPE34q/w5MZ+G+CmCnm4MlcMU9MFr8QvxI/Fv9TLMGTpeJf8Oy3wk3MFqvEv+L5H/DfAvFvYp34pdgg/ij+WfxJuOONbfjvN8JL+Ihlwl+E451UkY6xmSILY02iQKwXh0Wx2CQ+FMfEFvGROC08RJn4TPiJz8UFESKuii/x3i1xW+wRd8R9ESUeiGaxD3iMorpLkcBFft6lOnuT+DHdaV8q3iPz49HCn06LahFAxYA4mHrEcrGL2gFxGLWfnyTCqUmsETV0/3agMDg3hwphMITI1uaKbO1Xo9XL+cRYMY3el7SgfNDnZ5QnpuLJoBzldEy2jpu4rUKrwxNjnpt878lpvJEjRgmDw+hQKcuyzwTuAFK7FobOGStv4Gde1A85N2j/wHMojIKFw810EYZ+zCH85BqgvwAMvxXi6wb5HJDswowzMMoWnDgMbs8TBn2OHKtbJVt9Kt97ydaYjFYLWrzEymE0cKYLz/ejz0HY00PollrXlTpAsR+jx0+8B7yXYyVRMBrUM4++gvcGQcNwegHqHabn4F4NPQb3DLZdTLdbDOdeXvUfmZ7hsrW9yxQeb6HEQ4mt4wr+9rZw6OgDN1+t/vAQJAvzL8Hqf43Vf8/97ljLDbD3Y91d1HU2V/IRq4dTH1Y/TJ2QmhpqGXNCGDT/k2FYxDDUMRWqGAZP7nnIcP5wGKW1U5tAPVtJafvjePIm2x8BOsmPGvWOkjNuf8LtNG7/jtufcjuL259xu4zbf+X2P7n14TaL22xuc7k9wu15bi9wq4Ue/gSrjwWGgEmzizGagtYFT6aCBmPRrmcINkET9cD+p8DHF2trRaD4OXAJhT7qRCQ0VS/ixApgkQRa6qB9pzCLSZwBDwqhIU7QvvPcGow3sIKNiGa+/YIp9z7zbTX3MF01C1kz/pZ7uHVula32K75PZ71gjTCm8si3YS3G4r9fQZbeEz+E/J6CJZqKmT5lrMbz2K2ytRuD1lFooJtLhJZbPbc23BrpNuTBwg1IsRtorwMt3gNN1otMegZaZFE3aPGh0BbOlfxlKOcwfB4MzUhecz33H2Js97IGqFnHYMRCyOBLiwYsF+vpLtbZRm1YR0rfekifXCeMurBOOD0CzWvoHmhusFHyzhrnlMv4XGYaOrD0FfL6kYztYZbBr3nMCebqKHqCFSw6IMTxqVj/KVZbT71YzZ/+E6sF0J+w2i6MCITGi7diYBcF+FxBueDzObRxopp8wOcaaEu6qKNV4LPBlvlmF8OcZFicD3C7n/H343YK0yWBOSy5YHAZweMZDycKlZr6HuCD3grf0CHdcQdOo9imjMWbsUyBYrRGls0ZkE1xZIyYBSxmS714kCo2kTn/tPhP6r/TLE6hf544g3YhYGBZs3FlmvnLVreVYQM9IDtzQHdXrNQC2QmD1s8A9jaY1aBZwvxlahqf8HthjI2UyHchTd+Ki5Am2RqpCv7kHbomavHEKOrERG4ncTuNrosGKVOnrorp3M6k+uNLxU/Bm2ZY/nOiTczmdg4NfHxI/CO3vwJ0XWIxt26wjr3wblphFmu5XcftJiGu1ktaafRiK7ew+Bp7cNNGM0Js5zaTvtGMhvzWasaJfOrUTAR1vkDMc4YuaaaBfzUSJ0fmh2MA8+kRSxHzzLaN+TSX6XWW6fH3LG/jeCTzUvuM+SZ45ESeZwHTrJF7/pNbqb9vgGb3APM7wNsDPgccBwefSrsCmxsKf25gu6KFvLmJH0HefEG/dPCm98J04PEYUpclHE9W4++bsDFHsGoxsOn/qE98AgodB079ohSrPZBrOrDPcFjILUPnsJ0x+xlj8PcM+6/4nr2gAzHUPiwnaczvf+f7D5nrY4V4EidlBu2PyZzjK+HC912QUSk5/UXwVjYzWLo7mF5KkjH2azfIuC1kLRj20JW9qrRTi3lttoeabLTjhY4OA35btOXsO6uEA7ej6AGk7JUna4as/SOVQdb+iW5CvubRp5Cv/4GeZrGYTkKOlsC6dIlfo6dXLAdtzdLzVV0FFFMgI+voJmTk3+kMpOM/6Cykw4M6IB3b6DmkwxP3k4HbAKQDkY1mJnDr1syBTerXzIdHbNIsgh24olkqGuhrzQpQ24M5vIFp1cD465h6TGGbl4xfGT/t5acGbhO5/0fczzNo3ZjObOc0bL317AGc/8TzsCXXruH+b7ll+2tfzbO9wT3rePxU5ibHW849odLrKcrNAb8QG2kWsdVZIMTjcsSoOkRevvAHuZBDWzoIQbGnesjhKLpdeUha7pJFoHldfQSofQeS+Qs6D8lcQg9u3QSFqyGfv4XNNoG2/wCZXAfKF4NunZDGMGoEN8Op4ysP0K0BPK2hy+CmwdGJIT/CdNvNehbHWLO82b3FbTHTUM80+RfG7rtQiYV6l+ngVMCYSsvmBKhb4FdGQV7crF5HejEJ9XpA92No3jbEPx5Sbt/JYBh9AeNj4B0Oux4KGB8D7xpgH4c3Z7NWz2Pq/p5bhkh5cV07Q/FrhnQqj/wr1nz203bSA9oBonLY0Vda0AKL/B60eiHWH4Qf3EVtZa1YvwFQh8MDu2Hlq4DX4Pwu4/YfzO/JvBrP6sBUcCjnp4FoR4J3ucDHni5dSwXmNcBH4hwIfl0Q9WIu9CIU2nESWC3BOnGsEUngVyu49m+AzgQt+Bhc2wZONQKiJ+BdGN0C78LBx1JQ5Avm2gnmGvPF4Z+YF0dZuthD2Jxk3jH+2v3cw9GhM/tEI3sch1lMF9YOR5Zk5xTGYwvTDhh8eZWhHsWwwOfZsfXSSYlGvgX9n8o0nIHn0ueFkRn0DKcB0LMGUcQSaONTUNJgb+Z5WRM0AwxNKHOPYxb7UdzzNFTGQwuYM8h9W8qGRZV/4PaP3P8nYXN6pNjM9zIPWAJ7KzlWTY2ZveI8twYjZwZ2J1mvptEV6c0dZrPmTQPkzvy+G2TnV9yHd2/VyHfR6sVq9EzAyrL9I9qxkFN5rxfvMA5p/I6CCf6uxRlQ/FKIh+3DYA7md87AA7uCntOYBqkMj4yLjZgtKlTG3f+MN0/KNx/WQBrtbK7KmdGTReY7c4ZFz/ncfshtCbfHuD3JbQW357h1lL46c6z4DQ0UpnNMvgQ+2QZU8oKkucFz9UCuZQS5XmST9NqnwGkPUY7IwxdRlmyrobeI9YzOLEUcNdnfY75NYnnrZ77NZ8lhP2XDEmWQ90Zgom2uBJW1hxZJ7w4pOUNfQUo+hWWahwhOttWIdReC6rI1OHG24hjBMjZTtrYhFu5d4hzKxHI5jb6DRJ7CO2MBbRcobBDsGQ0s//o/skX4Bb0szJc0bfYGJEZwwB+R51QRAMlFDgm5RDRlyxGU8WNel+XSQfphGwv/1mMU8z9XRn0yIqtBLAYvacs6o1UWX0UaoPCNK6Cw/HjBZiGTsZnEegI+PGhBXiA/73Ee6/aajGzhVmVLBhuZtf4AOqD/zCTlScjMSt7/Dne+eG8k+LGKe37PLeTkQaHYyPeYqXI65GQU7MsprBKJrGcy7Es58E4SnyI3SYeEyPtzfF8N+pmQDcnW4MweX/w1txwv6b7k+59z+1tu/4Zl/yraSbBpo+Dlx0F6GxD3vg0vPwuxVBvi3rfg5RcI7bli4KyFr1/G9/8iDIeuIzObAL+/Uhiy4iUV4PdXCS38/u+5XS3EV76SLogB/sjtJm5B2wshwE+LqMBL6JudhTfux4FqWsQG/mIEYoM04YjYwCTsEBvkinGIDfLFBMQGpxCBLgIlHBEblIu3EBt8ivGrQAl5f47vq8UPNGtBCdkaXNhCiuvM2+vMW/bpdhuYKsdZ2tmW2t1mWSnj9jQ/ld5TzxbFoGV9t2Wt0bCVtm3hmVk+9b+Urcu/8WwbeRWO0ozcb78nVHqSt5FBeoCWduDlUmHz/nXQ0o6jUQdIw3uI6gNZJkJBRdn/e25BrZqxUhoaq0GJH0EC8sVfgeunMFs6KDERVPoUGb7Mh105H5atwY4jIN17rAUc+9hwFKBjvAV7GBHM8H7OY7YxZkwnzWCoypkcpJ7apDAVfkfPCwslHF+ugLZK/1FNfTVheCIly1nqxdcZiK/FJ2Z+0w3/2cEm/YH713K7nlvMcb8ZGItqP5Ep9RL2UwvZMIkRFyoQcduCSqe4ImCwcWQ4NzLk2UzR/8G4qOzUmXn1S9i1CVYdbwX+h3E3Fdr9pGw8qD+bucv1Loe7rMOIFxpHWjQSFhpj1dtK3zPJfMNbWuXr7VgpM1T6BnChei1sXQ9Hd5KGTpb3f8OtsgGrXrMEVm8iXtVQ1OwB1AGrrSop0tshJlHWQq/y66JQaW1fn0NiI63teba2T9jaPmFre5etrWwN9lylcmabacP5hd3bocranpVcdJ5j+faN/KaZxFi5g8rOmEFaYIOWtcAYr2QeUmeWut5wXWZLaH/PrR3gdQQc4F5VPHNvHnAZAQhMsBRLwL03UiJlvgQcP4EfcgPcZhlxCYZMl8d8+zVryE95pbmsXVxp094JlfWMf4KdXgiL34P5fgmPtlws5FbR5E+IL90kXRsiYcW/41peX1k31vmGK3o9XNHjeN7A1QAjx7EGXtmeMwUXrnYYfsg0CIbwpjONZT1f+sBxYg5m18vY5fwN8Ssyg0FLsWoxvG8f4rZ/wdhSzpfLZYUPY2DpEL39K7ebuAXHz80UWwCZt+R7mglepBP5lTfaq2IHcuoNIhV+pVakIy6tk5SsG8FS0QBKws2Bhtdgf88gEuqCX3wBy1vBbTXkxwyeydYgOHcWTD1xiXX3Dt9fZDk4xxRmydfM5HuuhmkCmRffyNbp75gjm5gjP+D+SB75S+R0Yy0VTEX5GurKrwcNDzHdVN9mMh9bxHLVAbmyEWP53V9AI8e+ipqQS06Q8eNbG0HzWuZIPa82LLaiR1xv9mL9/CXii6nfe/4MOrCLumJq4O1fQAJr6O7+o5DrxSzrE2Rr68LzZtIezJWFuH0sqPhARhQurBHiJmPMNlmOMoyXo749LD0mrMYR+ijGJD6k+1j7FNoZGJvPb3B9xukcS6e/EB+EQIvNnGUMQEZDhCi+JmvMiUliN3gqcw0p+bfp0dtzETM/g4zeF9LOPUCeDl3QMtd0HKHomGs2nANp9zF3JI/eseQ0K6BthRYaBHK7k9sgRRVuQ7jdxe1ubsO4DecW1uzeTYu9MQmbxNWwrSJxpfgE7Vz4D5HhKG5R25Epoo5zk9v08MtDoh73x8UdenZpnLhH7V9EMB6l4j4NJE4XD4SAmBkEc1NwhiI4TxPSJ9pJGmWPQ2wmwKdQ0GieKEVkt5C1c4lcATS6B7lwe0UdcH89ZOptpgjHpHre19Cyd9WqmryKV+TegI1wZjnxp85ST/CiCxwLhdzNwNxSNu4h15wHTZKZx33OBA26DoaP41rBuyUaVQtg+dGUKt5C7hZivst4Q8K9HDp6HVCmUR2gTIdGrhfFyF03iY+wfC1Wa4evuseV1PuwUYiuXThOcNHL1n6pbB07WXo4AnYKkq0zx2JGzpmMbLeNKxgrf45/A0B7V8zdBqwaoFczMHefjG+N7MEdfsg0UXUxrqnpZlnebuO3O/ntx3j7HtoZPAfetmPPacfw6TiD0zVxK3duXLjKvkZyLjkMnLMBpqGYT+5PyGpxMXTJF1y8Cxw/h/cPBS9vIDq5QGcQkdwGlZLEXdA+HWt+DjlrgB8uBNyfcCzyr3IdA1fs7DhXtdvC+sqZiIHtkzPn/A5cfXfwZbp9wU9N/JR3hOy5tuMoaxFGptUMYPuCZWzwqyvgz3epm5hu8wCF3BVqYHm7zx7XoD/BcsUVBq2KH/7A9w5MvZ+wBEwDLxcCRy3e20kvT44UQZCf5bK0x1nWY86yHoA6H0LeNnHWhmiF6wLQ9zfDgBXrg4brJlqOwrScWdlzTG48xCvzDo+e5cP5zyzLGqoCx5KAzwyskwasYJs0PbBKF4DPKXqRdhJ6KrXI4MD5kWY0S5oNz/1PjMMyxkGD/GqhmMMZexIkZznmywb0cr6jnB2uAYf0wOEIfcmZYhNw+AQjfXknB1Js+wFLBsddYgdDydqi+YR7OF+y4eqBPe/P6aRF0/F+iBHrlwKHJEjJbLbF89gWywjBVkYIVAaITvF+5i3IKuyh4/9i6Lne48xzC67cGdYyv1nS7a8xJSXOTpb9jT8yT/yxwhrIwR7gE8gSm0pRwCeNDgKfdPQHcrYt9xq+5L2GryCxnyNuS4IXvQSJNTipaJ/rty6cHTizbtqV8H0lt2yVnLtDpX9jaRQMl1D5hTP3axiPQOjvBECRCz1MQzsVUMj2PJWOcASVuB6p+Q23H4XKuGMn70xKm76GdwnWixDI2CZ4k0tyR4iraMfQBoJu3wLMcoyMRNQvgMk5bishmUmIDXqAz3luDTrejTCyr3fi6N4pmTHk2EiwVRJnuGchUzieW64uOZ5i/LU8JpUiGZPfMCa/AW8rKPfLcXLXBu15OpLrLjN0nsmbW1l9elPYIzoLFaMgB5Hw6E2A8p/BtyREVA/kPjp6TOI/EesVchxZLLl0ZYnI5xjrQ+hhKXvvcsRA7YioymFfLyL+7Ub8VMH35/i+ku4hfvoMFr9WVPF9Ne4boCVc3XPk7NfuGbe8d2bHmNtzzGQfyHzjrNjmdyzDzE/HC/yUJdLIOZHtR9zDVXxnnsE4l+2mBlCsAa4vwTVnxL2bgHEvuPYuvbx+E/akFbybSd9VlIqf4j5Q/AzyFwp6PAFVfgXM5D6EE6jyB9yng+OSKnmgRyEg4l0SHeuClj2Xnvli+1NueffEni2mHcd1Ro7ltGx/7G/zmGqWSBkNvqpQF9AJmcM4HWBs5Q7JKGiSC+x7AUZMwDtqx5aromIpc1JDNwGzgdt36BYgn8ZS93O0SbA0XYD8l6CACZG6bH9NfZ/5Is+zvVovM3zgspM5HAzrdhyyPbi7gy1qKccr5TJSuZIPW/EAfM4CBS7CStSDt6cg87WAlSXZ5WfMpQDG6MeM9VXuUbxiGTbwnpGhhLmazyOXM8dsmatc4XFQu+jSZ44QRlja9cIJvn6TGCN+VLVGuEJqPcQitL7it8AmEJ5xIqB9T/wj8P4d8vM4aLiU41xQDZkDuGoCbj8HlmVUhLuzVAws78Enyn2ijxgWPUPKFVPHIuYq7xja9TDHmJMOLJ1GrkxruLbgwHLmwPbCmfddXcYyV2WM/y7r10VIWxMo9S7aWkjbh5D/vwMdGyBz2cgifkrlsopD6cgiIHOamdDBDmQRv4LemZHVtGj0sITv5h+HFE7T2EMKn3x9DfIwVjMC2ctdWaGhUs045C0XNROBd7NmMqzVNc00+NwHmG031fG+zSPNfHGITmsWyR1yzVLEle9oVkBnP9Wsgs5WatZCZz/VbIDOVmo2Q0M/1njCcnys8QPGfYw3+3Yjexxnppaed/WcOfZz1nDL8bEt72DYMi+N3UzFPUwzHm/HEZEL+0ZndXbiBts+nsHIZ1/sdnJr5DFcWXfirEn/lPmjNP0c33NtxF7tMrKFc5I70k6W/fxR0CC3oZ1v2AF5ymW9rJUkdkHTbkCqSrmeUUYVkKezsIWBoMRRQy0oIdvz9KH9HMCygrWRIzsNx0N6lm1brgloGUsX9obOP2G9LKDzYpGlZnwWfm2ptMmG1dImo4VNzrgJ2ZejJvBzV34+k5/P5OcnMBfvJ7hIz24LjKph3105it4J6zsbkc9T+O4C+gi+u4xyL6/BTIEylnZUO7PsHY0skVqO3B3Oh0r/r2NrU0D5sDNn6T9kXud8h58YLE/y+Mmf+MldfiKjmEmAoTXXA9r4CFI8CrA3WHeZXCDLbwt7yPK7iLW6oKV3IcU/hqU1Q1e/lFJMA5BflTP+lhohvyuEHvL7nhgP+f0d5HGiWEffQH7/SP2Q340yJxa+ZC5th4frgBSn0X1IcQG1Q4oPQ/aXQpffhhSX0hVIcZmwgxSfFY6Q4s/pa0hxDZ2HFF+gG5Di25D9IETAtzRh0PwzmijEnhc18Yg9qzUp4Jfaj2Hr7cy+2IH13FmdUWCpt+MIy4V9rmEz9/Aes8tKbpUF4/0pR45FdLwjouWdSFuOah04jnFgmXVma+nMdT4nros7cqZpiODZpvNTVf1kXXHkMU68rhNHdk68I2cXwDz7BN7JGVi22AdhZa4ugPo9RVWgpjnEDErJ0zGl8A1TQaORkKEa2D9Zk+e57Bh6W8bZvj9UZemPOEtvgecp5Z1rgyNnBY4cUerlGz+w8t8I7XHlDOvHiA9CwfM7sMaK2zJHjgOfZVUU0LBNboFNLqVvYJPPYpZicOw8bHINXcRaFxBDlYNX9+Ft7iFWvQhelcKG3scY6W34ZJDtMrbYTEVHrhc58/kbR84dtexdnf4Xt1xlt+EeF84onLn268wWzel33K5hnJmiRumRbIDNi6xaYCMzPgm1KyTMRkQBXq3ck7Bjm6Nnq28r+WrHZ0tnMxXm4b0OaKV8bwneE7BFZ7mW+Cn94ny81PXzKeDWZRld23EObct73vacedpwJqhh7us4inWRUcRb8PN5cl+WjoKSRooGJe3BdxOf8CkEF+RJK1dq09Qx/Y+D8j/QNIg8enZiI/TmMO9mdml6MeqvQOGPEJNWQc9/cX4q6N+gtQf9G7UjQP+zoPktWKWrog6tzKMfaGWdoQF6fx85eAMoxtLqwLbe+TZTbzS3ygPK6ESv8hAHzhztWXY0nOMZ2UZquRqv4bN9tv/A91L/5BktE5/I2g2qucKLNQOPtzVmYFAECh7mnORD0GA9MLgNCw7bd74EGFznc3rfamcCRmnNDU6saS68h+jAHtvIHkTD+auGKw6OrIHaDSzNRuQBUo7bIMevZPcHkOYQ+hjSnAn5ixM5lPv5dUnTjwxM0yTOwNMZIpMowQyFyAVke5ZHgrKgNygLfkjKlkpqgvqSjlXAXOX7nAUYF7IsqoyFtd6BqWzbxDD+M4/hGpmRcz5blkHjPzLdFQW/YTnR0fuAVGKTxLY5HVb5IaADNQHXPMhdMWLAy4DolT1+CbhAZcD1HmLHKthjF8jAv0Mfr8ozIZCBTDoH7kvs7ZkTDYxrM+PazLjaMw/aJA/gASQPeiEbrF96rkQZmO62WdzDe/gOXO+1u8gad56fsmxo+Kkt22PH3zJ+6nQdV4wdJ/FsUXzPNLPlnXSdHdvCPD7jcR9cgf9yZItp/AOvYKSv4ZVTAd9SkQv7NgGycw14n0Uk4ArKc43MeYBlV44dy2PVKFdg943UfBfOWJyVDeb9JhcZHYyC3BZDboy0j7OqZ5AYaKTGG1R/pvGHRt7VhGCtUZoEyNMn4E8e8sAk0LKELeJzjlInaioh1R3gUikkRtrFR5or8vQpS89nMZHSxmuuQxe/s50CHL/mGJZ3YZw5DzKw7XDkuER/kO+ZTrYcxWlYF3TMAQNHUELWesdBYj7AakY6gnVe2XQnzA3YIRPz6P4hV4kBJOPXiPwvQmL6IBnABpLxnvgbSMbvML4BEnMH0rABMt8GibkMOciC7e6F3BxJmsy4mhlLPfTlQ8QDJdx/jFuJq7Q+n0VfhvX5SreAcR0tcTXI+t3XiBMMzmz99bxDassVWj2fanDkmosdS4ZW7SByfqn7M/Of6yL2XOOz4/he2SnLiRX2l84c3YmVzH2u/NiVseTJyNSIOLsNXtOba3WIv061Ira+ypWf87D0Z6ApSxA3y/M357k1GNW+nIpQFYdUvYI11VDJtm4pn0b2Ak2XY+77sG876RLs227Mtx5z34J9K4eV8YDnyOeTAochY+dwH4rV8iFjiBUhSwYD160cuGLnyHbXmU/y2XOdy/ExyymfxnJh3Fw4R3Xh7MuFqeAsMx5b5BBPIeUhVMZ1rnauc90Enh9CRxbK2rRIAeftZZxpYH1xUnscem5ZS3XslbUneD45x1SeYwayxqOYT840j2daKCphVZaIKm4NQtUz7BgatnC6N5mG9dwjZddZ/BPdi68H3eSZ7d8gt1sj3KAp65EZdYBWXpA/D6akL2LHLtAqGFoZCm2/C1qVUy1o9Sn0OAnS1nLoNKj3lazF6NmP2bCn0nKspuccxJatrR2fPVJ7M068k6Fh6tlx1GjPmOueMLZu1ORznKGYylDMYChkrP4FcJbrL+T1ga0t20Q7llvhzutzjGfH9/YySn8DGEoPFwhb5QHJeAysgkBPidUD9kvdwGoX3QNWoRgj9zeqgE8YPU5ei6yvDTblJKSxEJJ0GnpXDst4HOsPQrOlrJazrJZjZc7XdaqGwTjruO5uUCch+XyvLfNWzxVTR45q9XziTsO1Wsf7/JaqDrKXt5NnRX/EEl4OPNpgO9wQp1wEp54fHw8adVVfBo26/JaCRi8gU4HwQrXAsgPWJAhevgFYdsGahFA/rIk8q9YFLF+eOA0sz8GmyHPSZmDZA2uSSY9hTbLQjgDGPcmy5lsF23GGs+NyGkR2AbyRXVSgnQYNGkR2Uc15xXlub9H9BA/EO43IMe4gSloEbNg72bAVtWHZ1ExjP7KC6cF1Rx17A1uOHPW8X6/l+owLV9N1KrrgfjumpUFlylxxd2aPp2NPr+MzCzrO3HRsiWx/xWuxpOnusIf5DZ/UcwPPQkHFZ3s3gorN1aNAxYfFq0BFKQ07+YRiEGQvCfT7M6RhFyTDBJqlQQ7CEAcUs2QcB526uIZ3hmt4peBPNVcs5H7zVcTfdzTLeIcCMbcNQ2Sj6tp8WtiBpdTI/LbjjEn3gq2l+m0I7+naqFiczzU6qb09jr+NbDeMHBnYyEKTPTBrhn64YbWFXGlbAmyeyd+HQGLdALs8PSXhfWUTzwA/ubNzR8Z5et61MfD+k6GUYazmuTnyt1V7d7yv4cSxthNncrpP2AK/gxWWiFmw5cshgc/YAssIMwj967kitknua2K1ULS+gOVL8EDCEgpYLspzP6AdLLANVyMd+NyrluMRW/awWs4ebbg2omXPq+OY1/kF36uqBVPN5T2lMUKwxvSxxjwDR7xAkau8N10rd/ahH3KvsQGQ3oR+BMG2tQHSSuiH1JVewPsCmoHJiuYC3nToRxh8qz3rijwNXsKnwXOhGbnIpieKfKqDZhyC1E+Djf4CmnFSCOgEfBu04QzaRdChdmTcn1IvMu4K3K+CDvUi465GVr4BMnMfGbdBy3ZUy5UeLdfrbVkPbNnnKNnQqHOrvF9gZCvjwFVbPeuEluulzjUs+2xldFwlsuMdMQe149HEc/J5YiPv1NhyDi7YTttza2R7ZMt5tIY5Y7uQ77lWY8sZoK3M1mWMdx76kUZN0I90ugL9yBQ6yH6W0GpW8nnA43LPv1TtU5SCWk/An0P0EfgjzwZeRBQzCP4c4/YjaGEtKHdDsw6UqwevymFWG0C5UPBK/p6ijX9P0QXf9xxc+gw5Yi88oLz/nMzgVTXtA6/g18Ergw17bhvG255h16u9JPW7K643ODVwD1NUJ72XXrCEO7PHcubaurOyTWo3lbnhoPZN1Amkz3ieeNZgaensJA2k70c8PY8xXigx9pc7HDegL+UUAH35FK0b47SGcZJ7RRwNGLniZ6tm5UqqM0fKzuxnhKztajdGyHOnG7s3lYh/QQxp+dAtBOzGzZF0x7OYWjbH0cDmdLq/2UTmY0a+7m9O4utVf7QPMvXNhRhbTKWbj/OY9s2lVLG5XNhsrqKmzReF2HwVVy29v7mOzm5uoJsxPVS/uZlqN7dRx+Yuaog30a0jDXQvId8KR3GKK32yuZfKNpuFboselz0lbRkhRMU8erllNH2xZRwd3zKRCrdMFmLLNOhapRDvTxS2qYf5/YHDWfRwy0wa3DKH7m65zH0vt8ynlmPN9HJ/EO4X0cOSK2T++AR1beHzivRZyTKqOe7J919sWUHt+0ZS25ZVQntiObVvWStstmyg2i2b8ZfHCO0WP6rZEkRntoTR+1uiqLhiNmCJF9ZPbjHDa96SIv8Kke0x9Ex+f3WpfirekoH3IeVbDtEt9zLL8/XUuuWoEMW5WO+E6ttymj7dUkFt3qvVXMeMQhSqM5dazy56Ju+3VFtp2bjlMj3Zco06t9wgc2WqEGEe9PmWeurd0ihE+mka2NJCj7a0080t3fR8Sx89dsc8IaCnu0HRzd1R4RGfRIMfRPCkOveR1O0+Rojt4zFuvLBxn8Rjnu/qEtoKZ3rqPoVa3Ker9d1ncV+z+1yMXUDt7ouFwX0ZnXdfKcQBdRpDfGqgHvfVmHMd3T4XhrEbacB3mhWHhyE1QgReJrPnVeo+YqYBd3fA6U+97t68rrx/6h5Cg+4R9MQ9BuskYK5UwJ6F+3we0+2OPM+9RDi6n8SYMjwDjh9343kNfel+BfBep1b3m9Thfhdjm6jevZVuuncA/p4hvrkPCLFVS+Yd4NNW0H2rs9BtHSVsto6lvq0TqGGrK5m3ThWOW2fgGeRhK+i2dSGeL1G03LqcOsK6hIg+BJgmqDm3ulHX1jV4th7XJnq41UOM3gpPszVQ6A5coy/9PKhKBrvy8/FlerY1EuPisG4S5k2HUQwThq2wkVsL1ZhsIw1sLcaY49R/7gQNbi0FDOWKbweuMS2eba0C725iDujnVuhnapnkEV/yni/VTx9srRUip4nfq99ap8bkj6EnWxtoEMGmzdZmPJuKNdro061d1LG1d0jGP4UMnKsn89kZ1L3VLISH3srTBx721Okxgro8RtNjj3F4NpEueEymex7g+7YpZPaYif45dHu7oDqP+fSVxyKMWUr9Hiv4/eceq5RceqxVa8lPrCvm2yBEwjKM3SzpxbLXnTEB4zzxzM+6frNHkLBJTqAvPCCDHlFYK17YeKRQlUcGxuXyGLPHIax3lHo9TtAdj9P0yKOCGj2suiX1km55XOb19R7X6GWFSeqs0AanyEvpm+S76qc/e9wQYscK2Y+56nmcxKHvwzlYv5HHSp71e0AnPdqpO2axMMZOBR2hK8nuWAPy6tFHL49ivm1WKKg7zRXfoa/bHOnqNtitbWPozrbxVLltEnVum4L+6WTeNgv9c+nWtgX09bbFsJfjqHEb16DQt5Lh6962eoiWFa7UsW2dEPtBs20bhS7ZW2i3udPzbd40EDiHuksW06Nt/gq/bSFCt8+RLmyLQF8MxiQIw7ZUatmWRXWhIxQt92GNkFQaDMmirm359NKzl9q3HX7NJq6n+/HQn8IRr+gg/sJH9cOGvb+tRAjfXJa929vUrwKkvD7eVkZPt1UyXXu21VDXni4q3XYFuFy3rtO37aYQwVFCxB+nnmDoYkKNdf672+4ChyZ6sq2Vv7/Y1kFV23qoZtsA9XtC9z2N9MjTmW56jqIaz7G4IFuertTpOZXHP/ScoWyh52zYzKlqVs951O65kMwn12HsEmHvuZzHdlbWgA6IM2OmU6vnGu5r8FzPONV4bsIcHvSdJ2yBZyCVeYbSfc9I9Z5nHPV5JlGPZzrVeZqo0bOQrp67NkSlbA/pw+EPjiv58oQN8NqMtcuVzmS7kXm7I/phBzwvCp0ndN2zlnmv8x+vxkjbXDgH/XWg9xrq9WzA+9D1E3H0NLpb6GIGhHb7SCUvnm141gXb3EzmhCGeyjHCs5ffqfU0U6cXfLmpkQazeugFbIjZqx225KYY4WVPA14jqM9rNOP+xGuc4oXXRCEq2U8LbeUVC25u1HOsTdmoYMim12Qx0gs2w2smw91ZspIeHbfoeWYFcBhBz73mUPsJN6Hzmo9xi6Rvpjteyu+bvWBPvFbhL+yIKUPFGF4brDiwHsv4x2vzkM/+Sx/VT9e8EB8EprCctXlZ4JCxgPx8fG3o/Q86qD2qlNpiDENznIhTf7He8yOwlwlNVrnsSZmn5N332qu5qNEriHq8whjmgcDTNOgVRd1e8dTllULNXsAF8Y2knXy/O/AaDQbC/hSOex1q6jqGdfZjrFeuiiVg0zqCFS2flmyk5uPso0GTQ9x33+so2y+z1wlh53WaWrxAY2nH5MerGjy8DBiuAYYbCtdXOP0lemW7Dafn8JjIMmf9EC8qnIeP43vo2Gk+/0lt0S3D5/Jq5Ljl2ZE6GkxIHb6+V4uS83OW+aWsW2gyIOXyw6XCyQv21qtPxh7DaPbscMJr61vhh1xCV/aD/96qw8UbvPV25GftJQvo4fEhueL3vEcqH4F7G+8x36cQmXc6Ur839NF7ksLztXe12SYFv/cU7tJ4T//++8PmOuY9C/MgBvNeQHe8F1v7nx7Msqyv9iPEvll0wXsltVviS533OiFKFuC9jULv7U7PvIFcQon1/TZvf3ruHUI93hGvYKNL3jHU6800okHvVMyVhTXzMe4wtXqXSPttfb/X+6SihZRL+E750XqXYZ1KrAm77H0FsLHt5viSYfa+SQ+97/L9Y+8mthn3vGGvKxZSp3eHMHr30DVv2J5Aiw/x0dILH+iUjzM1+IyiNp+xNHi01ArDJZ8JMpcROmmnfKZSvs8Met9nNl3zgV6lT6Min4X0uc8SuuWznG76QGZ91tANn/V0z2eTsPVfKeM5+tjHgx4EAX4fXyrZHUIf+ARifKiwi6olk08knfOJo8s+SdTok07tPib0FVKVTzEVJ1yxMsnoA3vtA7h8yincpwrPLwpjDmysz1WMr6VCnzrg0SBG+jQLvU+b0Pp00aX351CuD8dc5OtjpjpTO5k/HE3PfdW/wHPQ154afUfQ5f0pdNl3NJX4jhNjfGFXfSfLGJuO+s6kT3xZ9umF73y65buIzL5LKcl3hbDxXUW1vmuFne8GSvTdzGMafRFH+cKu+QbRoG8YtVSsofu+URbeTxU22UlYG3mQb4rQBxRadHXqkM5KeyAv1U9f+2YILeyaNukGdfgeUv0fI8bKn0KDSUFCn4GY4BPB9rLD9yguxVOso+aXMup7mnp9YYdCZll5+tIXc/hefmUvgdMNavatpzbfRhWv+rbgasd73fTEt48ebIfcFc6h/qMTePyz7QbpIxEnj5QwWGX7+XZL3rN9ktBvn4Jruowv8X0WdW2fS63b+XedNLh9Ma9Tu30ZmcM24flK4bR9NbVtX0f9uRY7tn0jxrlTz3ZvzOuvbJDFXkp/Z/E937ORw+3AsGcyrwhMeRVXqsvie2y3hwyNLQQNj0XC90wSwz7bI/6S70GeNt/CQ8RO22MkL4bmBo4D25U9fGs7bNl22JOsCEVzi0/sPnwTeOYjXjj8+mqgMWzBfuS320+y7zFsL7P6UfY92ysttKxRtm47YgHQxxhmFHbbr0vfznkMf4LrqX/7XYxtwrjW4XgpWlDLdsCdvBFwIKfbPmB5hrxHxZxqnOzH2Ft+WiH8AJOfM3X4jVLrQyb7/caiX8lItx/kIqCYXvhNpSY/xH1+szlXhx3CPcs6vfST90usOL9Ihvz4If7zc6P7foj5SqbTA7/1WA8yglyv3w/x3p5ezB1Ij/xCqcEPMZ9fHJn9khRd/NKVfL+/WfFR8tbPhOfQBT/ww+84y5CdXym9OKulQb9yeuZXhfkuKp74XWVcvvarxbM6fG/AO83U49eGubsUlGf0wKmXGv3M9GKHngZ2mah3h/0QPXeMoKc7Rg/J0ysZlfb79dqG6qcbO2Dbd0wU+h2TqW3HNCs/encgztgxR8VceS1cB+jaMZ+adsAG7VgqcyU1dscqjnd6dqyll+meVlp2v39ayeWODfRox2bM70mNO/zo/o4g9IUB/14a3BEFWOPxPAXPEOfI3H3HIX7/eflENRPmNsep3NT2o8NkPmqivh1HcZ3gmM1ux2l6WTYXMFVQ+45q6txhqSntUPn0VztkXoc44iTke0cjcGih7h3tFlp143sffecPefc30At/R3oRb42JqMV/JJX6j5FxN3X6T6Lr/lPotv90avOfxbpY5z8X1wL0L6ZG/2XS31CH/2rqPndUjPRfp2Iy/4300t8dc3hTg78/1fuHYK4IYYhqpq/8YzBXAtZBzHYOfsE/C/PnY67D9I2/1Y8L2xQPofeHHvqX0Sn/SrrkXyPe8Idv8r9O1XnxmBN65n9XTPSHXfCHfvl3UKt/D13xZz2iEzFz1URSLpUfp28DtNQeYKSWAGdco6gx7aJ4KwD6EzCBbcm3Aa5DMhUwlVoDZtBAwGz6cwDikYCF9DxgiTAELKfS3Nk8X9dHC6g/ALlLAPQmALaoYhP1BGyy0tKcbk9dAR547styoQkIHLKXr8ul3J6F3ToWAN0KAC9C59CdgCQLHPDPsV2YwyS0AYVkLtezjlcFHKdHAZaY4WiS1FG1Zvlo6g5AXhVQZYWjOeAi4LpKLwJqLXPW0aWABrobgPwpoI16A7roSUAv1jTTzUA9fRVoL7SBsEGBo5VcBo6T8SeZY9fLmoaaI9aVXgRCXgMn44IOBc5UNEm6xvn4k8D5uBZx3+PApRJGuhoI/QlcRW2Ba4UhcANdCdxMzwIt+hPoR12BQfTi3CG6ExiGd6KoMXCofinrJlbf85fynuH9Mk5SvicwY2isxfdoA3Nfz3tUbvSa79GmQ48DLToZCH9QOWtoHZnnvf55lUMFnniV9yBW6BBjA+H/A6tfxepDvudV3iOG5Q/Ucxx8DYTOBjZa6NEy5HsC26k5sHv4uoF9r+o5wk4e2d5psOY9TCvE6oM7EavttMTxQ3kP9e0cT893TqL+nYjVC0GnnYjV5T9mJP29vF7V3FQ/fbBzFuaby/p/c+cCNWbnYnq0c5ms2fKYvgPIJXaupKqdq6lh5zrFg50blVzudMea3vRsJ2KKnSFWOB7sjKDunTGAJUHBvTOVzu/Moqad+Rh3mLp2ltDjnSepbmcZrkr6emcNxsAG7FTxd2/eUuUHz/hB19aqWXfepJ6diMV3wi7shF3Y2cFjn+3sAT0GqClISy1BRu5rDQL9942hr4LgT+PnUXvQWKELmkBfBLm+iu3ZFrcFTaXOoBl0O2g2NQfNo4dBCy18m0ovMw9RRdASRRPcNwe50eOgNVYcHwUt58vSr2LyILYR9DLIQ9UIgnyFISiQBoNCqeOjBIyDHQgCv4IQnwdZ/GvZXIwx8f2ToEJ6FlSMeY8PyUNKKJ7DHgRxbZWuB1XR85zp9E3QRVxXgRt0Pwj5YFAD8GhGXxuZg7rwV9mN8iAz/x0MhhwG2yv+BI+g58Gj6WXwOFwT6VnwZKELnibzceoNnkMPg+cPrR+8iHrfP013g5fS18Er2Pfog+Eng9dSZbAlz5RxCPL5zuDN/Nw22FPVTOQHObY2eSPdSjFy7iVrzS+Cg4bXL2UcDl219NOF4DBZN+P8vzU43lq/RMxMj4IzGIf+4FwaCD4EWI9Sd/AJYR98Wq7DOZ8hWNYKqjEGOhpsrVcBZ+hncgLHcFeCGzFXC90Jbqfy4G7g3id/xkkDIQZ6HOJI34aMpD+HQMfgC+6HjOf3vw2ZpOqXIVOG7MzHl6kjZLrKCULmCl3IAlyL6XnIMhoMWYm/q6k1ROlNXwhiwhB3uhTiTQ9D/KknJEQYQiLoQUgM3QphXZExwVD9MiSf5fBJyGvxLFTIPqSEYzJLnV3WrtQzKascP0991U/X9kI3Q+Bvk6ZhzTI1JqQSa0PnQq6osftmCZuQ69QechOwQsdCmoZ8Twh0LaQDeCCWDRmw0rJrF2K+XUYaPBfFvNTtQs67a5R6umss9e+agMuV2nZNpd5dM+jRLvjWXfOE3a6Fiha7LLHqPtigTCUr9ruW04ti+KddyH13rRGGXXJe+N5dHmz72nf5UusuVYN5dqxX6DNzMW8oxkQidowTWlMUPdmVJLS70i1wmOjFrkLofDF17DpOPeE3XvfT1LmrFGuUU/euKo65B3ZdxPxXrXFj665avgbCq+nxrjq6sasBdnEc8G6m5l1twrIXBfx6gZeZnoXqVc0kYyzu7en64TLq/LCbOkNHkDl0NK5xdD90Ij0KnUz3Qof2nZ6HzpSxAfIZg9ofCZ1P34Uukt+pL3Qp3Q1dwTXnp6GrMMdaXNC70M3CJtTzlQ2hx6F+Q/mu7AsNwrph1HtgMvWGRuE5fG5oCtsbCffd0AzAkGsZe4j6iiswBj4xFP7ubJ1wCD0NuCqELlTtQxhDL9OT0Gv0IPQGde9rUvN8EGK1lw9D4eNCGwFbCz0/C7kJbQe83YC/j+vonbsFLoPcq6Gnux2FdvdIer4b+rV7vNDtZh9NvbuR9+yeLhx2z8LYudR/0lvody8QNrsX0+Hdy+jC7pXUuns13llHXbs3UuFud7q925uadvtb1cNmdwjmRl64G/ljPnLn3Qm4UtGfRYO786loN3Rpd4lw3A2d2A192F2Jq0bY72YdooHdlvr9EcjgbkvOt/suPdvdhGetuDroy909YvRu6EKYVuaIVBbmPKSfYaOoO2wsDYRNEHZhrsI+DPIWNgPjZsu9EB6iDUMeEoa4M2yJql2FLVc5tbDGDtQU5ibr4cImbL3K68M81LPX6oCWfioJQxwaxjEoNcRtUGPguzs+SaWusEjLexOoJyyOqsKSqCMs/TV4TepvfB31FkMmE4Z0vDmsEO8XA5/jij9hpXQxrJy+DKvi2o857CI9CbtKd8Jq6euwOjyDfoQ1Y1wbj2+Ve5DA70VYL5nfV75CF2amznDoSTLoHG4vnMJHsG7L+LI/HHlDSSq1HVe15Efh8BfhE+liOGT4RCG1hk8T+vRF9Fn4TGr+SO159YTPwXvzqTd8Ed075EuPD1+huvClVhx6w1cIQ/gq6PBaBUv4BmoJH8rtWsI38/UyJVL207fhfir2gO/piFT+8mU4/FI4fFJ4FD0Pj+d9MkN4Cj0Lz6C74RYdCgf/wo/yfUP4CWoLP00Pwyus68CG4Dn8UbjFHx0KpFY5v4wRw28AN+hPeCPgQ3wY3k7m8G7g3idGqnIn9UUY6EGEo+Kt3KeTNdGIkdQVMYYGI8bjmkStEfBLGXFcJ3KOmE4PI2bh4nyJ+jOnkTliAcYvFiIC8V3ESjEmYjV1RqwTLhEbFUzRh+hZhDs9ifCm3gh/jA8R+ghLvZXj4Ql0JiJG1XEjEuhpRCp1R1h8k9z7jsji62lEvuyn+xElwjYCepZ+A/OVqfw7ohJz1lB/xBV6EXGdeiKgYxF3sVYTtUdY6ikVE4ThM85HME8HcO8B3Fa5hF/SCpvDI4Vxj4r3yvc4U+eeUXRtz1j6dM8EGtjjKnR7ptLgnhlUv2c2PdwDmu1ZSLl71P540J7l1LHHDc8Rz+1ROWTdnk30fA9yuj2+uKBL8njLnkje0+3bE0dde6A3e1TMZt5jwruFeKeY8vZAN/aUCpc95eivooo9qv4xuOcqrlr0ITaDTzfugW5UWPbOZT0lQ76HPG1Pm6oT7unF/Gar7+nc08WXpZ9uR+qVn8ezLovcmCPhKyORy8XWUk/kOHyfKBwjJ9OLyGn0KFLlbebIOcImcj7GLaL6yKX0LHIFPYlcNWRDIqEXkRuENnKz6olsp8eRnrx3Zo70o85IyH5kGPVFRtFgZDy+pwiHyAxcSu77Iy12K/LoqxySBiJPAJ7TeL8CVzXk/LKsM7Jc2kVew/cbr60PuY9spAeRLcIQ2Q66LBcjI7vpeST8xl41yLDXQC/2OlLv3pHUvxc+Yy/isb3WvI6e7J0inDMbqXvvdHq5d5bQ7l2AsZDzvRvlhb658nrVT1F7l1HPXujQ3tV0ce86yzh3xEreYsRef4wNYV+q2xtBKXtjaP/eBPLZm0pPEzbSo4x59GxvFqXtzaebew/T071DexRH9p4k/71ltH1vpXDcW4O5rpBpL+xcBWzw3pvkufcurib6eC/kfC/yl7096B8QY6K0aoYoo/oLWjVGWXxK1CjqjRqLvxNwudKXUfAnUTOoJ2q2sI+aRwlRC+l8lJLr2qjl9DLKjZ5GrRFvVSIGjVqPMZvoZpSHcIny5ensj22i9VGB9O9RoRQZFUnnohA3RSVZcWiJSqfDUSauKXZEFVJZVDGdijou9xA59j0ZVU4VUVVUGXURz67yvkNXVB0NRDXIOhDXe6ujINNRXXjWS1VRZiqL1tPzaHsxMnoEJcTMpaLYsfQoejQ9jR6HayIlR0+mC8VzqDRxyI+OSDEJQ/Q0oY2eSaboOfRZ9HxhnwO7Fb2ITkcvpcRoxETRq8Sb0ZDfaPiW6M305/f9qCRaxURx0X5Ddiua/60WKowOQz4+nz7Zf40+iY6iouh48YPoFHk+iPqiM+hgST6dj8612sLWfQtoIPooHY8+AVhO0xfRFcIuupryoy37dNFy7hvUHw0ZzoOtrwAdo9X5irboFnmuAri3y31s6ozuo4YYReO2GANfT2McxWsfMsWM5L+3Y5C7xoyXtQt6ktnHfUdjplBHjDrr9E3MLKZjS8wCMsdO4HVaY5ZZ57kfs5K6EkdZv38Vs5pqY9bhPbbzdDjGnSpivCk+xho30XcxHMtRcUwEfRwTQ89iEij3zCK6FfO9/UwZF8Rk4Xm+kpf9rMs0eGzq98dZxh6mypgS6ow5SSdjyig3ppIuxsD+x1wBDtfx7CbWu0uXYpr+y7ufxQzV121jOuSZPrXX/3//UEUs6xS9iDVSW6zzsGeXYkcN+y7lEXRk+8XnYL7/SRxrHdsRO4Nexs6m/lgVZ1+IXUi3Y5dQXuxy65juAvXsfKwbXT/mwfuFuYfHU2fsmu/PrOq9scoH9e6v//7zYWORJ9ON2E1Cv28Z1cYi7ov1pbuxgcImNpRyYiPpTmwcfRKbBHjS/4/z3Ig1CZtPx1NfbCE9jEV8F3uc6mNLqa9A5WZ3YsupMbYKtLtI38VeZf/SFVtH7bENuG/mmuGj2Da6Hwsdj+2lhlizqmt/DL2Is6eP4kbQn+NG0824cdQcN5E64ybTrbhp9E3cTPTNGYIkpZRexM3HtUjlTXGI2+JWCLu4VdQdt5aq4jZQR9xmfIdOx0Gf44JoMC5M6OKiFL3iLHXEAiM9i+MaGf5m0KO4XLx/iLrzVlBj3FExNg65ctxpmftSWVwF5uWchrriLlNb3DV6GQcZjqvH+o1cQzDEIQb7QOXU2rh2jOmmnoMrsV4fPdiHzn2WsxTyHMs+R3mGU+0R7YNvOlY4lH/zGEs+rvrp5r7xQrtvEtefWvdZ6giI2XrzllLvvul4hnz4U/iufXPZ/nTsWyz5LfS832ORzf0zqUeec0q01omo+8B8Gty3msz71ina7NtI3+1zp6Z93tYxg/vUOapb+0KAR4Rl7yRS5fqvf6w1V0s9zLJHZ3uscGjMvpjXxk8FPAmAPZUu78ui3o9XUP++fGF/qooe7zuMfLEEa8u9t7phcthzbhV17it7tSYNpqSDlpW89zbs3MP3L8v7dftqAMcVrHud2vbdtMZOLzMRk+y7q/LzfU3y3Cr+tgLvDqzXo3IRS548WJlFXfsGqCcxlAbitdQXb6TueGcajB+l4MBHFz+WGuMnUEO8dR/h1Zkrehw/g5riZ3OdsT9+IcYuoe4j64dofjRSyXa8OovVlzBFfO9DfWcXDs2LvON5vBvvQ1+Lh734eBxgwXzxmwCXBz2N9xV28YhP5UHu1+Z4ER/5ar9HnkXB83Q+82yOB8/ii6lX7gXHH1d7ZfKKr1M5n+onUzz8e3w51qqi2/EXuSbc++EcehKP/C6+lsc+j2+gW/HNlBzfNrRyfBe1xfcOxyjRakMpJKWcglId5cXfz8ebqXq/nsr224u/8KEL+0dQz/vx9O3+0ZI3w58eC6X+/cihjyk8O48l/Zf3+/ZPtN537Z9Ml/ZPY325sH8Ofbd/Pt3ZD1uzfymd2r+CGvavIvP+tfi7YdgczRXFdHa/JR6W52z2+8nz3Kp2vh+x+/54ecZOnWfaH4Y5ouT1ql+egZDP0JcL/3hI7T3vP4E5TtPA/gqV++6vxvPLHIvAhyrbXwn/XDD2FW70fH8jfZOYTi/2twCvdnpUMgX33TS4v0/BliDoXgLywgRrHEF3ElQM8W3CGDqfgDj5/Rbx64RJUu6o78hrMpOAWCIBtiZB5YddCQv+K00TFr92vwxjV0pbQF8nrBa/SVhHvQmAN8GdahK8qSfBn/oTQjAmYhgtXybEIG5WtU0+G5WQJc/gq/N7CbALCZXqXKS0MXI/JgF2IqHsVb86t5KAPDHhJn2TcFeNS2ilRwkd9DChR9YrqC9RS22JRhk/qrhH+uvECfQ80aKvibBPiTOoPXE23U6cR/cSF1JN4hK6nricahP53w2lJ4lr6FxOCFUnDunu5URVz69I9KDPEn2p4/0U8fPEQLYVd49MsI7rTYykwUTIZKLaOwfPLPI677UxJuv908RCupNYLO035i0VPztRC7jKqTuxik4nXqSGxKuAvZZaE4fby+8qSgFTg/X7k8RmoUtso/7ELqzfS/1JenqYZNUrepRo5svST/VJ8FFJo5WNSVJnHzuTJiq/nTSZ2tMzZJ1Y6JNmkjlpDtUkzVfwJkFnktQ5xuakFfQ4aRX+rrWuY06C/iTJ/VgLvZM8qT3Jj2vjg0lBdC3JcnYwGTFqEvLIJPjtpBRhTFK1/JdJufS4GGOSDgFWVUN5kXSCupNOU29SBa5qzHdZnteRdSI+w3MjqZ7uJalYezCpBePbgWM3nkE3Pq0UDsmAKxl+Olnphi55JHUkj6H65PE0mAz/ey5M2CS/Zos/XUm3k6dzrUxfOIL6ktXeoPW8+vAPXU6ey/mQpGVL8gI1BvDJc9VPkxcruqSvlTjT4+SV1J28WhiT16k9imT179CKZG96mSz/vT+Zb3pQf3IExsWoPYrTKqdrT06lhuQsOp889LsVc7L6/Ul9cgl9nQx9eT8MMJSx/7ydXDkEpawRJEO3ki37A8mqtqlPvmmdayD5rvV+8Ogm6jucD7o00cXkVmpL7qCe5B5hSB6ghylaupNipP4UZ9i44edrBlJGUWfKUIzcnTJB/rZG6FKmUt/Hk6khZQbena3OlqaWUX3KQlxL6H7KcvS7CWPKGsy7HvNs4vMB2hRfakkJ5L0v25RIak6JowcpSRI/mRfSVymF1JSCuDXluIwheU3pZzpSqqgm5aKwS0FemlIrnFPqhCGlQdikNPOYCyltwi4nAvN3WWHtT1G+yyHFLIwHkKcesKdTB0bQswOjye3Ia+cGDoyjngMT6ZMDk/k92GL+++Gx13z3gaGa/uCBmcLmwBwZlwnDgUV06USDcDqwFGNW4O8qOnYAsnFgg/zNwnDJOgA9OuBp5V/7AT/KOxDEOerzA1F0+0A81R1IAXxKd24eyKVbBw6h/yj6TwjbA6cxrgJXNda9zPttzQdukPkjyPaBerp/oJEaD0BfDrQL+wPd9OUB5KWpkLNUA59FlXNKf/04dSR9ljpG6FLH45okRqROwd/pwiaV9YKqU+cKYyp8RurQGc8XqSoHdUhdKexSV1Nn6jrKPzwXc2+kXxwZignMqe7UnepNH6eqeLQhVeWeR49Zzy+9OtthmRc+JTWGulIThG1qKp1PzRI2ptH0MjUfMBym4tQS3ht8mXpyOC0haxLvV7R8mFpJuanQh9Qr9Dy/lS6lXqcLqTcBy10LTk10NbWVrqR20L3UHuGYirgwTUs9ZXOFXZqRzwjeTIOeH3QUtmmj6GraWPoybQL1prmKEWlTqSJtBt1Im00P06B3aSqXCkpbQk1py+lkmvr3qQ1pa4Rz2nqhS9skbNLUb7Iup/miLxB9oUNymcZn8MUbaXHCIS2JHqSlU16aiTrSCofTMq2YnqUdp8I0pQfX09Q+8Qdp1nMiw2LmnjTEdmlXqT2tFjjV0bm0BuGY1ox528QbZavp/TT4krReeplmHk7LdL31vI3cK5NnYFrTR9BAviWHUef2Xovb1w9b93o6/E46fE76RGpKVzokz7x2pc+kwfQ5bEu70ufTy/RF9CR9KTWnr8D9KrahL48wLhi3AWM2y7NZ1F3sR8/T/ehpepDKndItv/FKj6J76fF0Mz1lCOd0pSuN6bl0J/0Q9aUfpf70E/y7tLb012r06dUK5nR1tuNFuqVWnz5Ur+xNrx8af240fGYj53KX0hGjpbcDpm7M00ftBwW1HjQI7UFHMh8c+TolqevgGOo4qPaUpf94eRDyf3ASximbMnhwFt631j7o6cHp1HU4lfs7Ds6luoML1P+44kQcdR5cbHkHuncQ8dnB1Zh/HfUd3CjsDrojb/Wmbw5afj/D66gzk+UHY/BuAjUftNZ3yJyBGOZgFmC2+JqDh+nxQejWwZOArYzuHKyUMRrmrkHfFcByXRgP3hQOB5X+9B1sQh/gPtiBq0fR6OAA9WZoaSDDiMuZbmSM4v1RWQuW+tSSMZUeZcxQOGTMpv6MefRdxkJhm7GEa8HOGW4YuwawKXtgyNhEzzM86H6GL8YGoj8U4yJfx4E6M5LU7xFkniP3Sv7S2X/VT7dOAv+MdGGTYaK2jMJXY+RvpQCn+m3ly/KZsl4POEqpLqOc+jKqsM5F5V8zrvLvO59n1NKLDGu8hrENMn/kZ48zmoFrGzVmdAE3+JsMMz3L1JM5057aM0dQa+ZoasqEbmRORJ/Sjd5Miy/JnCnP0/GtTeYcPEeen7kIl9rjMmauoJ68G5zHPspcRR2Za5XMZ3KOS9cz4U8yPdUee2YQ1g3Dpc5RazPj6WlmCn2XmUFdmbl81uBF5lErDo8zT9C5zNPy9y3Ul1lNdZmXqS3zGuZWNb/WzHpqq6il+sxG9LcIXWY7xnXj6hN6mbplGei7LMh/FuQ/awy1ZY2n+1mTqCtrirDJmk63s2bRs6y56F+Av4tB5wx6mrUM76ykJ1mrrSyzzVqH+RA3ZbnT8SxvKs/yR1+IPP9L32bFUEcW4qWsVDEiS54JRm6RdZhaskooN4v9Ae3OKlOykQX5zeLfYOGdK9SZdZ3qsm7iuov1m/A+ZDerg/c+7mUN0NVsS10+20gd2c70MnsUncoeK3TZE/DdVRiyYfOzleyas2djHOQqe6GKp7Mhv9mWmmC2m9Bmr6GybNj97E38u6ln2chlsyG32b64TJg7kC9LP93PjsPYJBmnUl92uhyj5Da7EN8hl9nHsWappQ+5Q3aV0GdfxDqIfbJrhUt2HcY10EB2M3Vlv5avZ3cJ2+xeYZ9tpsMmPX1msqdnphFUC396yTSOBk0The0nyLFMk6nVNI3umGYKrWkO5ZrmK1qa1Nm/AdNSYTSp30i2m1ZRjwlyZ4LMmTaT2eQJ/+yHviCMC8PfKOo2qVqd1pQif/9Et0y5lG9Cbmw6KpxM0FXTaaowWWwxYnPxGXTXZLHHJuQRJostLpxD9aYblGSq5994NX/QQ+WmFqoytVOjqZtl/jNTH13OEfR5joEu5DjK3zZQS84YepwzXryRo35D/GXOFHmOStjkzKKLOXMxbgE9ylks9xioIge5QM5qupOzjrpzNmKcO1XneNO3Of4yR7SSUsaSIgfxek4CxecgLsnJEg45sJ05h+mDnBLMCfnLKRNv5lQKQ06N0OVcoYac61SSw/E3xeVYYm95/imHa2x0NqeVHh9fQfdyEIPk9NClnAExNhdymGuU5/npfO4o+nOuqhGYcyfQd7mu+DuVSk70CtvcGfQod7ZwzJ1HSbkq/nicu4Re5i5nOTLnusFXtlPnJ9bfGlPFOdA8dw0N5K6nutxN1HpkHtXkelBPri/T6VJuIH2RG0ptuZF0ORf+ITeJnuam091cyGNuIf/G7XbucarNLRV2ueV0JRe2MfciDeZexbNais+to2O5DfRZbjPmb6PW3C4qy+3FZabqPOvvdIU2z5768xBP5FnOe+fBHuZNFDZ5k6mttIWy8hAr5CGmzkOskAdZzFukzgnmKRl8kbfKaru78pQNvJu3ge7kbaYHeZ64/OizvCDxRh5kK0/Vj9Pz4qkwT9WNH+dlUFNeLtaHTOYdFbq8E1z/1OedVns78pNXTa15l6kv7xrb3G/z5L6P5VxnXgvTdjCvm7rz+tg/a/MtdWK5byuv/OH7PWn5qlbTnz+Gvsofr8Ycs+RR+ZPkb1Ms8tFNGfnDfndFj/Nnif/uR1vQS2fy51Jn/oL/9jv/Dz7Ula9imYF8xDL5K+l8/mrEmutwv5Ge5XPOTH35MXyZ872HvftRvopx2vNDqCE/gsf056u608l8+Pj8LMsa+ZSYPzx/NeeXiP/mh07nn6Qv88soK7/y/wuu/39/6Ot8y2+NYS8H8q/Qsfzr/BtYkX+XHhR0UUt+Ew3mt8ozzfQwv2fYuy/yB4Qo0Mp9FVzOSqcKRqnaZMEEelLgSo8LEO8XwPcVqDPz3QXzaEDuHxWoukVTgap3Xy5ws87bX7AGsog4rkDV1c4XIP8p8MXcgUOry3/MTMaCBZHUWxCHOeEDDy8gc8H39rQK4DMLCunPBXz2m+oL1BmltoKh39uZC8pfG18lDAWwSwVXAXstdEqP74jXChpwNdPJgjZ6XtAldWXYMtoC5EGF6jdu5kJ7/p3v/ULkMvw7wnk0eMrym9vXz12+RssThZZ69AcdVF84WY0p5NiOOgpnvvo9KHUXzqcrhYuG41i4VPw3P/S8cAVdLlxFVwvX/t9H/7/7gAaWWvvpQrpRKH9b5En9hX70XWEQNReGUU1hFHUVIlb4eDLuU4a9rC/MAN1zqbMQNrrwqDxHQ98Wnqa6wgphU1hNXxYi7iq8hjHy98f1VFzYSOcLW+h2YTuuYefsqaKwz/rFoQh8LoKtLlJ7FOaikcJQNAbfx1vHDxap8yZjiqaIkUXTqbtolninaC49LlpAz4uGaiDmomXUeWYtXS5SZ7S+LeI4lqqL1lnXe7doo3V8R5E7DRR5U3uRP+BAbFEUIRyLYsToogTAlCreLsqi3qJ8rD/cZtUXwWYVWesddK2oTP77NNR3uIRuFdXgukI1RdfpQZGKNc4X3aULRU10r6gV/R1C+z5y4KIeaiqCrh/Sci7550NG+uYQ9P3QKLp4CLp+aAJ1HnLF36n/u7lzD9erqu/82vf7fgERkVJEmiJSRYvIKFqLSC1SpEgpjw+1DKWMUurDUEVLKaWIqaWUOtRShqIySOnJueeck3O/A5G7CAEBKcYYQoAQAgkkBAhZs/bn+3JOTmmnnT/m6bzv8+6zz9prr8tv/e7rt9ayqzqOtGs6brUPdnzIPtDRnvvuON6Odpxob+lo67kdp5u043Pur6PxDtH4yx3nm6DjwibebaGtO5uNDN1nn45vuN9V9qcd3zL7d1xnN3TcYDd2LMwxune77eaOAXt3h3wb3+9ox8B3LPo23tFx50L+Jzruc+19yD7Z8ZjJO9bZ1zs2uno3m7d2bHN/d5r9OpzdtSK0W1YsmXuyD6zYy5gV+y2mNHGaKw4RrY45vFtxmN294ohmnbtwYcVRS96/a4WzFVcc6975pN20QjQbrDjVveN0khWOHpv5xhXnmXTFBU2MSruOSxq/rUuTLrJjxTeFDysW/RbbV1zv8tzofu0YKzdm61YMNfH4C3l2rUA3cf1abdeu2GP9+Io99mlwn2SFo4kVj7s2tfWVFU+bN31WbNnj3tHLilfhcT9dgnrGPtrZ1ms6S+N3LvVt7J51/LLzTWvM/19+iK3Y3LkYD/ZIp/Smj3Qu6Ex2tPNofJHu92+X1NnWjzodPXeevEf6aW+qc3eno+vOs/jnpE7F1TzT+cV/tX07O79kH+68qP3epUuePdbZni+bP9oEnVe++e12vhc7r3Z5r2XPqbWdN9lNnZ3Q+lOdk6x7bX5Pdy7ygyb9y53zdn3n7faFznvtUOca8mztXGunOp3s79zk2vuCfb5zhytzl72qS/Eyv92V2se7avtIFzEydrJLtsae812ndx3a+CDf8EPa3V1Huvc/ZF/uWvT3/VaX7I+Xuk60T3ad4vKcbsKuzzVz4/bVrsW1dPd3ne/SLrQ7uxTXv2SOsp3n1a5F/86mrqvsTNe37DNd19mHu24wftfNLq3blT9gb+satbu6pht/oXvnziVlrOy6z7XvIcGy67Fmbxv2A2v2EtnZtdlu7dKa/+1dO5t1yXZ7d7jk/Vu6FQv8Yvdedn33fu75gXZrN7Ldbuw+TLHT7n5r91FNLNuSd1/vPvbfGtc39fWx7k/aR7pPspPdp/5H3/nP+Ninu9v2VrdsrVu7zzFh93nSFbsvsK90f3XBX7yz+xL3u9ylX+HSv2mi7msUd+J0paT7etYOvd7teFx3swfZkN3VPc56hFe7V5u4+24H0z14Wbd8T7u7H7c7utfbl7oX+VjZvcXlf8n9FDN3S4/jez2OV/WUdnXPPnZ3j+NLPQeZd/c4XtHjeESP4w89R9u7ejQH3t/jZHmPo/0eR/s9kt8be86wW3vOcu9+3v0cffd8ycQ9nEDX7FNmX+xZoFn3/GpiYXb1XGu/3/Mdk/Xc5OrvNH5Pv72zZ7idZ9KEPfMu7XbFGPWscWnt+bgmDrj3ULupZ61kDzHXbb3yjTU+S3VM+0rPBlfGJvTLV3t2LMSub+3Z5cryKXN7b+p+tfvta5/rPYC5kqaerb3teYTeI+1LvU6P7/2Yy3O83dl7ot3Re4q7P93laces9Z7t0s91/y/uqfFSLzGldlvvxfaF3suaWBdX9lWsCd3cu4e+3ivfVdB7s/Cht71es3cxfmnPfTJ29U7bLb23Kkaq9077au99Lu0h1+bH3N91Lm2jiXo3u3e2mT0+Ln2nfapX6912zxxkX+sL7da+3L7c194bqs/J+L4DF9ZXmon1+rXfv6fvELuzz+nofUfYNX2Kn272zHq871iX9kni3rb2neryfNbe33emfajvHPuzvvPsrr4L7FN9X1U/+i5xeS53711hf9T3TZfnGlfW9faRvhvdveS46et17w/Zh/sW5fh9fZLjP+hb7dpxtyvHjf2IG7+5Drutb3H+4Sd9j2u/sT7Nfe/oa+N/34Lsdv1d0HVdW1+1DzTmrYPlD/odLfSXdlP/PnZX//72wf6D7Av9jhaaucz+pf6Fh/qPtg/3iy6aPWWafbiavQubfY9e7D/Dru8/i2fN/gnNb2v/Etlne/qxz+3T/RfZR/ovdXmW2+f6oRU71X91s86j/fw7Tdztkndf6l+qcPwfPrarv99+v3/YlTH57+f+z/vYe/vnF/4J+m9v9oV0MF9jov5H7e7+DSbr37QQm5X2r21+b6Tbvv4XXD5H2/277PqVPrFZuxs+u7K2G1fua6qVBzDPsGHlwfafVh66EIsbrTzSbl35oaUt2SMmpW/l8c2c6xvzrvaBlafYu1aebm9ZubjOdfNK8fDVK89tYlntjpUX2qdWXmze9GmOzGl4+r+MzVq1h901tzh39srK61x5NzQxhe5vt92y6hjXtyY+bdROr5y2z6109L/yTlf/fUvg+PzcUfbWlZLjO1Y+Zl9YuY79NnesdPb7ym2sC2yebVu5u1l7bZ8fWKrr/2xAvGDXwH52y8CB9qWBQ1wezbnsGDjCvjAgun954JgmVnzJu68P/Av7/N/+2J0DJ+HH/cnAZ//93P95H7t9QPL7qYFz7DMD5zV7k76xr5l9YkBx8xsHLne/G11/rljy7uiAbJbnBq6xDw9cT54nBrS+/q6B3oV1XJsGxu33B2aXvLtjYLX5D37cu3fbuwfutx0DD//7uf//+Ng7Bh53/V/v4Pu0/dGksT8a2OJg+5JdNyDd5PZBl2cwtj8aLN29000G93e/g9z/y+yuwcObuAV7z+DRdu3gR1mDfMfgCS79ZPvk4Gnu7xl2ePAs+9Dg5927X7Q/HRSvvWPwInvr4KX2lkFnUwxe2aw5N/HgtfaVwe+4/xWvv3HoQ67sTvv64OK6lWcHZTcUg5MmH5x35d1uWoP32vsH19iHBxdjhZ4ZXGufHtxg1wxqz8IHB7UGf83gov/wHYOL6wcfHPLthqHU3j1Um2RoX/vskONTQwebvYccj5r8kgmG3mO3DB3p8izhUXbd0MfsM0OyH1gDPnSi9scdOt3uHPqcuz/bbh86V/Or7vfy0PlL3v/boQvd84ubfUPtPUPfaOf7ll0zdJ3Jhm5gLeGrQ90mHxqwA0PyLcwNTdvNQ7e6urR+a/2Q+E4Tm/fKEPO6TQyP/eHQRvvAkObT6qFtzLuYoT1iJVa17YZVuX191V4mWNX2K6xauh/exlVN3J3jO6s0z/vpVeI7jg/uUdaxC/l3rnJ2waqTiK1+dtVn7aurzrTbV51j4lXn2X9edYH728TbXeLqu3xJPXetusLJhoX9NuzLq66xQ6uaPftuBLYbV3XYJ1b12l2rtM/FE6vGl7z/0qrZ9vr89rym0yNeWPWwq6fRQ9bbTauedr8t8Hl/lXD7lbYZmgxrb9/7huVXWzu86CvIhvc3+w4fZKphrf2dH3Y4P/x+u3l4cV6jGJb+sWv4OFem08uHT7Zzw2+yxY0/fMae/yo2t7Glhhf9bK8Mv8k2d2lfshuGL7K3DV9q8uHlJhyWfjI0fPWb6mg+aRMrMNf2sQx/x24fvqnZ78D97XdtdJ0enrRbh+fdX6fbD99rnx9es6S+u4cfNW8fXmt3DG+wzww7Ghp2OvvwDrtzeFej69ntI6k5ZKQ25ci+dv3IYjzd9pGD3fNDl5T1zMhiDNCLI0e6546uR9qyfeR4+9MRJ9NHTjH/2mfvkUWf/Csj/2JNysjZ9rWRc+2mkTZNjVz4pvePHln05b02cpkrQ/Z5a2TJHox288i32u27zqQjNyx59urIzcRpNvevj3S7/jjZPzJqnxuZti+PONk/8pjr9zq7a2S3+91pd4zc5/5/aCH9ypGN7u9mV/c2u3pkJ/leHw3tPaNO3o/uZXeMOrobdTQ3eoj9x9HD7D+MHmEvHj3KPjt6jH1q9Fjq/eHoJ939SfbRUc0hfn0UWW0/PnqmvXFUfups9Dx70+gFdvfo4p6+Hx+9ZClARi83J49e4ep3dDa66L9bO3q93TV6o0uTTHxxVLGtT4wu7jlbji7aADtGZ12bVru/wrFTRh2tjcreXTP6uClGnc4/6vT90T38dA3vGnU61qho77Uxxz/HHN2NlXbD2D72xTFn8469324ZO9o9O839nM4/tsz9f/hC+o1jH7UvjR1nd4ydYOfHTibfK2Nn2LvGznJ5Pm+3jTnaGXMyZuwie9PYpS7/cve70q4fu9r9pMffOPYd+5Oxm+xDY53t//vbf29vfmrp2LC7d3r62PxC2xeevfEZu9ecPLbG1e/kzthibOrasQ1255ijmTHJnPYctp0dW1wPtve4v5B/23hqfzhe25fGtdbrlPED7Nbxg3n24PihLm+zn6ejm/Glsmdy3NHQuGI6/fET7bPjp9h/HD/dhOOfsy+Pn23nxs+1U+NODx6/sNkP1OW/zK4eX/BPmWL8KvvKuMP7cYfz4zcwz/HguLN3xwfcs1F77/i0vX38Vvva+J1m3/H77Oj4Q/ae8cfsc+PrXB7FqQ6Nb7brxre5una6PLtNNhGat07kZq+Jvdy99m5aPXGgKScOMf7EYQt1RxOSJf6EkyUTx9gNE8eyhm/HxEn2KxN7+JMmPmtfnDjT9k6A43b9hPZcnJpYXBe/e+Kre9xfYsKJy+3zE06WTHzTXjtxjdl74nq7ZeJG154O+72JXpfucHpiiexw/zudb0J6Xjlxt31y4n573cTDpjXxuCtrvb1n4mn7w4ktdvPESy7tVfsDpyc9OBm7v6X94eQ+pp7c326dPMhun1xm9pk83FST77c/njyavSeqyeOcXnWCfWjyZLtt8jSz7+QZ9u7Js+zayc/bJye/aHdOSid6YPIil3apvWNSftZ88kpzyOTVrrxrzQGTmhO+a/Im89bJThNPLs4D+5MSYm+fnDT7T87bjZO325HJe+0zk2vsdZOPLvZxcq2ra4O9bVI60VOTws9HJveYU51c1Im2T/l291RqN0/VZr+pfe3w1AFm7ynw0v1/qL1+6j3sY/7y1FK83DX1sYV1QtnUiSaYOsX9f7pJpj7nfmebaOpcE0+d755daIqpi13aZSadWvSv7J76hnvnKlNPOdycus6UU9pzaOfUze6+W3mnnC40Ne3y3mqfm7rTvj51nwmnHrKvTSmuLZ5a5545HJ3a7Nqzzb7S7Hs3tdu+Mh3a7dOys7LpvdirbPf0gr7zxv53Lu0w+/L0ESZo4ummjzF7Tx9rd01/crGN0yeZvaZPNW+ZbttM02e2y9xjn+Tp8xbuo+kL7GvTXzXp9CX2xWmn90w7+2Rmf5fH8eHpa1x515u3Tjscne5YAsut072uTeLB4fS4K2PWxNOrXX7Hd6fvd7+HXXlOv5leb5Lpp+1L01vsjulFn8rPpps1BkbnCcyUJprZp6m38TmZdGaZ3TlzOHvS1zPNWr6P2p/OHGe3z5xgX585eWHvhrUzZ9jnZ86ym2Y+b/yZLzZrJ9xzh7MzF5lwRnME8cxyu3XmyqVzyjNt3WTmWvvizHdMNnOTKWccz51xOsjMYvxzMDMp+TqjeLfdM9rf5JmZRZ0km1nEY3/G8dqZDe63yT4984LJZxz+zuwy1azD19lU6yJm9zXx7IJeok9zvsAb6z5m32N3zh6pMyq0ftCumz3Frp9d1DfWz57Ir53e7Ou6sI7i1VnNGT4/e75JZpv4o4vts7OXuTxOh59t1gR+y/5ktr1v1uwNJtN+CqzX2jY7wAnfi+2aNvms0yNmpc/fNnufa9tD9pHZx+zds+vU59nmTIHN9vHZbfaB2Z3N3JW9Y05z/SvncvuTub0Uezmnval/Mneg3Tl3iH1l7jD3O6LxRZhw7hj2V985d6zdOOf0ibn2/N/cqaxvXDd3pr1rzuHvnMPbOcdf575qu+fa+kNT7twV9oU5h69z15h0zunlczcuwOq5OYe3c72uriHg89zc3fa+uUV94f65Wfdb3U6/3/5s7uE3fCyubvkH1885fWHO6QtzL9nH5hzeOpQw804/mC/tbfOKP3p+3ukI84pHnp1fZp+ZP9zePb+HP3D+aBPOO118vj0/Nn+C67PD5fnT7Jb5M+zq+bO0dm3e4fG8w+H5i9zvUhPMK9Z45/yVds381fShvabP/nj+Jrt9vtO+MN/vfsP2+/OTLv98A0v2P7h3fg/def5Ru21+rWtXs0bM8dn5F0w87/Bz3vHVW9oy/5bU7rylto/f4uT9LQ5HbznYhLccanyzj/HM20xzCtC73d9fMp8x7zKnmdPNyeaz5g/cf39o/tj8vrnYfS8wl5ivmz8y3zbfNX9i/pf7XmJuMv9o/sx0uO+fmy73vcz0mZXma2bQjJvlZtLcYa4yPzY/Ndebn7nv91xdqXnM1fpeU7nvvu7rL3yPMG91T1OjXR5SE5uDzYHtlHDJ1zfvM80ZP/uZ2vy8y/fG9/2mZX7Z5O77NlfTnt/A7G/2ct+93d9g4ftR83aTmdIkRpFnpXve3Dn9x6U332jJtyllf5f7ANNgxnvc3RvfT5u3OIgVfH+Ost7lfieZ3zAfM7/ioPopcwgp/zef482HTSPlZPH+mpEk0Nz6GytBjjW/4L7NmVnN9xPmneZo9/svLuUE8+tuRA/nt8z8Ir/D3PeD5sg31fTmz0Hue5z7fcT8qvm4+/8DQHHjHrBrvu9yac3IeS7nntD223/1OWzJkze+pv2mcu/lRmJxfN/t4H+4G9vaYUQzrqX76m/sxqQZ3/1cP5q/Dbxz19+3ufR9HJ5kppEF+7hWH2sMIyj8acaowTj9Tci5n7u+xf1+fo8R/ozDyFMdFjVfzdIf6Mbh024km7E40MH9Aw7K/5HPKQ4ffpO7X3cQ/2WHAUeZoxkXfd7L9TeMdNJfYfyMw53mq7FucFxc5hcdRja/d7rxeKfD2mWm8Qr80pL6DjGHmDd/DnDf49zvYIdFzTi+YRULa+rLmvOy+ptrqNMzm7PjI8OpswXnivmcnpmd2Fyr5mzdONdZlJyEF3L6Vs45o4YTa8OTyckpb9kNXD/MlXSzvLmWOo+Yk/AizkBO/gdl/i0pnHSfnk0KZ9bVR9EqThDNeSv4Jte/JicnaqWcG13qlPdvkcK5Xh6noGWcwJVwtqDP6aPZBvJz0nDZtD8013L1efrYZQ1+eqSo3s/Thl6uf0dpx5D+g8uaE8OOpu+v0qrjeXol73ICqRkk5UvNNeak0ZiWF/dRC+dbh8torc695lTE/L9TckQJ27nO0y/OMUs4/SzgrL2QU75D2lmklLAvPf0EOb/NPWexe/Qu4STqeID0t3C/itZyVqPhjLXoclIYl5TTBZPbaK3afxh5Osnzae45UzYY4ukdtF81coppeQEp32M0385b3+V6FWX+HPdfIz+nlBswLeHMt4DxLf6e1j5Ezq9z1amGK+k7ZyZHnDrvcaJrcAd5+rgK/tdx/zlGUOfNcyJhfDAjzvl5CacrF/Q6+XvyM5o+uO1/jHfBMX8TNYLb/o9Jf5A+3ktpDYYYjWwABHxGPNKJh0A4gL5qTv5LnmWsKTkE82NOkQs+BPw5TzP9BWrhPL/set6apRd/QTt1Jj2YGc9QI2cvhhV5OGHTX0+ZorgnqJ0xin+dKzQVApMS3E57eEv4w4m2EScXxrQ/ex95Jmn5R4F8FxCAJ2Tv4H4fyqSP0evkCejFBmppTjT0oLJIJxcWKpOnCecdlq+RAl55Z9KSYVK+Tvk5eaC+7EjSOXkxhm+EW4DMY9QOJPONpEM1Eady1vQu7+MtWpsdCpSgQf9mymRMC2BeJPQXnM/fwSjThhicCaH3KKScA7h+jfz0KwaHC7CiupT8GilODy3EZzgFtTZcgZgP1cecBJl9m9IOoVXLSJ8CApwcGf2A68NcW+TkxPT6Lu45fzDaCWyBUgWmlWBRTmsD+hvr5F84fCkM0bmV0K+3D1Ddi1G7nZRrSDmYK2cXe8DfQ1IYTpxnQ47YB/8jTqSOGNOQsfbfSe1Pkn4UKZ3c/yW1TFDCb3H/GLV8gfKPpW3vI0UnLo9yH/MU6RY9whhx5mwMZ4g4szeCb/hrqVendUIpATSYIxfS1ZTDmcDxPdROO2PhxhmkXEbbziFFVNBgu682mFNoD+3UWAR/wlvQfnQ86Zy27O8mHZgnOygHzmwkm8BYbx33N1K+zz1tjr5BTlobCFbbyP9r5EGCeCso/3yuGk1G34MbhF/hqvO/Oe03lHwBPiHn1ObwlgCZ7kELPnI2gxNm8CsPLcJDwnpXkAc8T8F8g2T3wfA4pSUXk/Ot3PPUA/NDTs/M4cktUlLgWTKmBTgc87TYBSY/QQkfoXz6VS0HMnCkFNqpngEa8M+Kc8Az8hc6wxqYFzdSS0JrJ8lJ+SVvFcjTEnrMgGEO9NLfp3Y0jfJm0q/hXai7kGSB/yfAMAUTCiCWQMv1eeRv5Fegk0vTR7geS8lQUIjkrb7Lu+LVD9ASyfGtlAMuJWB4C1kWX046GJ4hH7ON4M9BjCZSO4XzRJzg7MHNIvihhz7mX8sVrm4+BQx513sXbfsaKTqlnJH1wcCQUYvP5S30Ex9p4iHTY/TD9Dze+hBX5IUH3XlwqvyLXNEcPPJ7MTVu4op2Z4CwB4Qr8ND7DOWPc7+KnJRgBMmHuH+Bp5dSr/Ql5IKhtb7kUTOyXpt2oAif+5QTXWNkirecnMjZ6CbSb6NMeIiBFsx9XE8F8uio5p94S3gCRzLoSwE0mJwP9BjlTCfWgocxbUB7iQ3ywqAX+bQ/30AJf0XJ4EOBPE2uAKM+Q6su5C10uWg5fUEnz7/LW+o7Omr+Vu7Fh7/Mu3/HVRrdU9SF9PQYBV+8XZgG14qg6+BR2gA+x+BDBvTMreQX/nASdvQ+7kWtaKH5F8BSQ/vBqxxdK+bk3gxcStEHQlHfWeQHGmYjKeJvn6AEpIOh5TGUntPTCMwMpA/T3wTazKXjoSUaJGwOduWcRRxIL0InD47gHs4ZggO15Bp8LwE+CZphzGhGcJgQaRsiT31oMAZPMs7rrqTlwhkCOEPBeBXQZviXtOQm+gXml9RboGfGaNG+tC/KzOF+kSCGJpyexRX6iu+khZKMjEsO7fhw71yaP/K6JRuQPAU4X9GLEi5UNrTmCf4REK7QuMrfoC/f4wqPKpHyJVAqf0wvwIEKiVOBpdG5jCD5U9E73LIAz0sosdb5z7eRciXlcP55qDPH4Zkxp50nX+cp+OOhn/hojAEypYJePHSeQroBGBsBbe8qasfqzKk3ZEQSoJeO8hY4kCARArSvFNwo/4F64ZwFOrZHegF9FWiw5btpw82UDx+IKS3DgouQsyV1lXAGAycMsVki6C5axVPua/LnjFSKPC0lLyRNcvoiDgw+x2B7Rr3lOGOEpeAJS9GNxam8Wd6lHIM08UTFjf7ji3uHHyQdKyODD2fgf4D25QueX6Mc4fxqUi6hXslcNFgDJ4zR8WKwOlzJW/CTmh754HZJH5MQGKJ1+HgzIuCcIJGj7/Pul7kHMuErTY3SNAqkaiILGrkQ8m6NZpVB7xn46aG75miAJdyvoLUl1J1iIUaCA+khtnYlqjmJNqDT5mBagO6Uo4ckwKeER1VIogS5VkpPoNfxpygfiMXIkQq9pUCy1OgVBS0ssM4KNMCUkkMwKuqlRmkaakPDq41pAf8TaSGQTNF4U3hRBoQzyqmhwXyY/II/PK2At1ToitEunqI15dTYgjfW+EZKRqfkvob6AtlN1FXgF0oYhRrKquFFsbBIFh9tiMGQXL4IfDulzliXNtWiL1BrC06eYa8VQ7TtHNoJ58wfAHvJmTwPNCSnsFhDaVboriG98+A5Pu1nKWZksKM9cLWUddNgiCeNPUcXiuUvwpKtjgO2QMaDq4RwpHQ59aKrVMiFTJi5hjzHU++FtP8BrmgyAfzWX0cJ8DQfTdWXBIfbh7K7JZvQCXPyRPTRh4ojNIrsLkqG8wTSecifwsl9WUBoNTnQDuFFCbp6JLsD3pvIPvoL8l/OteF+gQHbPVk0r/CW2gk3y7GJspyn6DYeHoBQeiB8KZIEkXaBhIrlfTqBesGKFB047aNGLK8E3pjeRV2Mi4/kSul1JEhi4yfYswF8L5QeCLeJ0Hh9KDqXX5EyE6Dqo1eXSI0SuVMw1sUobcDeL9FVUnSMGMjHP6a/8l7CZ3K4UEQbDDI0hZMk8ISakpMI+IBF0Ri1YHVG+DZjWeKCP/q8fyYpz3KVtkAvIo0d3KAkf3A1NcKFUmRNgr4X4R8oaEMKfZVYBAlcKARbYjAqhA9kW+jL3tT4ZWD4Z1zP4S1kjQctJ/DVlJIz5L7PeHloXBE+hBRbIIYzhGdwj/YSSwrAN2JosJJfhZbk8qk+Q2lody1gXiHfK3h4Icl7BO1ECyqg04jSfDyxCd7IVkweJHv+M+43cUXjTXQP9MKP0NNGeibiJCn8KpO9+UHw4Xhyip8DE4Mu2tqXcrBJU/hMIuse/SpfyVMs3xC7LL2Zdq6mRrhxADRy5IWHd9TcRo2iKXqUC8LfIgXKzYQJ8KIYuRZsp8zf4V08vSHYG8iLFfIWFm45SU6wOoX7JZojEIeXr178QfKUd2OwPRL+iKet5yovOjIiwXfkQZs1GmOAhK3g5wk44KNvRPi+SuEGfEB2hA//jLhm2JUZfL5kXCKgF36U9tOemJK9Fn2BNhP8HhFcNJKmJNwTpkEX4UG0XHQnmwu8DbB0ctoTw2MzcDj+Pe7BwEq8C20qBfKheiprETzJ4Iohmq2B68b0IgKjAmnaQDUVH0NDq5AvKdgboQOHaHQZ/CpEN4gZo1QzJn9O++l7LP8q8MzlaYSTVMCNGYHALKNk+TPR5ULaHMMxkmZEfPnKfPXxPOAszyr+kwKpl2FhZUAyw/OcAecS6BWNbpnId1fBWwKsqhZ+2go+n3Ffwr1rNKJgGX2hhSnyJZV/G7oogUyJ9w9fUGjoXYS+mog/4/1OsIgD9Lca7lrdSy/krQXaPhysBFcDdO8AX72PPpC8TvmU6YHb6X+lBNqZ4bXLaFsGJ/fxrSW0PMHS99EEDHgeX0k5SEBf8vfDlAb/L6BrH/2h2E697+GK3ynG0k/lk5GX5lSeAudI8xRw8lzjgkfIw8sRksfDz+l/hhrlTbqfK9pvAA1ig8ch+r//FXIC1eIo6gKelWYuxOHRbeCWcQDEQs2MSLptICeYH/OWh84QAPlIfhK06AhpW4LtvnwgQKP4A+7RMGPZqrxbaO4GiROjXdTwqAq+WuH7koe/1ejJqeYEC/DQgIc1FNTCoozgLTVyrcb6LsCHFnZWS7QDt/R/m/aAk9lq2iN7jZYkT1GXuNM2IPYSV9G45gKoJRFWMKY10C5CypT9gqWT46uMgU+BRM4kodCpasa3RuoZ+FWJnM3EgbGbKjhViNQLwYQECeLjK4ib0kIPXlSBvTVaQYoH24OuA3kGZPcN03d6F4AJCfw80bwSMrpgFBJaVWKzSEIVd9OjppZAnvNMshU7TtKzoIXVfrQKyyL7KE/v4YokquBv5QdpCdRXy0a4mBTGNILKIvqYa3YSDpMAsVIjBZa2pDsB+VqWtbwKUG4oexltE0ka5+gbBt4izSGQjAZnIuiuwNoKZdVCOwZ+kmNPBdB+gqZdIj19rGaPEY+vo0bKD6CvjLn7HK0glO8XnbmA7nJ5P/DXFZoxkRV8DiW8SJmMaYgOUyNHDDpVQZtbwhZkpYdmUoH5Kb0r8Bql0FH2V+REz8x+jxGRDkaZgXxx8p2qndCO9wjwR/MP8YJ62JLei/QUeRrg/cjww1dgeAlFRPLLLaflh4E5HycP2nVIC6Un15qhgM+0llGCZt7xNPqaTcNCCSk/l5cMv1NB/hA5mPXSWviMj4WSIf3D1fQLmirQtXKs+0z+CuysmFEO8U+mcPsSuZNhdRpRh3CS8arRWNLbaSdz3ymY7DMKBTwnlY8dKs6hxwSpUUhXoQ0B9nsKB4tpc4qelqgc2ZXQYyx/mjgw0rMQV8Qayl6jJXhuS+g6Rw8s5WEW130v9VJjDKbFDzOO4JiH1h1oNgHI5GjCHvRYQvXxO8h/PVdGM0b30Gy+t5b0T9ESqNVH1odgVyb7SL5rPMMp1BT38BZYHcv3Jc4g2xm8iuXb11wh0j+BsmJkUIa08tBefFlzyJGYKJoQios0v/YF2kZLAvizD+cM5JOfoe/oq540ecaxQMfwGYUUnpBqXlvzCFhSHtDw1F/aViPXAmmtYHsM90sYuwBpHvJuDN9LkP7eBlqIlVRIWq0mpyI6gF51H+nwdg/dI2dMM0UUwNNyeb/pSyF8FheCaxXQRYv5+koRRECpYqxraRH4fyKehuhIFX0P0aYqtOVSnBP4G+i9xMorNXOKlC/A7QpdN/oV4CZfKLpohoZfML6JPE7y5snLDW+pwJ9UVrAkHbyoQBrGGygHaLSY/S+oPRcPR/LG8hPCYyPNN/0SLcHyreT3Y0Qq8tdgYEtzPXD4Ap28pp0Z1FTS8hbjnoJppTzG4FILntCeHW68eaEHRbTA2JyRKoQbaEolMqWGB6a0v4QD1FBWLK813rAauNXgZM6I1PDJliIi4DYF6S15/uUtlE+V3lXQbA1FVEirkvFtYXFkg+RnFCowp1bsELpNzojUeJlqIFnjyYzh+dUySpPuJ8v9A/Qd2dqC57SgC8VslOB5rdEh1ssDYi3aX6JxFbSqRtct0SJacIAWnt4KW7VF/FsNXpWyR8CKFtItlXatOC7N2jCLnSHLArSvAjsr1vw+PrdIc14NVCOs48hQlyePH3w1phep/LdAKZBnif5WmjV+C08VywSOBZrzEn9QTBHlBPuTR7YzfY/W8bRpsyc8jJCAAU9TKC7F3+JBy9nz9A6dJ0Fni+APgfRkaeBgUQKnzeBIHn1J8I170vwpM8UaCig54T7FUojAtwL/Tw7mp+rR0/Qd351H70Kw2lPcF5wkUPyePHVgVHAjbZbPFomWar5+OXmwSnx0hhSqKdEzY+zNFD02PIQyp6lFMkJWNt4ST5GNSPkMyyJiRiOD1iJGLZU1dzNX7PFU8XVw8qrBSUPbgrYP5wPcg6spcqegtBzJW4i6j6NH6Mk+Uru+mbc0X6wIQ+AQgpkxnC2F8wSSudgsgWaf4VTxJ+gjvLFiFjKRXEa38fGRlnCqGH4YoiP5iv+EZjPN3iomE+pL4fyp5m2hiPh3gSc6asIol6spTe1B14rhe6FixtAfSnF7cWBxA+ymCE5YfJiWa6Ye+qruphZa6KHjZfLDo2l7tD9F78oO411GPJd8hzt58IRInFDxNug/mTzMmrWBJyd450JwOMfD46N9JfJZoUW3kHoR1lkGJhtkZUprfSDWnn1TnKFmNmlPwgxvAmeOZ2mtImCpK8SyC4TbUF9J21JF5cF7gwLckKyBkwtPMuRFyghWf00eeSmlFyGhKjw/4btJx1Io5XtXxA66VoE2VSJJ/ZD2g2k+Onwurz7ahY/lUoD/JT7SWBqF7H00h0hxv2oJNNKCh2RosBXYUtLCCC00gFLkv42Rmwm2WAYfi9GCAmE4UrsC5m0f+53gCTifwrUqqKlEf/PkOdRMNDgTwZFy6mrrUUApZ0QqRb4JHyQZ0dl87KaM1haND80XFw2gskrz2vIhKxpQ0XHSkTRTz5xdpBlq+JUPfgbAxwcPK/n9oKz029R+OL1DN0tktcGjEnhUDmUVss4YEe8u2oYukUGbAZiZSJ5q3k0+JUlzrG/JrAjsSrDvAvmOFNeKXItFF2gvGVI+R4KE5PSghVwxS1i7GXy1UMQpXDE4iR5p5gLpmarlb6FV8kwCw1QRofLWIlNastTgh9UXKAeLNf5N7rEO0u+C1Yqqgitm+MEi8DzEDsrhTjkyKyceIEKC5NLV/54rEipgZjMAwwswvICn5WvIg5aegideSDlItEi0TH9DYTv8LenjLebpasWkUXKiqCr56DS3KK1DHkV0vPRj9FF+/mO5l8dGXlMwM9JcHvAM5duBS3hA1Re/UtQfdkr+x5QDjUT0VPOkCbBKf57SmI/zDiUnvCsCqiGaW3wz6XhKDb6IEJyUx8kIK5C/GXwjY4w0wx6oJcdQC9pdpblORS1iNcRouSF+hlAzm8zoefj3QsENiojglqHiuuEbhfAcSvToXQhsY3l4kD4JnqIQjStmXGI4ofwAgWa+Gmz05XVP6KOvKKZl1AvHiCVrFP2C3y8Gbok824rPfwFYoUXkYFeBblOC1ZnsbqRPibZcPUL7H6YuNM9cPtjV5EQGJYovha/68I0CTbWliGJJfCBvoNMEfEjQvVtgaQvZFMsrq0g8eGyAHI/gpcUPaQMWcajZc0quZffJOyqvOB6Ygqfee3nrYMqUT34/rgltZmQ9ZgeqLlJqrgdwxR4MsAdj8SKovkDuKJ45bFoemJD2oFf4aPjel6hFtAC9BBqdhBRsZF9rKMCECFvAw16oFQEFviXEPgXgai1/u/AEyOdQU8U8e/BOSkZ6FuB8hU3tI5sqRr+FlPF0lTaC5yfAe5CjM+RwPF/xnPKcyJ+PjhQowkox29Kf5e1EJ2n9Jk/RCWNFidC7UjEtoh3aHCA7UiRXfTUwBLcLtN9C9im6SgEXrTXjgJep1rwn9mAo3QMOViqaBepIkLxtTUz4oBgA+phojg+JmTFfmTW+AiNpJdunkj8K+6ulGHjqraHxAq2slr6KVhCqv+B5Df1m8MASmZ7Kg4cuHSJxSuBQY0HUy8FSsKimxupoYIKkKBTzCXwCeYrQwEutThKfr8lPC0NZBMiUSnKw6VcI7sUxmkwIZwiEjdBdqEgP0iv5GOV1kW6GPpwqls/nrR9yVcSFItUf56qVJlBHKf0WnlYpqg2IBVo/hV5aKOJR2gtWWKH1X1prgGwtaUkLf06mqANFDh9NOZdSL7gR4vkskPsFvvRM8yNoa9HZjDI4k4oXIeMi+HCKrlhrxhZ+XjGyKRSUaFYCblnJh8xIlYrzhyOlmg+VHYqW5Utm/SVXLKAUWeAzUgn6TKa5RfnQsJpjQUnr5rR6grZ5N3MP7dRgUag1HZdRwhH0DpgX5C+h0FKaxtnkQavXPKYHzWbLSTmeq2Lsd1ALNmmpaAGwKKHNKiGANkvJWeAcwMFqRZKQkog60HgLNN4CKylEZ6g7aA/442sVIZ7A4HzSl4PP0HX1q7RHMdhaI4aPKP1TUjQ3jTWXKl4XDSFtMCeURpRAjyGrUTxFW/0t9/KKaAZEawyRTaVWON5MTs1iKEWcRN6e99BC8WT4ZCJuplk/RV3CkfwNtBMNPJeXQ14gRbKBydU/0x7FzskHrrgjRQZqfvZJ4IDG66Fv54rCUqQKMjSjzQFjmv01T+WpO4cyNbeO5y1B2ykY2UpQFS/VbCx2UIjvNMHqTGh5TXsCcT95byQp0NnSp6nx25QAvy2lCaincKpS/kwwzT+Ne/Ec2R2KnIeLtmhbCz6WKSbk++Sn/T7pvmb/8eoUWkOKllXwNMWHEKIDp+Bk/De0DSyt8UrVSKsCHSAH/rlWtcBnAiygUvN9eir/CfWmWEyBZrLAtAAdu6VVKlox+iHuJb/AnBJeVwPbHBqvFZGLTC/kV2QsYo3axfRasY7iUZrNBPL157jHGsql1dDTQvOt6Est0RG8tF5Nyh/SKvnlxJ1u54pWEIvzKI4OaJdgdSFrBWlYP0F+RaQgc0tFRMBXM/TPCtu2JT4MLeRAu8KWrxTxIr0LCAfCB3hpdgj3cKFQURBI/EJea60I0PqyP+VeNiC0H0pWosHWT/O0m5zgRqkoMvlghSeKY29saiPPRi5v7R/xlnxN4gO0v12OYl/FAeSpQw9PuJaaJ1JUg3xB6JD1a5R5M0/v5566fCLK6mWk/CLX52g5+FYhoVpvIwX7t0D6p/IVaO0AUGohbaXZSltur2gIKBM6zc+iHLixr9ln+TE06430j7W+AA5ZyfP/3yhBcg3fV4rFGqHJRHCDGKs5kmYCj8oU+fM870KnGXhVoqOGyJEcCVvT8kAyVPGZ+EBC8WSwt6VIP6I4koZOE3lia7BUM8WKMM+wKGtFD4JvNRpIorUG4o1YDTH+pURxrbLBqTdpLMFUMUUFtOnJLwevKBVzK1pGYynO4anWBiqODr96hEUWKeobbbPUWgmtjUJDzgV/6Rj4Q3xa5cnrCLcJ0G1KxQPAjRP0W0+rdzX7r7lF9NhAcx9ae6IIRlkNGmWtKdCKSDChxkaOwLdMPlXaGYM/CVpxBkUn4HZO76oG//32/NEnKe1RUqRZNT5qI/9eqAh5+eGh90wxvXiHYjhnrZZo7Kir0lwbPDyDM4RojDX07jcYFWotWEmcCR6A0Iiy8BKUktrok8lW0tEHcvhSpZhJOGElv4ckIDEbsXqE3zJQbDbUl0rmgquVolCw0Qo9VQQ1YxRq5hEvSv4VSiNnIgmI3pJjKYd4qDxFJTXSIVDcQq5owGkgqfl35E6qqAYoOhznLazF/L3ca34BnC+xhmKtNtLch7w6aOmxIlEVIY9mkikqAL9ECkwyvJoBGrsnLUKRpVqLKk+p1oOfQwqRHgEpAdQa4wsqeCtUjIrsfbh0eCCQQcuK8E4E2CMlfs5MshIplp8MHLQ2B4wKZSFqncjvUPuPgJ5ipOGNtaK7FckmKYaVFIO3FfRVaf4OOs0UOaM1uRo7eGkNB6i15gK9sdYMIBZHJXt/Gem0tiZPS74OaXfyM8vqVIyi4nnw47UqSmB8k7fTC/FhML8lbyraQqnxZQQrYfUartqXQ6sb4IS1+Azj6Gv9FzD38H/6+DYjxUhrHbQ0MfwAgWK94AM+7Yzw41XIHR9OEmrtLZI3UeSMLFxkQUVPK8XJg+ep2iM/J5ZsjOTykI+xZlK0GhQ+bOR7l0cFbhBL35A3kjIj/Bs+FpYHVww1390FDOEneIFiXzO5shAVKUHOiDILRVrCMz3tMPAM9Ta04AvrIiRXrj0ooPcMmehhu2V4Sn3FKwrTFKcnywvtPUbHKMVnNNsIveTyQ57GVVGdWnEmf5H8KooHlm9QEX3IIE/efq1zB09CSzlQdIKvKUBORWiShfAZDhzz1CM2IwxpCdLE0+okfM4etURaVQ3d+eB5hr3sKXJbcYZI8wC68JFfOTI6lv6vGTRREDy8Bg8LzVP8LmMqL4rWWUhrBW4trahipFoaTc31w39qqKZU38lZi4rHKFO+NSRX9TRYoZUgGgvJF8WiY/NWyKkKDlaDwy0oKNSMg1bzMSNTYGXE8g9rzY52ZkBqlEQylPS32sC7tL+Gu9byV2hdEpyqgku3wL1MK9YZhRa0VmFV1VBNS/4H5GwuX7poTWvBNPeqiE34eSkbR5o8OROkTwg/iQUTeSNlWUgPpP0BWFo3NOgjg0Lt/FMrluMy3tXaHMW0w8lLxfUpIkjUBNaVog5wz2jnHEWMaycErVsRbsi2krdc6/2ZAQy0zwDSzcdaycjpo5mnRMTJm5doZxvtXQOPKrXyjvJLeFEKT461sweYXCou4hu0RKv48ftV0Eh+Dun4T0o4oQf2plg9RSOtPPWlgHun2rvjSaDXcNeIVa5xDvfOtM+M5puQCNk0OcGiBPoNtBeH1gcBt1or+hmXWJFmsrKhLx88T7RuUVYqWB3JZ4LXt/okoyZPjiwvLN8IaRhCd5XimsATH5lSSCpJ4iumTjs2aF5JkBe/lSUCfNoRKVClRznyOiZgqaw8T95jeafBpUBrnz9LumK30MGw+GJBLGTs2Osjwt4J2EkjkhXAaovYKN5PK1N+jnu1EP3faE8eWUz0NJBNpD0ultEeWuKh8XpaN6ddBbQ6AKsh0B4vIRBTzI9iJ8CNWOv7TqeF8MkSKstkj+Arq5GzqfRVcR6t5YGmMnhOC27cCigHaEeaB0fXzZFrrcYaTdWSUvM+yKYWVlKFp64FTbVkIUIXig6t4O0t2Uriulp3g15kNPraWQitsuA+0XpS4JAhTWq0L09rKhUnBgwTKEi4nQoa0lH3B3qsBdPeUy3KiZ6idtk+2ltAsvJw3sXnEIKH+c20QRGMmh+nrgC4pYxFolmtF6gLD0kBpbRk5yILsn+gFjxymq3ItZ4X34uvuQZFM0q7Y9QqaKfWTJxsVVkx8m/AsaGasL1HDdp7JItS6/SZu4m1zuht9A6cb2m3B0kEtK98OSmMtTxLCbIsR3ZUjFSqXmitgSI9fkDJtDORzw0+7CNnA7CuZjRr7b3wU/LTu5raK3TmRCtSD+Sp5qNP5x4fSIU2GGqdCxp1yVuV1nApTpXRz7S+WDuVaQZc6/q1xlxR1tqhRf7kc7iXBg5XrLRKGiqO8OpE8qwqXkjRXECvli6KJRIh11qCM/cZkrTWrhSKe8fD7yPFEvhGpNgDcsbI5ZbWF8iDCk8u8T/XmpWQFYZeEb3KPRD2FPGCR6g+nhStCpSnCIouKMeHQn0gX0gzEd+W/oCOVKwmD7CN0EUrPIeh9s3TCnetj8PSyRUhoxVYXCPsiFi8XTMCirhWfBQSKsEWaMHzY+RvpbkhSsu4xmBvoP3TpJ8gWUL6kgDJSlbSTiCg6FCtsEDzT7B/a8X+iWY1jwkfSLTrFCObyXeNTVFrJYtmVYBnjRz30VtC9KIQf1REfxWP5MkrK9mkPZHk2YPraiWv5IVGUPMvmmuQZ1uY7ynqAC4UgOc+8QPaYw0dKcL/FrVllmJX4MDyc4bYO5pzl8+h0loteu0zP+KBw77iLcWxmYnWymjNwsdaewV9BfBwD7+Nxyi09zvS/nJqMxzMyBupniIdPElY4K/4nHacjNZfaJ0skPS0YyGaiSJVPGEI3NsTl5Olr12ttCcMOqEHrLSaMmA0PSgUv0fsA4cADS2GZs0WYKLY+OWkjJEC3/PhZtq3wXuNdK1Aoe8+EA6wRELGMWjsLF+xtanWi2nPAXwpEbTjKxbiBErT6KCzRZqdlw9E82KK5pJc0x5EWkeJZPfkXdGYQpuJVv9przNmnXzpTt/hqhUNeKs8aSaa3YBGfPDKU3SQ4mq09nw5PdK8Axp+LC/9hsuMdR+eKsZM6yjh+SU9SrbzVLs5iQ9otythr1ZIaf/G1eTRegSsnkSxcw/ylHpzsCsFAjU+rgK/fSY5KLmsGBhFAjMulbwKX6RG8KqFztACT3Kou9RMhKI0wRYfzMmXURoctcQKKxVtpagbRVfKi8Wsn2zwSHtsHk9+IFkdT+30q/w493hsWr8KfBQb9jeU0M0976bvIEVr/FPuNXsCJ6yxxSrtD4a2kGBZ1JoLTihZ0W74Eyp56bVPkVZ5CPPRY2OtCYW+EnhjKv2QkivZkkC4dQ7pmkOf5F47WshL/EHuFV2glUpq+QbK/wLXjCsWRwpvTOFacaMPBPKI+shcn3Es9BTsrZjrqfCi5Fohjqc0bySsZ0Ke/k/Ste+cooi1fyy9CLFoSlGEotZFs/j6AmznsounR/IUvp0MXGb+NwFrYAEAeJxVjr0OAVEQhb9drM0WW4goJJKNQhQqD7DJxk+BRCJboBOhWhRotN5A5QF0/oIHZAyC4n73zLnnzgwG4DBmi1mtt0LcaLCYkqIkfhEr6Nc8/EoQepQrHaHfDPrC8On4YacphPsdU/IxVUKpvtoQxoeD+YhsNBtGeMrCYrqc6BSDhBKloYxj4ZKnK5ut2LxdW44l6WeqLerjNfQ2xcmI6ul7T/+YwpzWOa1jP53SJP/62prb/XmOdlixfm/4muPKfgcuXLnprnuOnDg/ALvCJMMAAAB4nG3Ta0yTVxgH8P95WyiX3qStQItyE8uQUm0tdFymYKEOEBERGd7ZRfEuU4eTDd3cssUPfjJ+8YOJkhgTTDYdbnNRIuIM4Tb4QK3iBihgbIWXSoFyOR5QG4l7fjnn5Jwn7/smz3NeEMyEEuDy8TbI7ImAzalsY2e5xeAgqEMd5sbrk3dn/MiGYPYdnKNa5ag2uW9Kxy9aZ1Ic4c5IhbfDJM0eab1a3uqWd3rkD+TyY02KK2OKRpHymlF5b55SHq5qmZz/V1Sw3T/kVr+6WaluGdLUTGp+D9HU8Zr/qEbop1E0Lbg8tVDYE35VFfHnYMSEIPKGPNIWGNk9HXVDHH0pJtr+PNoesOhCsLZBpO2XxEr/jq/u0dUM6K5qdX84Epq5BJ7qayT63/7V9/F6p2sZHhpqgw32CUOP2+AIMl6UGBtExibO+GTK+GzEVKsw1apMDYLE657Eev/EF66kBj7p5aC5C+aeNnNvq/nZy2SbLqValuLpTm2RpXYNpw5L025K0qafrOzqS+/vzTg3mlHLZ4yNWK63Wx6PWwZdmb/+k+kZzTotybK5rT/D2gXri8nV093Zba7s/tGcttGcDmfOwEjuvcncdj63Y3BNhzMPvWubHevukIIx2/r7fGFrfZHNVcQPFz9sKjnbWGIb2+Lp3Ops2zbgLL3cXnr3aVl9Zxml+4aH9p+fOPj4l/JHLZCzvghm+8vR7Y8200oaQCt8Q2myKIxWiDW0PEhGK9XjND0mZLpPu5vmxbnp8Tgfmh8voxZdGK0y8jS2vJBKKzewG8G97TRO0SWr8BO9NoRgTKEQWXOcwlFkM1/hJCq9juNLfIMNXuuxF2kcQRWzyWsrG0UoxRZsRxn2oBjfea1EBb7FNiYMmXOUYycWM7uRggyvRViHw7BglVcsljElSMYJljmM5XPosA/7cQAmxMCKeBzyysURLMVnsz7G16yuB/ERNsKMKKTjC8iwGnns73mfElpWo0jkEymRcXizD0IQ4SAlQvgSPyxhIpBAOCIgBN9jHsQIJL7wQRKMSITkzaqGBguwEHH4gD0rImIiRihCiQ/xmekxCYAfdmAtPkUOPkE4jkEFBZGgAAUkkPiz+uzC54jGZnz4P9bgBxighz9WQMSG8H3sIzNrCLsH8xEA7hVG/zyPeJxjYGQAAQ4GJgaZ4pKifCCbsSIjvQQsCpLjAZMaQHkmsBjDASBEJcEyIMz4jukrIwBuxApRAAB4nE3NPw4BURDH8e+8589i7UooVKJQyRYqB1Cp1KJSbNiOsMkmCgdQqRzBAdSiFudxAG9fMdH85pP8ZjIIQoAFesQuE8xufzyJY5HmmRik2Ga5CIYBY6bMWLBiw4EzF27cefDiQ3mCNAnL4d0iUof01W1G6oiJOmau7rD2dn+lS+ptXFje6ookztbZSFWuYJ7a1eSrrpulOvjbadgh/AD+shM0AHicY2BkYGAQYGACQhEGdgae/ILiKgYeoFgCFDMwMpSnZ5QwnGBgYFaAYKAYIyMzkOIDYqA4IwuEzagDxKxQ9gQgZoOyvwAxO4TNFAFkc0DZe4BsTggbZC4AI4sJ6QAAAHicpL0HdFzXdTZ6e5ve+2Bm0HsHCDawiGJRVyzbsRMnjuMk9kpeynvxSv5n5+FiICtOXU7yv/dbjuM4lkSRokSxixIpkRIpUeydAAmid2B6vTN35r59BiAl2WSs9ftozWA498w5Z++zz7e/fZowHMMwAiPhTY8VMPQPN7z9E/YudgWbwrJ4B74V/yr+ClFB7CKOkVbyu+Ru8jh5mZyiAtQWaoiSaB0doLvoHfSf0ocZD7OK+Q7zCvM2284+yv4m+ydsnPtL7gf8k/zPBZVwRvWIKq02qb+l/q76n9U/19Caas3XNT/WfKwZ1lLac9oRbVxH627rQnrC0GG4YfyS8dsmleld83bzb5n/zPy8xW35B+uLNsxmsTXYem1fsP2h7bu2l2xHbVn71+3fcRgcf+T4rmPOkXfqnBXOf3dZXHVul/sdzx971d6vlTFlF33Nvr/3/Ydvn+9Df5P/i/5/818M/HbgzwPPB46Wf698ruIbFX9fSVV+r/J01fZqtnprzaoaufb/rt1TV1n387pQ/f/V0NDwx42/1VTWtLXpQLO3+fea/6r5n5tfaX63+WLzSHO4udiibelp+VZLX8v/atnbMtPqat3Z1tP2RNv32i61TbS727vbn23/o/bvtv9r++72k+2D7XPt2Q6u4zc6ftLZ03mjs9BV27W267munV0nuqLdVLe9++nuv+7+Sfe+7ks9htXu1X+6Jrj2T9bVr/vzdYPr0uuF9eXrN6z//nql98cbvrmR3NSw6TubfrRp/6bbm3s2/+bm/s2vbb71SNcjX3yk75HYln959K8fvbi1cutfb728rXLbY9v+YNvz265vb9m+efv/3B7aod7h2bEK/tu248s7/mjHX+34Www7Af8tv/eRmB77EoZjX4MvngMrafSupPKamprysrKymrq62urKysoKv7+iuqqqpspX4YfHZYHygA9bK75QbjCoOY5TG3SMnEqkMrlCoaCQdCoWiUQWFhNSEad4XbDRaBwbG4O3lNPpTF25ckWntfgaOjo67LhSyETCsWwqn89ms7H58ZExLC7W52dn8x9/7HBMTX24c+fOD6eOfDg4sZTn3J2QWiojWaglFc+m08m5i0eC5exKklOplEySpJRJZzJQnIQVZYylcxkiLwfpYqGoYFpocjSaKRaLmaiMMxqtRmApilKyaYwzGo02i4ancfhZcFk3TtCNE3TjBN1oBE1NQ73fpjEaa2qwLnGAoWmMxXE2qOFc/2uPd+j2j8LhI0ewg/DkySex+lCoPqjR8/GFpTwGOopEsBw8kWVMKhallfIZKJ+B8hmMIHCcwGu6cAXD6YO4AcNDOfj8wFz6LhzH8PhB3IjhRA4vLOfifJ/JhfURTdDK5/GGUmkXc3hxOV8ASgtAvgDIZPi0THUOwojkElbkcj72mDNo+LRsjPON15F8wop8drXaHjR8WsZYVssjOYUVOTOzs5mHyOrrgpbj7EFci+FDOZDoIVIYSlLIJSnY+1L4oTQ/5Gv1EQY9hqNEfJL0grqlFqTa1vjNmubGrzd+48/6aZAKZ/P9VJGAwvXc+qNy4UWd9OJY4ccvjr04PtJPg1DR+jf6qbFy7FlMr8M+WySh02PhpBq7Ed2bikQPR/ft66fyWFGi+ymiiOWWW/U4tOopaNVToNtqr7e1ratnDaT1vb2r4U9PR6ulbe369eu7myyWmq61a9d2NlqwL4pBAceLeZmgKTmvDBjAOGWM4nkerLyIgT2TwWqjcXR06PLZs2fPnT937oOPPvro0p3xV+e0AFmFi6++mrXU19dzY69ig2JQoygkp+YpmqF4giJEw4EDBz6eZqurq5eWlkhn5+OPPx4UWDYXnV8IhULRjOjEYaympLSeSYUhRfX6s7ciqCf1WAKKI2mymFfIAkVKoBHRAJqgilI6nYZ24Vge2rjSv8+A7F8A2b8AstdUN9Z6TSaTt66uTMdoPBUVAbvFW1XhgjFWVl/T1NbR01AFkIJ1iyKTnF/KC1q+36HxNjbZzAIUzKk4JZGUZVmRwlPToexATZqzWPR6vc2uSo2dPzc4vnF1BZcLXdz/n//5n2+Pp5m6R7ZOjQ3emsaSYhWO5+KpIq9iFEXhrT5vZfuWJ5544pFWQ270xP79+98dzuVTqTwerEmFYkWVSiWHw0WNTV9MxnIkqWSyBHxXiCXjc2OjS9FsIY9FUJkqsw4LLyagzOTMnZFEOgetS8fjWYxBPRWP5GgViwdLmmBBEyxogsWQ/ZpByOf7CB00Df7QUNrzpVztkKsbcq2CXN6ysvZNWzdB6u1uKStr7e5FH9et6mz3ej0eMJE+VT6fZ9VYoYCDIb+A4X/nXVjAqx4FwR5fZ0ssLOSi0wCxE7OhjGw07tqFxeAnb7zxxiCBKbaeZ599doAqjZEfeDkuOz81Pz8/MbqQ57hcYmFubi6h8JTEshwH2NunggqycczsEKDKIK0UtHYT/4PlPl4Dbe6FNm+ANgccW7c62toefe4rz0F6dsfmtrZHdjyLPj792LZHW1tbWrBviXpZpijsF9se0Gi1mmxW1/GFr371q195rBLPZrliAhlfKk8LPh+gqCLq79yprMQeJEPAcOmSwWKh0zEww8hSBkyDxTIA/pjaamDMZosF/ZwgikXsv5EFwBPz+jD4S3AsRi8Pctbr/A3nk86nv9bHwOBGSMFz91GAM+7aGdkZ3hkJicwb2FgFwATL8hf56/yVy30MgCZ5Dwd+Eevav4ZwvSKEMOzu5YfhuutrCNcNIYTr8csPx/Ue7GuAiI+VSnv38j1ERL6qzlfyVWrVijwlWFStyNRZofF8rU9YlguBO6ZR35OtlFF9T77KngvnQ6KwLCMCeUy1Iuf4XFG63Ccsy4rAfbluAEt4YfAiWGalTMbrcT/tfva3RegMaCNj3HU1erWYE6mFHlAcw3LsMDt0S6QAaYnlUr66wkS+hphIWYUHwMLhr6r0eKq71q5Z1VRZ4TIZDAaj2eYqq2ps8vsBSTesa6vyB7BnRZFORgsMT/c7BFdDm1lNg1cnFVmS0DjNExpIvEgmEpnCQKPJ49TpdCY9Ebv1wYe3Jtb3VLCpqWM//elP95+fCTGB9h6LZWJiylFfX+myYFmxAsdTCYzlKBj7mrIqb2XzpieffHKdPXv3/cOHDh26gNfWRiJqEESvovFgYyGRV6vVeCaeYbjc3OTs3FwkV2S1Wq2KysXmJienbty4fv3q5dHZeF4GwIbStUa2kM1D6fGRqzeiGAW2SnAcS8OA5CgpEllaAq0xDK6s4IsG9KQBPWk+re1NG70epvF3gkSRwpjem8Zdu5W5IDFYjzHTMywXkENBglvRMw+/5+H3POiZBjJXhq0TX6BLsPEOfGBZhsE04gsPtNHadciS+XeQ7Umae5b8yzbKQZnP4/lSPkVzz0Z/OZ++lM/3DrL59P18JqjVBPlM0EIetbDeSZogZ5AskRIetVXw7t8H7Q2SJRbCo1YXKKcFWh4kS7SjVFIdlFQLJdVCSSYZ8qhMTk8pucwaIwccllNK9AErKiLyGOB9i0ETThSlvL4WKOq5c+eujc3E8thdkSv87Ocvv/zy/utJkViM5voxlDElZVLZBPjWxcXFjJwIpwFDSwUpDJMVGUKnQqWv+MheaM16aE0vtKYMQbu3tmvDxi1b1rR3bd7cXte8ZuOWTZs3rKpDoP+42E9JCsNRoqAyaFVqGqNVvCzS0WwRChwoQzo4JVWs7jz67uFX/+OnP/2vPYffO7m/rrvFNv469OSH8PM7l6+MZ/r5bCxRKGaT8YVRkT4/pwdqP1CG0D4zNjSVjI8P3Z2anJyZW0pI8VhiYTrOgB+Q4dcFmshlRSG8lC2mswUpPHZXpOMJCcbTwLI0AkgjgDQCBtSlZIeU11PW3CZiBZEy7jLPL4nYayLFcgxlFDFSXP5VOfyqCn7lBx3ovV73pi3f/va3//DP/3DtVq/ZC57lb8V+PJcL4grAhqIM4EbjBuV72qtX6372M+iaozIMOeyA2L9R+Y7ynSD+rW+J+Le/PWAAeSRpaX4+PB8pSqyGxaJQDE0HoVkivFZa7IC6PVC3A/F5r9e69ZmtjzzyGy6DC/s9yE5RQZxhBoAM/7Pdbt+/v+q1117DTqGqvqF8I7gR+0fsHwc0UNHo0MjIyCgPTHcafqXVBvFodOAh9l0FJT+P06dK42D6nn1vgXY8AfmehHZU+stcVgR1Pn+guqGlpaWhptxX0dDW1tZY6fdYEFLZvNgmMYgpzyMIwpR+RpIKSiFf7GdQUFOUpWBlUSZ4yKomcqnwArj3WB5P2zZ98Ytf3OBQJo/+7Gc/e+PUJGCZ6GdUrJIvQDn5TFoCzoLt3382Slu7gRd2mfHotXf3BwNykWABw8hcNhMPQWHhRK6QmJ+dnV0EvoOqUbEilhLVqMtJAldKrRLN6J88TwJnRV8ReF7Gaf4h7L9hE8KR6izSSzj1MI9o3oQ8ojmL0IFMPdwjtoF+nsefLJV2KXVPy09Aac9Bvi+Clms/j5abPIz1IZoWVjRdwp/az6PtmrWXLyGNV/5qjZcQrPZzaB2LSzrhlzVvf6DmPxV7IU08c08TxvJyY01VwONwOHzVNbVN7d3d3e3NddX17T09PR0NNRVuq9Xq9FZij4h9CGk+q43iZ+yu1ilcHc9lizRwZq6Yji3MTE9PhzKFuGk9ELa1lvzooZ/85Ce7T4wi2vtA24t81vZqickJLJXJI+YmJ2LRxZmJiYnZpUR6aQL47dRiTCKQV8QkCE70AITFB9ug8iAb/GW7sZTsxluyG+6+3fw5aGsA8g2AtnrtFhOYjd5gd7jKAo31y6murq6+oSlQHvAGAm1tTqfL6wtA8pW5HTaDDvKbrOhBINDejoPb6iegLUGW1hgMakrOBgF7lWQ0KgHCyfIPEdJR8IZTIkYFfeCioK95WrC67Da7EYiDdu9eYDO8Vq8FE5iZEa1XFhmDAT0wWTWx6x/vgyDotZPwgJ6ZQY5MNJCknJEzedBF9O7duzkK8LSKVbnr6uvdAgmcGjt+fKhgd1DM0gLbBNrfXk9OnD5ePjZW/qyIPRvslZMFsESOwlPzs0uzoQhQ8/l58HGzk2PTk5H81NTEhBydX0zmIGWi0TToFQyAJ6bAk0xNjY/j2BS4PsJZRqRS/aza6fXyCq8LVt0eGbp86VKYJDCICiCSmptYzGcFm0UD8sUWizSyM0WRRUwO3o+R2qEn2qEnnC5XezdE0d3NLld9W/eqLkitdaX46FGRzyWTSQn4EMv2swVZyuXzudwLzp0773KrV69W3925c2zk1pXz588PTk6WiM6gyDeNj48XnR0QQvWzCimY/A1NTS84BWEpDK58KS4IC6F4PJEA354txUZpkY9JkpTNwJvUzwJmKelYLLbMkzZASzeseHafy1VX39LeDcOpu6O1sR7+2baqG7hEW0NNqb2PiM5iBlI2j1EML4DqVKAAYCAUAfzjBd/OnTeH06bmDRs2NFul0Zs7d46M3rl59erVodFRo/HVV7ER0Wk5c+bMx5eGE7SlrmvN2rVro5BSBVoLwc8LPkHIy1IazcFF46lsXhASiwsRiJXi0XhJEknkk2hyoYDGYr8GBjnEsFotr0igxWV5/hLk+R8gz/8Aedasaq3xu4BT0xSr0lusVjBKAzJyuydQ1dgOqa2huirgr25ZVdG5HvTdVR/QsyCUYMa2ADNXEMMiBlQ4wbMUjDyIBIv9ZFHBcdFWLKI5w3yWINQaolCE2D8LYZyKCK6ZjxdIAAGGsWojl05duzpTEGBc6AwGRlqcgR6SeWJxYfLm5cH5tGXzV77ylV4rdnc/kPiDt7Gc2NPW2dXWWFtde2d0fHR0cmEpkshIMtSL7CufzRocfHh6bmFxYXbqxIkJVmtph1FQreTzIyeCayYWU3mMNTidNqNey1G0Sm/QouGAyflcAmpeDCWyUioWmp9Q0kDoJxdSEmsym80mAcuIPUuReDwaDkfiUdA+vMWhYuhmCmEARWNyEVeSEKuCpoHwh7M0DM6c2szEYkrwPlYjbvAEaL7K661sWdPbu27durVrNzzyyNrOjlUwAjqaGpqAwTa0Qcd3NHiwx8SgtpjPyQp6KdlUrpARdWm5CIEEBlXlSaB0XLDKaLx24/rlsx9++OG5yxdOnz568O0Txzs6WirViV27dt2mW1tbmTu7sMtQmCLncvliLhVPp1NydlbUTcyzPp8Pu3XrVsLcAiMG4ISVorPTM+CnpqejirI0MTkyNz8+dXckBHVlM6FQKJ7gQB9BLcEKAs+wgopT62xV7aKuo8khCAIGsZyGKGaz2WW5/xTk/guQ+y9A7m5/dU3Apmc5nSvQhKbQerra6n12i8VitFc1Nzf3rFvb3dZQV26DMlxVNTWNyIetaQNFiLZsaHI8ijScygPGFwsQ/skFRZb7baTep9WoOIZhKJKAbwqIt1I8M9CdwFUatXbfq2/fWWAbe3t76wyCoFIDwJKZdOj2lRutnfU2JT15cs+ePacn5uanx+7cuTMdxebEThyX00lp7OyRAwcOfDAh6ctrqsq9DpOOQkOLs7hqWtc/un379h43G7p59drNO5OhdJHAg92Z2ckIodLrCrFsFPn4CGABwlCVUUjOLqWji7PzoWSywEPcylI8V4yBouci4PqhRpWzolxvNaNHeBHEA7MuFhWWhRoL0dF4Ip0Fk0KTZ3gxn00noA/zK5EjivwMvlLkR5FojoD0Eb0bRZzC7hH4mvew2dl+/DmMjGtvTIJ/wpZ9Yhv8sgN6pwN6x76qra68bDmK8tR0P/oMpKeffnor9ieiSKHBTkBQReDQKJwEF0fgA/YcbTCbjQh5d+29kqvYsmPHDnDnkxArQaiUT0ObRS4ZPfvKK6+ci0N0lU4rQXu6BI+lZNRmxobn5uYWFhYSwOwZkqQooiCJnLSQAIyIzUkiIeHkQ7jm5j9BXLMqjrz8XPRhXLP+TxDXNMUR1yxEH841t4OYz+NPlEr7OHqPM6CZsbWQby3ox/0Q/Wzp8DQ+WEcMgWMlZul+iKKq11y6+BBlMaCsEoF0P0RhSwmeeojSGFDaJ7GyAVkHyGBAUTeMAFNZfW0Aa4cYB8e/z9dy/9/0dNepl7Dz8MXevd/nST1YeSyex4jlHJ+Zl0fRncHI8Tp7VVvvtqef3rK1tc6OfRlyhsMivIK42z1gWEv9cG62Yu/SwsZbP+1cCu3G7kCGH/9YhFcQ/9rXBgyS0VDIzUyfPXP07YvvT8/kS5VbrSK8gngsNvBLderIWoFpbv/6b3b9zrfamhSIvvowSsRJiD6ZUjypIz3K92/erHrl73bV3LjxAvbqcobBQRSh4uXlAzpPzGyZnTl8fPLImzNzPuwqZMiJeD4vApqhUPJ+JOlYiSRV5eUE5avYuLm8HPxdH1BdPBQSsdiASqPh/vbWrZrdL72E7YcHMBj27hUxZUA1N8eQi/MXzs3NYVfggV6EMFLEtAMPtMymLch+qf3I4u5cuWe/aP7SiK3MX6KlCniRrGA2gy83uFhtnsKmKRnrY55U377992Nj7M7nnsO+AhEtS+YLNE3ICpvPy5ieppbL+xsorw/K+39AovUqVmUymfSmqnVVDqfT6XdbzXqWd3htZqfLiRJ4/gqf3coVZcpg1OssHpdFwLFSAKPBOkXRyqoYQF+CUmSMxoGWkYyhzcPi/QynZs0GFSXaEvMTE/NpDIAqNX6RAToAcETRpBxcTxJovowjWaPDA3VJ07c/PLSXM2gLs3ffewOA9uChw4c5jcVhNajZU0NZtR0aZSFmzx+EIbEXOy0GcJwkCpl4LJGIx5JZ66pVq5rK7C4hA345TJW31ldYWdGm9dfXewT85MnrcY3V7LbbrCY+NXXrZHC93qSFAVJMLsxA6JXNEkWWodQqKRQrUBoOsFXKZjNyNhVXSLVRyM1OLaYzuGA0G3UIv8GM7Diez0HoQXEsp1LrSPgJRafvTsdz/UwqnPjw+kRMNCiLIyPhIk5RifHRkI6lCVwklaJEBR+8WtaJVsv408gSsiROfoJhnO+TXFwnPAeMSoMSnsc15L1VtV8sraoTcAtvKZU2ROIr84IvQK4fQK6/RXHPr2UBAUOOED6fGbgeZgb27dvtwd5fyxQE735i3+e1B9fD7IHJ5Zhg769lEzLpIMy/hmGwyDDyi4v5h3g4eyeex3DVaXDX+BSJ05/k4j9lHTzqdawfFAJQFMSNqOOXc34IOT+E8j5EPa9RKOh5X/umR9YE9Hq9yenx+50mc6DCYfbU1le4q/0OV03Xhu07tm/v7PzS9t94bO3qtrqampr65rbO7p7eR32PdDfUt6/q6W5rLHeBEu0uj8vjB01oNDooz4g9I/YqCsMVWIhnVRylFDDET1m1scUBQSORDw1duHBjTkIr+eHFRDYRuzKvgE/LhSKRNDjMfDqy6L0xEsqiqVogqgWIJEGR8VvHaQbCKbWaJXLBvt4cAYaj1hid1W5AxMzU6OiV994xOfTSwtjoxPjQ8K1LZ89dOnfiyKGjZ88tWLSg9TP7gd8dPDuWVhlN5mNlhw8euzw6OReTcwWVRXd+Em0tkK9fOnX0zd27dx/A3hd7ESGAmvO5vJxcmGMbgaZ3VtntTArCwtj8zVOnLo4swqdoki8ry0h6ez1EhK5sfOH6Bx98cGNRSnsl4OvgOo1N69d3l2vxEyeuR9WWmSJjNGtyc7dOBM1alwVZXXx+ZlEoLy+vdKpZOSf2RueLFpeRpTWk2mLm5WQ0AfFEYiETimSlggyRZTItLc0spbOICqZi4aSUlCEoJonoVIwxeSr4mgqXDkajBsPFXoKQc7Qi55UiBoRVlYNAUVHSY7No/bWoqtm0qdPNQCTBGQWg1JJZhwIWzgAcGlJiejrpRUwT+i1158yZoXCBYJjU7BIO0FLIZVJZGDvLNvZNsLE/ABv7JthYa0O1z+s3G1wuhCV2q6WitraqumnVerQ4XtvQAXpqb2qshBHvhmSzerAdokgW85IkVj9mhpGkNlutPJqrT8dSBA3hpYKzWrRyqWTTRT6vMqjwYOvxo2++tmf3O1q9FsBg585XdgIkvH302IkTJ469Hae99fX1jmLs+qFXd76G0uHrEPrQOM5ozWax+vGJCRzLTF6+fGEYMGJpKRKT8MOH3x+X3aiRFfj0ZDpJpa4fDrZKBY3d6DSreMTeNQaLioMxnosBp4/LRSkK4SKEUKzRCOEzTZvcZiyBalGWrl4VTa/heDKR4vx+E1NaxNGYtbioJ5TM9AJaM7W41BlmfjS0Mnv9FGjwWdDgM2h3gc1pB4omGBxOM5HXVbisWpVWo1bBV1qzzVFW3Vjh1KoMmF8UmXw6q7AC3W+3NHVpVagmrADBoQzqy8XDN4YXswPVUUxnQNG1trg0dPby7eYGezG2dP2tl19++YPZmOJp7x66+tGZO9i4WAkQlkoXcCUNesmprbayhs7OzvZqbWbo/ffee+/UtYmJ0bk0xBtV0YgCJZKZDKky0BD1mGx8LkPCV7lIIbEwv4C2C8lYUeRwXOcyFSKLqX5reuYOHkOL7PH5pbzJBBaWS2RVtIjdW0H57Nq6DuR7vo+wQ7tKa+vFlbX1z9ia364Dn6Q2lZX7/VVdHW0tpdTTXVNTFQg0dbY3QWppba4u89jRFqAyFEUaaSofTYCCMMFqtehZzGzeIdolKQMwi4N15fliKqtAh6rUDKaArVnsaHuCxemsQHODtfUNDQ2nTp62V1cGPLa7sqsKpbpaLzZ54yhaQjqHXRPNOC7FoqEl0OP4lQsXxsNLCxMT60W72SwQYGs3JS6dmZxSysHeVrnyY++DrSWjMkjCFDKp2OLM7bHJqampyZmJ2zfHJpbSybvXR9DkxPTo3blMIgeAiWexEKpFY2AYtB6MaSsqeEVjwfFjInX1apIBW1u8M02l1R4bB/a2MJvByBVb+2PQ4HdAg/8naLDHYdYgw+IBF8uqA+3tTVVOnUDQGqu3AnC/psrn9Fcvz4tW+5wmNWRVGQP1lS6sTRRdxUI+n0qBy8zhHEcSjNpg4HieYyAUkX9AgCH2kwWwRZHkOIoK9izkISrW6Swub23nqvaO7nUb1nXU58J3r188B+nS4NSSpnkdwonV9fz88DFQ5sFrj9UIDdhtsZuAMvMEDxGUmctls3HCWV8ujCa03spKv7fMriKOHLkcJQpKRtv5hS98YVslNn36iHU0URPswTmdDtyFhivmInNLk2PjMwlCbTdyxXQ0BCmSwTKRxSVI87NhCUMTUUb1UEXZDSwvijRBMgxH95spjdVqEkgQiaWKOZAX6A7Rby5FewyFZjpknCEUTC6o1Mss7znQ8pdBy18CLde5KvxWUJ3RVea0WFw1TTrdU3X1Pqvbawbv6HS4feU1jZVOnlBDSCYy2VgKZ3m638UIdB5qo5D6KAqXoSPzxaIUA6uIDdQleasVWqvXkgBmF4Ymzp9ftbojwGamPwBfdyujbVizoXdi5PLHQ9i8WIPjuUQKnCnaiiEb/JUVhaLFWdfV1VXJZafffffdKwtyTg4vpvBgXW4pUgCFMXImX5CzSzMc9+FSJC1nJQpqS+YWhoen5haXJIbBUmiAq8yaPPCjfrecikWi6dTQXBrtFMQVErxQcuz2XJbVCfhD2E15O+KpZfPoHaTGH8x9Ve3wBDBBDZI8jzP315QQYgi+e4hRC8p7vpT/UVReH3EVGvj8cs6vQc7fhXp/B3I2fe7eqDQXNJ/pEd9/1yPLYX7T5+4WX8eZj0Cghs/dNSW22vS5uyclGzWf7SLff9tF0vy8FLy/hrkFtLUFtOU3GgVBkvRej8fjtNndlb6KKgdQcZcr4DdVVOgQTvd5KIoGNNVyWg0PRXLa0kqJTlAQjlLUgN9u57jRUVkF0JW6c/XMyXeOvX340M6XXnrp4KmTBw/u3YsdgUIaGuaXlkLjw/MTd0dGRoZvNq9/9NFHq32aBGBhQ8OAnwJeYrVqzH6/v7K20svztD5QXgvoy81OQYDEQazd59FotBzNGQ0YyaKZb4uVBagXMEuZFVy4ZjkKfwPkewPkewNx4jKfDdRpsFgMCOU9HotGo9G7qlpbe3sbAfB9Tk91t88fCAQqamrrausbwcE0VnbU18Pfto6O9h5zB+BkZa27urm5udprQl7aiq0RewG/Z8PRVAQcXiwaLahITJFRmCTwUh4wuwgULJdLRCLhxXmU5uZm56anbt4cmgzF0miLq5FMzAL+J5NAg8FZFnK5TDLYy/IGK7gtj8cEf6weD9r9ouPyySSOL0xPT49PLcZYeuLKlSvjEqVkk0lgfBImq1u2b9++vk6TI82zFy9eHI7lXNu2bbNnrvzXf/3XO5MY0E40j0titKDXE4XsUpGQlTSa6g63NDfUeSxaHIhhNhsavn3jCkpXb44vZGlh7963r07HCgQMBWN26uoHB/devTqb4bxeL5OLjlwN9ibziMgVJAktItLxOA0q1nD0yMjdu/NAZQlSig6GcdCEyuEcnhy/cx3SxOToIPw5f/rM1WHzIHR9OIYXgWrjRRrAX4UDIeilaRLCAQaCUrPJZgULL9IaBoqRcRWvZPM0eFKdzuq0mVGf6lUsTuQV2mDQ0Gg2FV7GTF5Q6cCZcAyJthLynNa8glDrV/bBrUd795ZJrNvr8za2tdRUt6wppTq32QxfY78h9qlWm82JpLZlx+OP13L9eHpWZK+FkD8tWz/g1eu1OyHt2rVr96tvHn376Ntvv11RUcGGr+wD0oqdEfv0b05MDI4sLN0+der6DCh7cUlkQ54tW7YE4sMDYN9gsBqtTm9kBQ7PRmdm0ApsirJ4zYJQ2gXYguN6NpukHQ6buh/XVYrsehd0uKaqfuAB+OiGqH0ZH42HcbSKSmCWe/go+O5zL5IAxo/RSHL4wQuE/pXDWFgkeMGEEeJD5xwAv1lUKi6b7qH4Z2un79cuHS6hM4GZ7tX+WeaHJpmA8nmxw+iPhLL9amn6CPyT8tAeIyuUZ0WzndC5nJUOF7Hy+3uMisV/+7e08B8/wV4S+5hwGCsBK+9yud1GR3MdVgnfQvM+wcPP1mzCGldqVr1RqtmMVd+ruQJyVkDNFWgHzEICbTxfTnpBgp+x165dvnjxxuCgSA0O3rxxLai/dvnUsVMz0Mif/vSnrx2/iL0hat1uX+X1994bAW8g2NzuoH59h9tgsC3POXvLW6E2LceptTqW4QmC40C+FdvdCLVvvLcHCWzXZbX7mzvbWxuqnHq9q7Z91ZqentUd1R6Hy4VtAiEtllwBhoMo4ADUWoqT527eEOmhBQm8w0CZXr9799sz+vqaXa+/+u8//Kd/+uGP/vOlV/aUOYzJD1/ZvRt6p880MXH72tBMPBqVClJWllOL07MhkU5yFpvdMVDG8zAQCpGpmdjkjcGk0bh09sxEspBZuHXmRqxkwf0UrdbrWFGQsoVCBgNaRaRFmknnWYYe+EXLKNkl4bK1YWaxZJQTYJRaG4S5pZxfh5zfgJxfB9lbQHS9sayqvqbCV1bZ0Ll+69YNjzz21DOl9MTm1R3tXaXUUuW1WdFI/pbYh7WK5Lp1Aa6fFirXratU9ZP5+PSIyA3O5sGHCd5ukbsyISF3Zu8eaDEYtCd279nz+r7Dbx87vHff3j17jxxGOwYPAfZZsPDVI2+++Sbgs1qau7gfIAD7QOyz/lc4/P7758ZgnE9eWvkQDi0titySpv2RRx6pSEFtEVUbfPTFRgZaOE5lcZuMOh4nKJIAjjE/OXz14vnz5y9cuHBpaFrCEgsLEBPEcqTBbuB57BZ06EaCCARcqn66oKqqsrP9JKXzN4nc6gAL2Kirho+NboST2kDNsn47QGudK2s/YBUukyVQ29pa17oMdI3lLvsK0MHgAvVUa/rx6FWRvRFH832WrgEHYNx7u157bf87x95bhjfV0pX9K/Cm2oskvjwFcg6fFNlh8+bNm33TJwYcgGxmp8GkUvKhZVCLMyardhnUmC8jGZx8P+75ssg+7pIkiS3vedgeLBe07XmcOFPauRi7t2KzCaR6BPJtRCv2dpMWRS16i70Mmud3ObwVQBkqvFaDAHRPjzWJIlogKxZLdB5gpAiuGT6Az0VT6cWgr8hogVoJxRQEcRMT8ynJ0P3EE0+sMuUn3gEGcwVa7UUr31JkEfVoJFkg8BMnLs7mhUqwsQouM3Yi6JOLNNrVwRNKBnxZMqdIMGqi8YzCaJGHwhSRwXGI61h6wIighSR5NU0oaCkfJznywTPH/iYkdyCG3tPKw9gz37TMnnXQ0OdxlfJg9lwNilhGte2xEqrdgjatoNoOyPk41Lsdclb8Cn2WG/OqT+nU/kCdlmC44lfotbz73FlocuBX6LYE3hW/Qr853Gb8tI6tD9DxJ3j/2KflDRh4x6+2ITKq10d/hSFV0PTE5K82pgAXiXC/yqAqho2OyV9tVAKXzXL/rWE9yBaE+7bgWLYF/Se28CLk/A/QzY8Qd7YYNaAXQW/1uvQau55VWU0mq8c3N+f3qgzq0sSX2mi1OrwOndETcNktNodW4/CWeX3llRC4u9RWi9WkAe+ns5eBW2NKm3Vz+X5aCs3OziX7HQSJFzApz0JB2WgczQpjSjZ6O0rQ/U6SUhRBwAsFiMcVkqbQHn8Vj+fSUiHYy/AatIuPxSkWzxXSkUh89PLFodEzZ9qbDbnJjwCrLqX0jT1r1owNjy0kknkoLC9jlKyu7tm0adOqSjWfnTu7D3pk39kFbBbNB2NyOoz6Y+Kj/fs/GocPBUN5dZWMWzs6OhzZ8Nz7779/I1zMFRgBiUGqIG4iodvO3onmSVut0NnZWe8gM6HBj08Ee6kCiWawGaIg0zylNXAYybCEUlOD03pGg3akaFW5aAp6iMin0kUZYjVOiWcyi/NIB6mwOo6pjCY0qachMA0MNoajBLaf5n3t7ZXaYFchFQqn4ynIq7UKeaDn8fGxaDa0lIaW5cILC1MRjKIKQJQNRg4yUSqBBM2urE/9/qdn2Eqe1Ov2Bnw1XRs2bHhkbX11TVWZ2YL8QmNTBziJzoZaj9WGJnOBKVdij4v4wh6R1Kg4sp/KZ3MaTb+7EJ+bW4jnlYVcLg3GibZpyMmcKs+oobuCrVq99uiru/a8dfzQ3sOgxRPvHt+/86WXf/7yKy+//HKs6GhsbPQx8Rtv7UFTuTt3HriOXRWbNfvC4ampaBz6IZXPApnFcUNFXa0R4gLNgQPv3FbsnRs3bqzV0WaN15y6dCDYquJVVo/NzuYotQwuZyYEgZFKbzMjbWflFNqVmS0YnUY0l6s2mVkYiWzxGYKorhXy/RXJOF0sIPNMjo5liFyqyLIWG5nPQvwvGMlJaWZ0TmKX9fdnoL8/A/39GeivazmCcNotVbX1dbWN3aDD9R015R6XW2swIyXW1rWDErtaGqvK3C6kRavJBUrsiO4zmw2C1qzVsgAkRSmTK+0CAVSQ5HwmEY/KsiRDWAX4QxYKtB43WHg52LU8I75r1yu7Xnn1wLG33vnggw+OHzl66PVXgYUgfb4cJyHObnBhsRtHdr+KNLr7wFXsmtjB7Z+YsAjy1Jkz58fi8XgimcwU0IZLCJJMaJskd+DAu3dzrjUQndRzoYVCTpCuHwh2Lc+NCzoLzyqZbD4+OTkZK8qY3mXzOi16UG0xF0U7FXJZBh2iQPPkQJAiYge+BcfH5qNsba2ZQCs/BVaLttPLC1ev3hxbTEk4RbFkdmY2mUxiWiMdxSbvhKkVtPqMhba1tXV09zRWVVb69DxUaK1sqGtqbVq3Dh74a1vbS5PCnd0tFX57aZMw9oTILUCrIOJOyP2URpPL5vtdFMRairIPLBTnNUxelUsVgOLwjJLNgIU2NTU1NjQ3NjUGbBYjQKunyu8uq66td53+4PQU7m9sQHOl1S5l8tIBUPHbt0ChzRqGIk11dZUGhAa4JKNJ9UQMwtnwCbDQSymrz2BiDHVgpZ125fY7YKHgD8bv3hqbCWdyBJIkMTYbnxscunxrYmoiPXtneBLNEwxfvx1OYjCCpBgWEvXFdJ7OjozE0UmEQoGMxvtdaaKqiiDWgoVKs8NTqSlCxwKJSucIi3nFQltAf63ohCXozwYWarQEyqvXPLO1vbG5c3Xvqnq/2401A+e7IZI+n4Hs53mjC02LmNxlAzZgfO+8vHPnnrdPnzj05oFj56+fOYAY7usQDpwMh0+e/PAO2iCW/cs/+b3f+731uY8HbMD2LE6jVZe4MyUhPAtLOhPHAWz1mb5DEE1NdnRMV45NL2YymYJ368os9begjX8Mbfw2tLFDpdKbNEQonufQupILxoXbh6bwSILTsJryhgqXxQQxEac1GWxOBxBvkxYtj1iLyaGTi3kEDiSGnHAhK+E8qfQLlLvbqqXRyShc5OCNoFiOwgc65iM5SlAdeuPtWzN66HRDIkHqkBUrSxPDF06fKyvTpeYuv/766+8P3bk7dBWY+OVhbFBsQ9pXCtmxszDcRsO00+syCxTJ5GOI4ydlxVQFhVmLc7du3BocnVkKx2U82KGhi4pgri13qFH/SKyekzIkGJfDbXM6VelQQuF48CJGdJanWEATdjSmQtN5rMFfB//qVxcIWcpjrF6PNvv0W6XZO8XlbZA4kgbLZTL5nLyyPwztAekHff4NlLZO5X90WyNntBm0OovLB8lfWVtbVYbmOsoCZU67DRLo2e3xej1mC4SdvpqWloYAfKVjsS5xjaLMvPX/Tkfk0g5tVqXWCDyfSGQVnCaRi4ZYDVMUEsdKR24IolDk1BTakcIU8kqQeuPA0SNvblS+qfx+l/jSG6cWGOgweei9d9/9MIS2wkvTV9967WVwveAi5ekrF26dObx37949+0+cePfI/v3gGiCaghZI4ZmRDw8fOXLk+PmJBO8N+P1DQ3NCVevGxx57bGNLmR6Gc4Ccn42D4hgVT3ICmvshWdSCdQF9IsHXVNosZToCQ2fkCslQSEYThphMMGptadYIYI3nWE6HdjBT+VAoVkDo5cd4cQ2OOx/9A69bDWVn41EgXalMRqNhlYKycuKOpMBVIOYH/4R+I6U0GJmSS+H0vZnwtSu7ynrQrjKrxeFwsMJ6n1qwm0xOp8lk0tncPtC+HWgRCbpUi4zdrh/ZL8uiSlEorpRAyUH3TQly83wDMT48+dpr77+PIH3f4ZMQeN3BTotOilr8aNUqmi5ceHF0NBxeu/atw2+88cb+t1ieCroRDSxQ+I0kKwDYCwICfOAkoIYcxsL4ySVIcnExUf6MXk8QFJVLpNGOQ3J5PqYLJFi1csrV2VBhL6Xy1q5V3cB1gBStX9NUGajBnhJFnNWInF+fSqWkfGxCBD5FXr8+mR5w2tDKDCDmu9OSyV9dXX38+Hu6ynKvw45dEAE3TbXhcPXcrl273rrFVMFHkbJadQwedC5hqlIqzt2+dOXOxMTk5Pjg1duTU5PYEvrd5HGC2D0MgEWGr8DHYZEKh9PFh61BtD6F4ifTBfQ+t/SZfTWfiqIcT6Eoqh8a9jxeWHpwDNUNwi7z5tUXSrz5Y2jP85+cD9uwcr7W+1BttQdUrgdrjEMaW15i8D5UbeaaY+88RHVcSXWlYMn7UPWV9hM+WIVcSYWfxEgWkMcC8ljQHJuJVlvL69orsFViH60o2ayiDPBd/I/DkXVDe7AJ+DIc3rs3HB7glzh8furSmWnsMnwJhJ29t+sO7fSrhPIq0e5ClsIok79xw9OPP/3YhgavAfuC2I+n0yK8gnh9/YDBz/zP23ece2Zmmj5+qXpq5qfYJbGPD4f37Fl+ifhXvzpgwLV6Ij89c/XjY4fO3ZgOy2gWhicIlWr5JeITEw/YZcjqGFLjrqm2VzV6NCzWuLy5b26udNjV4xnQ8S7qH65fc7309zsD167/K3J8bBo5PlCQiLe2DuhUObU2H7sznLh7M5YXsDnIAHGhywWGJOLj48t1fgXq/ArU+WWos762zKgzWcxldU0t7V1rGir9Ab/PA1wQTfiV+cqr60qptqa6BtsqWicnZ0PxuyO3b98engFyFc2gsxr5vMhJcRQGphIRkY7O3B0enQzWy9FQnjXQOcZbU17e1l7vNurVGoPB8Oqrr743uCBUtLe3nz17djhOc9hdsZbnTdVd+/fAww+mQna7gBYpsNx8OHzqLfDxBy8mNeDOzHXVdj5YS+mcTpvNQBUXF2Lh2XCiyGtL5/jR2RWIPTPJbBa8WmhseClYy7KUIsPXACN5Np3OyOCjpAx4BopH/FVRwBTw6VvjKfYXxijrWx59HVvR6LTfRe/wXxz7hTlmujT+VoN2lsdfL8rZh10Lrow+t2/5dKAH8nFsCfLRlkqtU1db667t7mMkAqN4BdOwbFcXz588+a//+u67aDcVv2vdunXh8KOH9u0b0MgyWu9OJpeWUik0V8WrCAUQXy6Q99bWvwft+Ruo57vIyzp0HHgUb89j2zb1It7f3VzjsegEi8vla9jy3DPbIT321LPbtmzc0N7k9Va3rt60YUNHwIq2NFiwr4iOa9fujI8upZLR8NJCOJaQkNISST1TLPR7o1PzhVx6YfgGpKGha9du3hy+fTE8Pz83Pzc9OXoz2Le2tLhHkxSrRQWSqaX54WtXBA2TXZwbunDu4sWLly5dvkxQglrQkLcXMXDiGkGaufbRRx9dBEqzxuv1uqwGNbhADsoxBerr66vdJosKRy6Dsda01td4bB4PMMbOWm8qxZVVeXxlXq9Tz+Cp4DpBzYHzy8ciyWQORS1oy1BWRienIYDPZ5cPSSQUJZ+nciSeCiWLCqPWq9AhIRLLijpZzmaL+TwEwCQNLJzsZxiBjqdyxX4vDkoYnohI+Xx4aXp6Uc7lFibDPEeTaOoglQvenwHctIK2ZWoeHDdJ86VDJhrk1tGiQOlsjkqtxsyi59atMSCX6PxIaHr41uDgrdHFDIZ4VD4yMTQYLLuL5jvzM4Pv7dmz5+ilofEIUNXIxJ3rp48dO3bqCnZA9NTU+Mw82unIGNxVNZKE62ye2tbW1lq3WUtIQbsVLIvV2Z0VEHxVuh2WAEQn5Ta7S9dPap1Yu2gOh3MZGR3/TaVTaOEtHMtiRD9NKploKPyps36PrZwfqayp8TuAHfrqq6ta0WGRusrqQG335s2bu+tqmn0oEPLXYk+Kor8ogykgzRI0WZBkrFCQ0hKivBSl1ZNyNlcIlgOp1EJhBr6YRZw1LikFfdsTv/Vbv7XFhc8e+/nPf77vzEJQ9DNq6CLEMKUcKQg0dvjwqeEsa+nYsWNHhwlPDL5/OFiZlClEpORoPDYFEDU2G4unQqNgpncX43EMPAotx7FJaJcCHZzOoRkJCDUpAiNJVhAoChmYlFVojiE/62nMPgITzGZBMFrQPRDtYh8ly8Ui9X0BoV9Kyv7wh9jP4cvvftfnM32foSHqAtilWYHjGQ0Riaf6sfuzezugvMfQ7J5KVeZ0l9fW1tZU+Dwuh9Wos5X5vC4/xHMQBVXodDU12Aaxz5fLJiORVL6IDpSTZFFScWiTHEUWc4lYuiBn8gMVghCJ4A7A6QAXTtw9cfBUwI6F5u+eh5jgg48u3frBDy5fBoztM+764KOhmNZTufapp5768nMtVCYp9Qu0weH1lzltb775xkAFQcTzeXTwfH4xnixQxWgaI/OpTGxxcTEcjYUKhcVFbAYaxZGCTstRLIUWWRW1TeAIGW0GJFA0IjMsM4B9Zj5zB7Ibg95g0nE4JWj1WoMFSIgBRn5tLZrHMxrdDrSN0wjRX7+5KMV5tH8HlEwyNKdWMyRHi9wSDmbKaDlioHJmLlYUtG/ufOvGwlTau2bNmhp3ZeUXv8iyly6dOHHpGmDM1WEIUkUfQeG5pQsH9wFbf28wztl9dbmnnn5aUboKcjoLjVMGrAqrEVhGMBmYvJxPRSKRJIzH8XERLxYhXp8HxEjn0a01JoJJFRkUMhJyKivl8grEQCJnVOnVjByTlIH7dwPUgcR1ILGZ4wS92ahG7tRmUFEEUVGB+USaYTQmIx/k0D1XDAWgFjSTJO7e/I3fP368srLKvnToRz86fBh7W6TLb310flIOcps31xXvXL5ypTxoBjvNhEKpOUjz8+Eiz4+ic7B0Tskn45kgVzqyB/w/tzJyn13Z3/csukGnorHSara4qv3eckh+t93hcpjURrXfz2vMZfVur8/vRyF4uygaC/HFHJpkktBRJxTB4Dg66yAKfr8NHeqhSVBeTYS16FR79lxeNPRC6vIys6cPH3rLZLh58/hbb3149fyVa1euXIFQdUoUy6VYNH3hwIEDgyRtawDgqOIheFGUp59WqwwIJ0kGVwYq86GUoKapRTmPKH86jzO8WmdSAY/jVJoCxJVZSAV0HYDIM2ouLaANmgVRQIEqo7cajdBOYWo6srx9WiqsWOJ3Pn32sBrdGWTxVtfV1rWtX9/TAhGozQjcRKez2NwVTR0dXd3d3V1tDVW1zR3wqa2httoLgam7ogbbJtqRLgg0CGmV1ceV9qwqUjIOjSLkfOF5C7rvoVC6Z0nB0YVGRUAVgLo1uBG8AJGVi+mlufCTj61y0Asf7QeivP/Y8aNvnxpaWIpA5y3OJxTvpme/9KUvbaywk8OnT5++voA2NPbgYP2hGPIUEoSV5ZueeeaZdZ5CZPit118/em0OdJXOZGLxHH7o0Kk7iYIFnUL35Uc/Prz/UHDNUmmHXzY0Mz09fvPm0HwyllHQFUMcTRFSaHx46Mb169dvjcyE5sdvI7c+Nj2dBrUWYgvYOKq5oOBIoqKcDpG5DNp5RBBYEYOAG0fOqljIF9B+NIi+FXR5EyScValYYsUGK1cYNlqbRgf0BK0ZhoZWKwhagwEziX1MPpPDSI4US/t/iQFDebnHBpEpQRw//vrPX9q1Czsr9mloTw+MXRi8z/S21lgGOIpiWFySBKEfEBfdLwYF0QKH5XMyxF3otqmVvv8x1P8i1P/vaOXAZUf7V2yaQsG1enVDAM1BeJwubxlYPsJhm8VisdqcLpenrKzM63HDNyi7w4WmOtEnlcPl9sKz+tWrsS7RWtovjrYjokPsJchCSRFblo2giGijsvwdhpekw0r2kVfA7RQVonTjVZHEFTnYm8FU6Doqq0qVmZi4dWtkZGQSQh9n57Zt2xrZzNz7L7300pGr4TRVD/xtlRdPTR772c9+9u50RqWyWIH8sio5GZ66e/fu1YkJLIxWDnA5kcxAj0xDQ6RkIo/jBw8eee/y3bAaBNBER6+cOHJwcjIqeBt7dzzxxBNbWrzq+KSlUFAYlZpU+hlZ4TmI1IO9EgAxA8o1GqXR0enSJV+pnJSMoX2MCQmj0KKsIkmZOLrkK5lOSQrIhckSOHYBXZqjAqyMQ945AKtEqWUEw9IEqdA4RXECQyLfhlxH6TonHOwJI0gKKCKLy+Bi8zLJo+vHCEDLPEEUeRQdkFSRvuep/8cK+y3NMdmMNo9d4PVWi8nsLK+sqqqqrqmp8DtNOr0BJaPJbHEH4GswMqsL+t1udjqs6Bogsw3iSBN0aGp6MJqUUF8SjNagA8tExP15O06WLBQHk0cHnXGS59EcDQUhnVIIrlPSOUav2bPn7ctLOWvHhk2bNq3vbG5pb660qDC07mIyEhFETW5HybGYCXy/Lhm5+dbuV1555dAlbB7N7nBaVkqOnjr05ptvvnd9KSVU927fvrbCBGPVZOH1eg6dZE+FSR6/cGFEsXU9BmltpZZODV8IrpPVJp1Kq9NreBJLo6tjFhcWw/MRRW3UAQsEks3SlBQBvxFKM2wOekMRSJzk0JqPBkui2nNJiQb2V0K0Yl7GWF6l0qjQqlIuRxFFGQyagELI0nEaIod2a8gFDMjQvZGOdpPfu6uuyumEmEMQLB6H3QesJuC0OSzOqpaWlmq30+vQQCUqc2k7b+BBRDG3TBQFnRq+AvisiifyFBTH5CLzo4ODgyOzkTAeWL1169Y2dXzk3VdefPHFl9+5DSG/GCiRxcIDyeK6Gu0yW6wqkDxiHpiUzyHCAxQLXQw3MzOzCJ9KN3EIpIyloTilKP8SXeSX6WJeyikUe88KXSC9C2JCF5qZcDpLW7cBSUSyrQ3E7KMROODk33EsTi7fsMQGHB7fUWxigvgrn1PE9oq6VAriXOD4qRRNx2JQfR+dSMTjiezf/TKDpCh3WXkVmv/3e5w2i17FGlyAV0jXFf4yP8cBg2wrMchsOpV9AIMs5EonO4BBarXxGO6ASKucj6ZHTx46XeWmIot3z508efKDM1du/eM/XryIJlOMu469d2mWc5Z/lkHqrC6Py6wvMUi9Pp2TEmBcizEpxxm1CqXV0TKa5QwtLYbjKtXSEpLKB8zIYlazPFXCT86wwiBlGi2nkjRDr2D3kyDxkyu32lShwxtWnaCxeAKB2rZVq1Y1+jyebdtMprq6iorWnm7weS31yKIsBJZ/EIfkEYdUCA6RyKpIKEMbrW/tPTmSCCk1W7ZsaatsbPzd39XphobOnLl25w4Y2Z0ZZFB+iiEfxiKzmQKOaGTVUrYgpZJZvJiASHhieHh4fC4UungxHAbqHL57Y2hiYmJqDtmTBdzmg5gkD0xSwxMSopLYfS7ZdI9LwkDyBxx6H1A0lxW4N1XqXeCS5pp6B+KSWoEtcUnKbHZv/uON2N9iL2jGxrZu3dZNf7xr1927IAnwyYvjshrxyQZyZJlPLi1ND48s5RKJWCwWXgynUiWzAz6ZHB2aRXwyHMt/ik9+AfvkRsba6uZq4JPuuooAYFlttd/t8TrNGqOmuhpcvLum1ldRjQa+D3VJiVBmHkAoVX6/U32fUdZmdF67+dChoYT9UUjrq7TRi+8eP+F23r17+uSJszeHrt9GHTM4iTqm4hNKSVnrP0spdar7nLI2Mj4rY1LybjSysLAwvxCB0YCRbDE7MQGBPkAl9BFAQDSJuoenBC6tWiGVyqdIpWpqein1C6zyL1ZONHwHnWiwoC0JaoPVZjY6q6oCTovFoEYBP0OrNEabtzzgQ7fguu0WqwORC7fDZgPsNTtAOw/klDkg9Cuc0vQLnBIrZKRCsGcJ4E3ILczN3blw5mJzo5uI3T62G2Lm3QcO7tt76MPr165AJHTu/O1FphJtPCtX4TPvvPPO1XnQXTewul/ik95CZPzow/mkMPo+sMmeIgO1sngxV6QwCI5IvrSIBbEOtLuYiUUicfRjCbiPjOZb0mkpCVooZkG7FqAkv8QlQbdlysO5ZJ6FUfELTBLN1TJWq9nqq68XAefKbDYH0iMwyewnTJIiB5iGhpqqhs5OkRscPHVg34EDCMY+yyStAwaeV/FKGigv0DiCLl3Lgqgk/wmVzAzc34OCmOSLUD/rcPkgivIZxUZHe3uFGZ3fsNrtFmOJSDhcXmAW0MkOmxlxDosDXejjQxRTNAgOdLGPy2FBx6PK2tuh5b8OkSQ+IZJ4iUjGCmqz2eywcFxsaOjCWTCCm9PRrK6mp6fHV4xNv79z5863Ls9FCz6InBvMucgYmmg5Ph7jODNaquZVUnj6Fvzq1NAQ6OvXIZL0fSLJICKphfANbQwntNr8zExY6u9Zlhitw0sQfqOrcNA9O8h5F+UsssFMBr6AWKEIpgSxU8lIUjMz0EXLNJKh/ndoZPFTNBJDNBL7DI/8HuKRToujzK1VG61ms81TOpQEIFcVAE+HLow0my0QDXgr0R78MofNDabgtLhcQCRNJqsdevQej8yUehJHPJL/nDwSl4oqq2nfvnevRwq2jt6NGzeu62hsbm2usmoIFmq2W+nY1G1AwQg1mTBBC/Sp2NDxva+99trRq9Bhn+WR795YSrGVn59HuiWaxfI42vVazMRLR8lC4dhiAhN0glHd71HAFrMRhJcpjMzB0yJPyDm0RYVmoFeg8nyWcQB8omuGlXwew+n/hkbS5Gdo5P27KC3Aoyw+gqFxAh20pnU6UJ+VBELJi/dn2CgIYF87Rj7HYikRnUZbnnbDWE4dIHNK8d6u8c1Q3uZ7uzLRQoEe+siopxmNCgIUnU4L5JgBtEExKRYQ+9x5BfqLF+BrosCWaCLPcXgObFGWMgM+HB8ez6ogShsfPH3ksEY9c+fqe/v27Xtjz96D//APx46hIyXuo2evLHD2CuAobU5FRlc+pXC9Qa+mlI8+PjjgK6KOB/MrFNA1RnJBKpZwnVXpDQwoClOLfVqGZiigwshICUXJBXmQiOFUGpoTBu7PNaOVvU1orlnLaFlcIVVaAwin1nk8VrBEjUatVpWuT3CKHkWhqNK5oTySTm2kNTrwWGo1TTEEoQTLZudxfP+bH4zMpsFNaWm9vr6eIM6eff/9k6dOnbqCXRBFJ1dYGvzogw8+uLKY53SOaM+q7lSqs4glE0l5oCzPyQQnYMD/QFEcs7SUBCaLREQdLmNm0QQGoDCaUkRISJKcpmSpILJykWJw4v5pzBqQqgakqgKpjByHQluGAQDgKByH/ikTGZIwu4yCILJ2uwldVkMGjcUi41u/ffdummYT7+/Z88or2Bsi4xi8NhJLp0W2qoq+ff3WLUfQSBCpBPBSDnTKaNVq8DoWkZLxWCgTZKPRAk+z7L172n/hRlOPF1sjvkBP+D74PvYyfPDGYi5Mt3KjKQd51ZCXK107a34a+wvx+33YyQ3Kn/dtUGrgM1m5DfuC+H3sQbl/G/vuSu7fhdzaUu4N2HP3cgufzk24vwldPoDyj2xQvgf561F+UxO2+V5+GvLT2PLKlIhZxQERO4beAuLAA54H0KMj6E39wOdO9OgCejM/4HkffBtEV43AW5kY/NzPy2F0231oZz0athTTj7HH7wK3unv8J5uUv0H/gWR/JQoEsXDpAER2By4RQZZBK2cABozA81q93oFuAWVJ7Jdv1Ud33enQtQB6PWYSeSWXVVhgezklqEKoS+7a9fq+fdhu9GT+3IED5xbQk+xykopoGYjPyCQuSQQp37vtUFixBgH9fy+AgMEAxSqC/aiV+KqmILoVwyXi6avB+7nR6mApN4Fus+nDyoPLF+3/M16OoU2PWvjubvBBFqH3Y9Woh49sUP6Pvg3FGdTDtc9g377Xwzbf8lkPdNs+y5TulAes8FqdJoOpApSN6fv1RdXL+6/EtFpt7Mq+f/mXf8H+E4WXqr2fVqheYSmVCq2D8iq0ZxWziSSQcnDXJKJzLLnc32rEuUCWMh+xXBnNEIzX5LSaDBXoklJMX9z/suoKVPL/t/YlwHFc55mvu6d7uue+DwwwBzA4eADgZZKiJINkJMprK7Jsr2NFlhPHlfKmshVXJbUVb7ybChvNSD62LB8r25Fl2rJlORJFh7JISaQoQrzACwQBkAQvEDcwAOa+7+n9/5khAVHcjaqyPcB0z3T3m9f/e+//v/+9/3iz9oPwW9TnPieqKpX1j3/9619/fL29Igl8tZoUiD1eViuUer3A23n4MVyek8lytPdn4Ln+FJ5rrcFkAfkKG4bNUimVKp1ebXY6HQ6H3W7FuA/of63RohkycYseo1BKxRO5Msa70pmsqEuA/p9I5ErQaTiN8ftru7o2fvLTX3zmmWc2WYBBQmUAIXBsBSBHMjg9dmuCadm+fXsLMwPMjijE1Qc+uDIZzKoatjz99NPP/OdHlxKIRrPxlEwUrBweOLCSkmuXFuanJ8dv3749k4zH8hyVWVxIzt8egy8mpmYXgsGlhUAgsLCEshTks7EC8tlgEOgCRgKcnV6K5HqRxfEcrtNXKssNsBzDGunyDNDFBiC7Flqb1+m1OlD7BAGEl9xrwpEh4LoibGp0rSYd4upYntUa9LwCJ9OTkVAtqAKPoSABTBbTMWp5k9YODV/44OBrv/71r88vBYNx4Nk50GnzGDVTbW5sbvEUx44dOzZWbNy2bRv03tU93S69Ijd79d0XXnjhxz9/nS2nE0DvSjEWDS7F6JaecJjzPvjFL37xQW9YWgsQIg1IrlBIzs4HxodHp6LR2WihiN59mWQ0HArj5HcUQ5QukjPiakbOg0Kal0Ei66xuj90EfYUqJpP5Esb9Y3C4YeSGPIiyZU8pvu7JhWGZiEkScXCS1ZJInCK5M46xN7trvZkF5EexNKAuBavEKUxADhyCDQQmg4XBP5K/IcMIlLdxjzXv29f8GCHaqj4naFDEGkmjqF5cXIokAZgmI0uLtXp0QfmboR5dUA9rkQYUVATADtKdyDSpMHSJyKLZrNNZLEY9IDmL3oifzEajpOnjw2E+PHEr9MOX9/5gu/zfqOef3y7/PdN37AOyXf6fovlvtq7r3jqTzU9P57MzW7vXbfvG5z8vWRVFTgkIVqngEALTjFkLCEglmlkVz6q0VUjM6dSsoGI57r7xDjaQb6C72yB5CXchUqx7vX0o6lZRzWdp2mQiBm2puge+zuRisZzEGHneKDHu8u/OiuozZ9rYA0Oy3N9P+uGCBxKJByRGOHlSkIwOinXpdDRtpxQuvR44qBEuoFQqSmIA3LE16ungN3V3ZC6nU9HAzZicwZCTWIv/9CFyQ2T4QICXQMyrBTj33EfvUum42l1wm8SebDa9Ra5hHIBAQMIUMklSWCGzVXWeS6tA0BGbBDAESU26JDpG8iJJrrAK4evXkrJENBLdhE6JjRIGChFJagUfr19X8xrTe7tB0YBPM/P05U5C09E4XbkTIeejV3u6alefv1i7Gv7+H1ffKfsPh2pX3574v1+9mzIYaLWS7Kb+4R9ow1/DfmCAjsTJ/TBDN4r7eXyL3xcTdN3FBPR9z1fvP4RvE/Xza+H8Wji/FmeynLie4nS6GpzOBuTnwNRBdpr0pEjzihTmAFGUymqHRaWXLG/tf+01UGv2v3mw0Ql6mMXmbCTviqb0UjBdzi+icQSfS8gaNp2WLJQClEGFopZ3oHZAHKJpOp6OLoZLRdRj0qn5G7cD0/XWWrOiVjBy6KrZJAb3Vlano1ioFWgHKotDXS6hvpZJKXi6uMeifeV3+9544419v3tFozdbrVa73Uw+gEql06xGTuTQbCWxmC+ng0uSBbhaJBwOR2Afj+K0aDAMVDVNTwdu35hPpcNQq2IpvBhNx2sj4GtQp69Bnb4GdeoG+WZyuLxuJ3oU42Zv9DY63GvXuh1wYLdYq185XB6vy2HWQXU7gwreqFcIKDCVKo2Wx5QFnMCzKfj5VCqZjKHalk7HI9H03ERA6lYoWEahMtgcVpMGkEDh2tEj7526FlcoEtdOv3fk6ChAZUEHVLcb1ZyCBbnYqR27PbmYBhUwnkik0zt2Pbq5vdOnygAPvH3q8ImDr77yehqXjeOLwQyt1krdWq1axVejE1bDEoHCTpUZrhwKAXMsoX8QWjCDslYuo/givNiZT4RnZxdnZ2bn5+dnpibnAguLIaBgPJ6ROSZfRFWzjGvARM7xwp1MMR+im17LM6QIur3McEol8EYZs81EoxXQR3G+HoijAM5fLhKFoNVX6RaYmEtHQUal00iiZDKVisdiKZYXsEuxvFZTpahaUOiNvELq1uv0Oh7VXhmIrXY+/NSfPvXEQz69vvmhz8LhQw4QwEqqUiwzgt4Av2AGumnVdCa4GAfa5NKvv/LqwROHT90G2ZFR+TrbNz+6a0caxGc8J1fSi5O3x6TuvuMnT5+7dGnw4sjgwIULA9X4qudHJicH+89dugCfYLs4dGHg4sWzJ0+c7CMDQLeywOcQTwFtgESFYp7hMJow9L/Q4kJgbnJqBigKdF2cnQ0n6nSzAZY0AN0MK/M+sGinBviolraLVSgVtJJwpndNN0w3bnJh4aQQ2VDNtgEvFRdNJBPRaJ2TIC92gnx11nR3gCpsbbtTzPKvNJ+l4HWTE6DADZH6CURrS8ElXLviElAyFPwRmSUCPYkR5PrrtV1OJNFl3K2po/TdtIm0AjjfKu2mfgpsfYdICYK0m/YCU6Bu30HeH5LX1eetejvXURRspFM0W5wuq0ZjcTkty0fSIwAO6i/9iy/+5Ec/eg232pTHdvkR0TwdyNm6uqy5wPTykWT11TaPy1bHXXIxA1DCrOZkIgiUDAD27pFE7vtUnSKl1YI28bPq71Dd3RLDtEAZ1F1/7A9pQyuexSUKK55AvaLG+0VhRS3VK+pWEoWP1Ei5Qh6KUCrB6kAZpFuiGbiD8MuoTLiLNNqICbAFdRGq/XeINW6C/lzDGpb6ylmrlxZ44NtKFv4VvNfj9RTyhbyJV9W3auocSqGs/g9eHAQNm6EI9QqU+G21ChUsJU4NKWlVNhXPZpsa7E1NelXm7lbvpYZ6vao6DWI+ZEzJBL5CXP2XOOqV3+LrWYHn6uSgeYfD1uhutDk+wd9TIuJKHTyBDXElPoFCgTUPKaplYSRcqLECq/rbPaDzVuvIlgvZctnKriznwxRrIquRYr8Hiv0PpJiasHWK3cE8OsQ8wNEwuwoDEtImsWrTeydAPDOa6WmNxJYqpTJgHiZNUel66w3Cvdfg3kG0h2jxNFQtHBo8Le2d3evWrevubG9p7VizZs2q1hZfk8vlavK5QFY3+jwWnMcGjaxh1drOtWvXruloa1vT5vd4PE1wurnZ1da1+RNdHV6QS/VtXfeqjqYmu4vsEnuWlsLJImHVGEjJoFaQYiq8FI0mC4Sr+ktypJCMIpCeuEyHw7FkFhSDtMGgEzi6gpNWpWZg/OhhQjMVh54pAfaXBbtHEOx+N89TqXA4wRgkbf/NrIBeCZkb/Qd/2rtT/u/4cvf+5K3+Y8dOnjzZ19c/ePHs2bODg6+8MxrKFYqlzM23X9gp/xO+1j9/+8atiYmJG9cmozLGHZk7e/TNvc9/5/rU/OTQyX0//clLL730i72//OUvFyOpXDYbmSHfE3v8fq9dINnFqbGxscnFDBEcXv+NG7Npomlqb29vVJP07I333x9c5K1GjUqrV+VmR99PpwuyXK4AXuH5ZpW2dednP/vZRzobPE0OXCpwb3jss1vanCY6EslCfzNCs/UoBQ1OaVQzC1ZXAWRodCWL60XoLAZdjFc6VOmlcDpD0YKlCa27QGszGAw2l12bjvhBrlGgUZutjUY+E0nR7nZkPxhDShkLLsyFA2kWOlCPzQbqAylVA1TFM2XCasw2o1ELjZMC3S6NjWX0eEx0Nl8NQ6nVW6we9AfhNAa9RsWxzbyQWwQhsxQvLIDkDgaD106dvDoRji6mdDpVaWkpTdc5wz5Si4SzD/ugwWA0W1HZx/A+nubm5o2f+uOnd6BFToMDDXJAYbc3Nfs7Ozu7tu7a1d29atWm7u7uVodBrUJXKq3F3dltwfw45UKOsjZ6zFpBRbaJLfiEpGoFjINXzhNDkzudzcQQY9JyMZ/Llyq9RhCWpQJRqbQYf1DFobG8DeEIU4yHTRpaJmj7orNik+3pGbk8mVZ7YHS4ufTMpX7Y+g6dJiA2StHxC9DDjh15t29oKgkjSkjMzPT3g3wGST1y46a+aqCusFDmdQ1z6Fb0+0NXSlg5Tkuuiz0Mo7b7Wt1mAcM3Y+obqpxaymgc69DAwaZSJCcvvv/WW2+dnYoSjXViwt7gcBJGo1cxmMRBQwom2tTgbSpMjOPqaIYvlRhOIfVsfXDbxtXNjY2NHo+7EfC209Hot5hgTOOgha997V0w3DvQFpOrVHw+HSqmFm/3VoNKD1/qLUyZavLCfWaj08jqWhsb7Sh+WmQ5F5qZmI/l0SSXYnkVyVMms1rH49Q+KcSD4USh1wjdBMYRwKd8MV9IF3A1y4bLMvh8S7FKhaVhkGUygsBS9ZkOtKDYAVx0tZfWagiNk/8KhAQ0rfFhRB2jd9WqVd5Vj5GYSOReKhYTqRghZr0+m6Uwmj5dzJ0/Hw6/9NLRozvkb8rfpNTqHTi0d5PviDzf1dW11mdWMxLL86EQz4PAbEQdTquhKAVzNzOjJplhyvl0Np3L5NM5ulSSezkqjl3JUKvlH+DwIPTdA1V7Mpfb6zIUicruWdXR0eFp8LW1uYF2bqcgmFG9USntLqtr/bb1HS0+F/QLYKSdG9b6/f6WVRs3bu60w5HXBYfAUVscJismQWha3UW2ir5oNDxz/g9jS+hbn8nTnIKS85l8mVNWsrJckalyuXcd69ps0WIOO+CfsWQqkcyWSZHwXKmQiGajoSg0j5wB+uNCWiWfK8pSTzhZUWp1h986OhowAr8350pmXwMQt6HJbPY2wkElGJoZOjfS0mIqREbffPPNk6PXrl8dGRoaOnvi9OmzA3Y4ujyeNmKy1abS1PiF/fv3f3ArTm6LPTBQgCvJiyPAY+dzOp/f69SwHNpOMzjjz6K8K8usSuP7xMMPP+wX0nNz84FgNJ2LxmEskLK1UKzQ3ORkuCxrmzDSgYFOzk1OSj1UIZUhKrfDpCzjRFGZSc/NpgGZF9KlUg7d4ywOpc5ELc4E86RCgCdWCiSbAJUnla0wTDpuhaN0KTk/FwqFMqy+GnqwEg1JPorirR2f4Cql6upUIhJLA6EZOZetoAcDmpj1rktPjmQz6DkiK/UmrVqj5mnUf9I5uBTYOJVKJdI0o0C1goPHyN+ZDfhxfbX4J9hPfB47WkPpbW6ff+3GrVu3blzT4vNjpotOv8+NgdtsHn9rEww/t99jUDeu8Xt0jsZGJ7A/V3OLr4PftHXTGm+jh+wUW0CWJgqE1ZoweYGOJYVEqCZLWS00H8jSfCIqKvKZEtTebmhuZdUoJQRFJZsBuZmOJ25PhXN7tgxMFtDOSchPDBz51fPf/va3n//l4YHT5y9fvnz+9MDlwVOnTg1euXD1ysWLF69cPnPk7RPnecX186fe+FFdXmp//sHgraxWf+E08DzyU7HH42m0qahsaHZqamo2lKVUtkbPlSvTGUrjhc7t0VCZ6SughGcUJnwqGAeeh5588sknetzyLYzh0seNxqPzMwsRqadUZlQYX0JRKRVSEfSQTxVLRYxTB3tQqgSBKRVCOa1WW4iVKEUulC7aSSRcgi+yS5nIDD8+OROIpvMkK/YkElGQzteHBs6dOzdw6frY1GIsEQ4vTI1dGxocHBweHZteDOfzdH7izFFECO8fu/JOX1/fWwdOjJfYUqmU5GbHFhMwgmpy66/q3qwitqm+Ph+IAb0sOKNX7VdqnV6PfpSCYMBFCJTbOr0JpaUBRKW++g288aRJpIvFssih9SrLUKK+XETzJ1IFPGXCsM/iVEjlZz2jAfQJLwdGT+1/+eWX958avXQJGunS0Oj41atXx6emhi6BjDk/MHDqzLmzC4H5uYnjx48PXZ5YWFgYvzJynPyr+LBazZMsEHdmZj6aJbz6xo3pWFbWQDNQ2fCNBSQoVcrmsrlybGEhgtPCC5g6s8AZMDiP1IRghQMGhPEAstk8+tZW5/Nri5iih6m6OuPCHlVGKz6MicFW09RpDfBEZRH6Po+20GjYQDEMr+pdDddX8woAgFdU/XmhDE6BYfvYmvUGw6jUTAEzDtVov4os592zoZ+wIKxZ09Pz6BNP/dmfPf30H39mxw7ytCgBRCZsLylIRBCJao+ttXV83GBIJnW6f72pO3AgFjuXHhggL8CFj4jkkV7yY4l8WSRf2WObmZmYCAavXOnv/6D/3OnT58+fgXdyHi7E5EG9hJEAR5N8bUbrk/W112q0NjWPjicYf7sq5I0GNe5gIFZ9Ze2YtqgCbIiWjCDLgHblMujG8OQyQ5cldwT4MbRufDEYuAXNOh0Nl+C2Qvj28PDwOLkpNra0NLuMVBYzVBcovae9JZFQmZ2AgIAdJiQ3SDPkuzwP4hKXpigWV6hoBc5Y8EAEJa7BA4KWe1noWUQuinwuXwbRS+5m3fuQNsEZjQyx4uQeoSXW5Dp5hgyKoIiQ1RJbqZQpJEcF/u7RBVFjIW4RY2GSN8Xt8j/BPo1TiHVPOI33wzOSDI0HbvdDIMxp09HB3RScUfL63dWgQdVaeT+SC5Ah0K8Yd/WuXoXpKN5H+F5cCYQ7CVX3tGmDOm2AO1tw7tpG0xa0+XauXm3T22wNZC32RtjQpQc3ilJJZESkL56f3S7/DfXPex6Rn4TXPwKT+yZ9cxCUrydFfUP3manxffvGp86sc3mefNItGXHFhucEXDMQQE9RgDYLz0RVJK5Cwe+m07WegjEt/wTqsgu+a9Is18Xr9HMGlY7z290dHdXpPVMvUNljWbmZTBacX7Su2CRydkVVd2BVVc995+cHTvWfOPDz5/55h/yE/ATVK22X/ys7ffZCpQLVf0L0FPSHLg3s2zdw6ZChGhoxA3g+np580Od/9FG/78EpySHrdNl8Fhp4fj5TTFNlSqbS5dzCgixywP0MBpIRFZHFZCQmOuLR5GI4mE6HWGWhoGQFWUEnErRCFu4zh2nTNts1Nnvzx44t3VlqaCiNl/Avm0gVGQ7DNBpXb1ApMVxHKZ8rZDEXTSayNHw9kCpJ3SbnyHWH6drIxw433ZnguERUoYgyTDmbkxkqB5CipHM4fV1btmz5xCpD/tYp4KD9V2dnp5ZyjNRdkjNFUi5lyMcNP91ZGh8veWnayzDGJqscD2eh8+YXbjNJUMOpVDBStmIM9lKmoFXCSKiPJOi/IFfQ1xtkS4fbY3N32Dze9g6fTc8p9S5f55Zt27Y9sGn9Kk81a5G9rbOra+tDD25ev3Z1M2Ytamhrb1+7fvPmzQ+sJ58WdzNWKwmJ8GfPR2Zn4hiRM1OiqyEdkGnLxWr6Io9Oo0L+wTIYwbssgyxieHZPT3PH8PWO5mvDCaLSqDSYxmiRWw2grUPP8yqMD0mnM6GbQ1e7N3ZYMY0RgMDT04GFuYmbN2/OxkhAVCJvxxQi4oZiOpWfvHD40KFDp2cAFbb6vY12o5ZGwyil2dnWtW3no48+uqWBDV+/fGV0bCacLlN7eiKJMklEqHJ+YTbGaE2GSqKQCIJIixVKNF/NaqRJLUVy8dBCMJpKVTCrEa9QCXIKLd9jIPnX07kcTTIZUi7zNp9Xb8H+pwCJVcQw5aAUqjAtOElMJzOFqqmhXBEFuVTIpNCDobzcl/8S2uQvMbqfzf3gRo1lS/eWhx/oAhW7a9u2bo+tqXPTpg0drV2bN60BzbX7wa2f3PnIf3po8/r1m8hjopJFS274/3gpyddRQiZKlImlGK1HHmU0sdGb/adHbj24yU2nAmffePHFFw/eihH/J3eM3xwZniARsat05UqppVhs+bgpytdZVAXaqc9SdKGiBv1EKBbVDf4mNVVRAnZUsKj4qSqUnA6H8mX0pymJXVav17qYTC5+3JTltf58mNQyQJzA/izUhJRaX8mA0qhTY/dgNAYB+gCrc6A+6nK1tLX629uaPW4XKKwelxUDdKm1GkzDa/Zq6TJ6gDIqg9HXCqd9/vb2Zti3rWnOZDwe4q1mDJLsFcLziCdAn6hGMcAFbLpcBLZcKiqMAqnsweR0gLmULPR4YFqYgKd6q8na6m6yqelYQdC5GnVEIHql1BNAVMnzJi4Vz2bk5PQw6PzDU/EcaBQFWoV4W47Nj49PzezcucmS7ccgSPv/DQh+4NDFdLrvZMlbyS7N3L5+PUD5H/jUl77yla/8yacevj6HqQVl8sbrhw+TYjXIVy3GF81xmQI0fDoyD9J/Oh6NRsKRWLIkF6tGkRV145Zdu3Z5lMVYKBQMTAVSlJ/QLbQbRCMv+xy5a8C3hueCeduGhx5ap84p1EraVy77pB4tRlAX4NXgcdpsOgPOGVnVKmM1RBRGFtKruHI2lYonMrmSUofOcFo9PLhGrbHb3e6ylzI02F12m0nNMnFMARVKZNJx2M/PRjdtBPIzouiRqerI5LmKjHKVZko4M8ULpCQrcBa1lwP4k6HVyl59FTyWCsVcoUIojsc0JrzeotMK6AYSmQkHI8m8opLLBwMpkQaWVBuNXpTrXgJ7Wq+jltOMY3vqcp/Lkl1PNtMKT8v2nS2PcKEZn8PnqCVOouKE2LXaw4c1mmBwfPzddzUa/nvXr7ejpdhBUVsZHa2ET58O4z9GWzhwADMq6dSnNfz5/kWODi0NDiyMcJHs2NzYXC2xEqWroxQ1TgxAnczeuyjF6TB614u4GFs1cqSZ1lvGKDMhnzOzCgQ9tCISSc/fUqTmokGybG2GFkNaLyAwg8HnI+vF52oGQzHqXEttxTczd4sOh+t32OAOC3ono7eJ0ej1euEN7lLmcjyPf5Jy3Ngc9RknopIyevSCHJXPH5GEdHp+fh7eyC0RTZtJJFLHgBu9tbgwiGgNeoSSVBXj01Wwpjf4GmrheZzV/XpRa6vmTbdFQ9ALIsRlMMRiRNve3q4jcZAB8RXHCcnVcu5cy8zM0Huv/vCHP3z1yKWZL3/5U5s80Ds8Gx/7stSUyczNhRdnZ2cD4SRmGllxnISKusK1ldMk2ovnUpHgxMRUIIguJQvTE9Ld+XdsBc0yVjT4DL71CrM5kbjTDIaYIcYc1E4+oMjMAS0VExOx+D3UrEbuNRiam+ttoIyky4Kl2VKWgDvu3IkPKSmt7xRLnwmf2ioJWHH8h0oqVUwyPHxq6o7NwIdK9GFxen2txDKUJ5TTkhJDN8aOHcMSt54Kf6ZUfEcS5upFVktMTp0aDifv4ANctbDV1taWV9Gwr0Fv4zaqm3zARmqLbgCS28ZMUWOEHZo/ogg9pOSq3Q4uD4fT89D1uNmJiVt9fbVyt5FaPJ1tUFNXg8XsbHJBO5vMZqvT63PZHK4m0NW9Hqi7k6lNc1CFQgWV1upiuxKTBjKSc+rqWKyclbPppo0bGnROC1tSUIJqcujCNcmJuiMMbLVSGRt4++233zny3nvvHTh48GyYk1yJXHppZg6VfGjkwOR0OJOKR2PBcCgMNHACWCngZD/HARNHyV0NRpFM5ssrLHo+0vJ+aPmSbKCWW/6RGHOmIdoBLT8DLR+PTCyuaPm7MZtZs3/j6habhmxF00hFc7MgNEuCsO3AzU8mEz8nM6LyAYPhgQf+4i8ekAR+aeLM4OQcRS6JyqFYbGhoYWFIWs7FvauO+X0OB0bX83VufeRzX3rqqae+9KUvffUrn/v8p7etX7O6s9vf2kr+TiRLoqrdbxNwrro4P4yfmQsXQlQvywkse2tJ8ul0r7322r53RoLKNnTlGBgYBCp32guLCyPvHjz07rtkTDSoIpFHW5Y+gAuPjtHWSGS1qNECQ4UmqnAWy5BK8qlxIoiTo1NXTxw+cvz4iRMnD7915Mi5iYVkUaZRTxwUDUCv370/kQGqlULXztD0DVETDsfQ/w1jY26t21ttBm7xA1KLkuZ0EFwhgy6JiaWWD2mKcSgF0JFAoedUfPuqjo42f6u/bXWHQd/5sx7MWKVU0oV0KjgfCATC2TzjXrdunaOYXhzq6+u7HD1z6fLIyMjF0x/0vf0mCNR3T5waXIQuMNP/wcGXvvvd7yr3HlloO/e/yGGxJxyORhbGrl4bGxu7OTYVToT37z9yZa5oBrrrM3MjR/b7/W2tHrNGh4ZbZod/lX92Nq5q3PDpL3zhC7s6GwUqPvtcT7lCV2cjKnKlAMihgKlsgAGBxlwNqEVVItEqc0pEY9EgOgpGo1EEpKF4LJ4Aea5MRPXT0xoA3D+Avi4pqmKpFw7v+fhcrY887q2twqEvib/l/hT8MDFb7k/Mto729tYWf0vrqnaDfu1v/sN0bdp75K33+o4dO3b4zTf2/foXe/fufeX3B45chaIu/eGNX317p/z3+FJ+99Uq9X/7H6d+c3z237ni/0fzdCSis3OLIDSWFubm53C9bmpubm5yfHx8OjAfQEizMFdtv+l/p/3ubc5qa3aTWi7TzTgelPdsHCPr1dAoRCfS85HCc1whOj8fKVK9NBxIzuOnT3/w/vuH9HAhkWm6EFpMjty82n+cnBTZP//zr//tt74l8t/61t/+9V/9F1BbJEznLDm192w6o5UVYLiTBpHV8OgbJCI3oTiNRknJaNirrOeH2URWZF31tHd1d3er1fDW1eGBT+vgaK2HbBRtmXu2bKESAR1SAqzua1+3TqFYt669taP92LEjwNDfrgYtsoHQcAKMPzc8fPn6zdHR8VBkjssb7NJudakIaifL4hRWQeSKBfT5yYlEKdpU92wCR5sArK6wz7hTWyd9z8ZwqkIFkwYoRLrBxD3HcqaGBhMr0bCXdluLJVqh0PzhyJGjx0/29Z25em0wOh8qiaRPZF9++cXvP/usyD/77Pd/8i//AoNLQpQjOXX3bFoVZzPpdTrSKLIA2KHJMQ4ihUmt89gUd+f6P0RVi0VZdefRWCwatMDq6EAjIk2dqqkIdNx7iCs5XlUqcYkXGPTevb/6xQsvHD/+wgu/eOXVKlXthjw3FwmNj47evH55ePhci8vlNJskB9yCIYVQQ9Kg8yFG0lNpNXWqakw0J9xD3HptN9ZxNOZA186j82NwfhxGxO1cpZcr5+IYzFmyt7a2tmB8BG8LSKlWf/24tVXSadFrha4CRJzMU3xv76FD390hf1X+KnVtUaSPv/66Vqejb5wPUfWd9jM7Hn744R2PP/XMM888pWN7Oc6Aa5BOyZ7J5GRWhW7amCQuXz/OZ1ZYXtYxD4hyRkFLTCuwPQ7zs7Hwd36G/NuPfsTa7czJm2GEOVUfAaAAg9MyXCKZqxTy6WWkgHY0GrTKXyCGvMS4XI8/LjG/I/KPJUYJAlBiHKRolJhcbmGhXgM93KWHu/Ro68JoGxq0EuvyPOpx/ZEkMoHBwYDEUgPBAcoHH/OxWF5ieTbN8vX8t5jjuKNOaapc6OVR2c/nSYkGZQjTN+WyEka8dGNMiSbQgW02V1Mzxvp2u2ySRk3VkH81frZ6koyc+M2pkYsXR0aH1PzQsWPT/EJg+vbUAtXs7DVZm7see+yxNa1tGzZsAM3YarU2S/ZkkjF4gft7jEyyUlHq7S0tLU4DX6nVbgtZjqTIOyzQ+5CvYfZ2VqUzYqYXu93hkJxWa1NrZ2enx25v8K9Zs8aPcSL9q1a1Nlol55XRK5cGBkeHLyVoKnjr+thscGl2KXj22JF+ycnzSpzGF5TKygwG/hoCpXbg4sXrKV7iJiaDOZpRiAYZrfS4UnRuphpndnZScmLWOwz7qqmaPcD4gw/qagit+8aj300MVJtnN7lEXVTsJhmqxFSvWhltttdczkP/KsrlTAZkC3ohZgplST06dG1ifgkGQDgaFXlQN2LxyFJgXIJhXkrV0nSJwoVzx48CYL0YKEm8NxWPp/OgjGNYPh6dV5RGQMjSbks0JYPmYBBILldieUEN7EOt2GPrH7g4eOnK9bGp2ZmZfH742o2rw4OXBgdWjMStd0ai0wiNZnQ2OgAy0qVerkTzALJ5HImrmtF71N28qnXlsWTQV736MZBsfc/+8vW+vr0/3CH/o/yP1GRQZMZH3n8fKEiHEVDe2Wsdjbiw6Gzw+XwNPA/jUUAYDz+FGpdQXbfAOASpu8f3HQ22ri6bxDb6d/gbt0H3nz51alpi2ZHJEdYKH3kskC2B7C0Vln1/uLt2wmPiHnG7/FU0FH5G3HOfMWokizhGH3/cJTEh8r9xjBaLSonJExuO0YWFnHQ/++Wqz9IgvsXua5+cx1O/wbfH7nvegaeO4VvgvucNeOoSvmXuez6Dp97DN5245/8AG5PifwAAAA=="),
    "pix": base64.b64decode(
        "d09GRgABAAAAACg4ABEAAAAAcMgAAQBCAAAAAAAAAAAAAAAAAAAAAAAAAABHREVGAAAnzAAAACoAAAAuANEBBUdQT1MAACf4AAAAIAAAACBEdkx1R1NVQgAAKBgAAAAgAAAAIER2THVPUy8yAAAB+AAAAE0AAABgXGUEIGNtYXAAAALcAAABpQAAAiwzvbcaY3Z0IAAADBAAAAAsAAAAUgemEkZmcGdtAAAEhAAABu4AAA4MYi79fGdhc3AAACfEAAAACAAAAAgAAAAQZ2x5ZgAADfgAABgOAABT5jvWnTpoZWFkAAABgAAAADYAAAA2Aty+vmhoZWEAAAG4AAAAHgAAACQHswJUaG10eAAAAkgAAACSAAADcEcDZmBsb2NhAAAMPAAAAboAAAG6293HW21heHAAAAHYAAAAIAAAACACJw6xbmFtZQAAJggAAAGnAAAD4lxCgf9wb3N0AAAnsAAAABMAAAAg/58AMnByZXAAAAt0AAAAmgAAALJ0Qd0yAAEAAAABAEKvW9LhXw889QAPA+gAAAAAuRj6DwAAAADm9oCc/0X/BgRlA+gAAAAGAAIAAAAAAAB4nGNgZGBgYfvPBiQf/Xf928+SygAUQQF3AH7bBg0AAAABAAAA3AAkAAUAJAAFAAIALABaAI0AAACFDgwAAgABeJxjYGE6wDiBgZWBgamLKYKBgcEbQjPGMRgx/GJAAg0MDOpAigvGD/Xz9mM4wKCgqMTC9p+NgYH5BVg9I0iO8QvTHiClwMAMAFQJDNoAAAB4nNVSIRKAMAzrgK/xFBRqagrNP1FTKDwcZd1CobtDInIcIU2THo2nqVlttKPBeQNJ40IGCbKHvzwEtb35u85p5Ebf7P/Xjh97PjJX5hFuKzPdkLAwT4xwaeR57pHu+hZvOYj2njPHsg93yjtyzCtP1AiP98PsOI+eeub1bkqLd2R9PDuZmLHT7X/ZaD4ASk35GgAAeJxN0fdvzlEUx/HXU3vTFkXxbVF7z9bee9Xee+8aP4pRWiP2TJTaIzaxxa75KxIknn+EuqlEnOTec3LPJ/eez/siAaXCSg57rCSXTkgJOV2kdOi2lWmj4ti4hKKEH1FyVDdqEKVHGVG7KCu6mpZeXBy0kXayFMaySzRJUUqUGkUlmsx/msFK4nfir1/xvL91fFG8IJ4TT/x5MLycHg56KYrl/u3FNsVyY/n+jzx7XHPYU5/cdF2Bkw55Yadjdjhil92KvPbGPjfCjRVUVFlVScFTTbXUk6q+BhpK01xLrbTWJvjrpLMuugafpx11xgcnfAl++ulvYJh7hJFGGW28CSaaZLKp5ppvgYUWW2KlVVZbY51T7iv0Tr69HnvmieceuO2zO9a77L3j7vrmnre22Oqrj2454LvNNrhim1zb7VcmcC+lXOBfVnmVJKqmuhrqqi1FHVUC8SaBWCNNNfZSho7hB9rroJsWFgWO3fXQR0+99TXAcEMMNcw4Y4yVbZApZppmutlmeGWWFZZaZrkc86zVzBwXXXLWeRec89AjB/8AcORsWwAAAHicrVdrWxvHFZ7VDYwBA0LYzbruKGNRlx3JJHEcYisO2WVRHCWpwLjddW67SLhNk16S3ug1vV+UP3NWtE+db/lpec/MSgEH3KfPUz7ovDPzzpzrnFlIaEnifhTGUnYfifmdLlXuPYjohkvX4uShHN6PqNBIP5sW06LfV/tuvU4iJhGorZFwRJD4TXI0yeRhkwpa1VW9SUUtB0fF5ZrwA6oGMkn8rLAc+FmjGFAh2DuUNKsAgnRApd7hqFAo4BiqH1yu8+xovub4lyWg8kdVp4o1RaIXHcSjFadgFJY0FT2qBRHro5UgyAmuHEj6vEel1Qeja85cEPZDqoRRnYqNePetCGR3GEnq9TC1CTZtMNqIY5lZNiy6hql8JGmd19eZ+XkvkojGMJU004sSzEhem2F0k9HNxE3iOHYRLZoN+iR2IxJdJtcxdrt0hdGVbvpoQfSZ8ags9uN4kMbkeHGcexDLAfxRftykspawoNRI4dNU0ItoSvk0rXxkAFuSJlVMuBEJOcim9n3Ji+yua83nXyokYZ/Ka3UsBnIoh9CVrZcbiNBOlPTcdDeOVFyPJW3ei7DmclxyU5o0pelc4I1EwaZ5GkPlK5SL8lMq7D8kpw9DaGqtSee0ZGvn4VZJ7Es+gTaTmCnJlrF2Ro/OzYsg9Nfqk8I5r08W0qw9xfFgQgDXExkOVcpJNcEWLieEpAsjx1YitSrdsirmzthOV7FLuF+6dnzTvDYOHc3NimIILa6qx2so4gs6KxRCGqRbTVrQoEpJF4LX+AAAZIgWeLSL0YLJ1yIOWjBBkYhBH5ppMUjkMJG0iLA1aUl396KsNNiKr9LcgTpsUlV3d6LuPTvp1jFfNfPLOhNLwf0oW1oKyEl9WvT4yqG0/OwC/yzgh5wV5KLY6EUZhw/++kNkGGoX1uoK28bYteu8BTeZZ2J40oH9HcyeTNYZKcyEqCrEKyBxZ+Q4jslWTYtMFMK9iJaUL0OaR/nN4WInvkz+c+mSIxZFVfi+zxFYxpqTZsvTHn3quU8jXCvwseY16aLOHJaXEG+WX9NZkeVTOiuxdHVWZnlZZxWWX9fZFMsrOptm+Q2dnWPpaTWOP1USRFrJFjnv8G1pkj62uDJZ/MguNo8trk4WP7aLUgu64J3pJ5z6t3WV/TzuXx3+Sdj1NPxjqeAfy6vwj2UD/rFchX8svwn/WF6Dfyy/Bf9YrsE/li0t26Zgr2uovZRIND0nCUxKcQlbXLPrmq57dB338RlchY48I5sq3VDc2J/IcNn7Z8cpzuYrIVccPbOWlZ1aGKEpspfPHQvPWZwbWj5vLH8ep1lO+FWduLan2sLzYuVfgv+27qiN7IZTY19vIh5w4HT7cVnSjSa9oFsX203a+G9UFHYf9BeRIrHSkC3Z4ZaA0N4dDjuqgx4S4eFD18WLtOE4tWVE+BZ61wpdBK2EdtowtGxW+HQ+8A6GLSVle4gzb5+kyZY9jyrKH7MlJdxTNneio5IsS/eotFp+Kva5086gaSuzQ20nVAkev64Jdzv7KpWCZKCojEcVy6UgdYET7nSP70lhGvq/2kaOFTRs84s1ExgtOO8UJcr21AqaCJJRRsGVv3IqTmQjGmxEEb95J/1SFwqhPY6FxGx5NY+FaiNML02WaMasb6sOK+Us3pmEkJ2xkSaxF7VkGw86W59PSrYrTwVVGhjdPf7tYpN4WrXn2VJc8i8fsyQYpyvhD5zHXR6neBP9o8VR3KaLQdRz8abKdtzK1p1l3NtXTqzuur0Tq/6pe5+0I9B0y3uSwi1Nt70hbOMag1NnUpHQFq1jR2hc5vpctZFP8YHmW9e5QBWuTws3z56/rbMZvDXjLf9jSXf+X1XMPnEfayu0qmP1Uo9zOztowLe8cVRexei2V1d5XHJvJiG4ixDU7LXH1whueLVFN3HLXztjvovjnOUqvQD8uqYXId7gKIYIt9zGwzuO1puaC5reAPy2HgmxDdADcBjs6JFjZnYBzMw95nQA9pjD4D5zGHyHOQy+q4/QCwOgCMgxKNZHjp17AGTn3mKew+ht5hn0DvMMepd5Br3HOkOAhHUySFkng33WyaDPnFcBBsxhcMAcBg+Zw+B7xq4toO8buxi9b+xi9ANjF6MPjF2MPjR2MfqhsYvRj4xdjH6MGLcnCfyJGdEm4EcWvgL4MQfdjHyMfoq3Nuf8zELm/NxwnJzzC2x+aXLqL83I7Di0kHf8ykKm/xrn5ITfWMiE31rIhN+Be2dy3u/NyNA/sZDpf7CQ6X/EzpzwJwuZ8GcLmfAXcF+enPdXMzL0v1nI9L9byPR/YGdO+KeFTBhayIRP9ei8+bKlijsqFYoh/mlCG4x9j6YPqHi1dzh+rJtfAGNMAUcAAHicNYkxDoJAFAXf/664QGJiYUViowbDKTZkOyuNBdRwAI9AY0KjZ9kNIQFpLL2VuhBfNTMPxw7vc2aJHrmhXkKiuFos0gYbRuKo3YpVwMHEMUfeyH46+BAVGOHPwnSAwmu0HjNA2x3Vp8yoOnNeantw3klMATqPbOzSU1Ygoeri8j/c2j2vPV4mHX1uRtwtQzfz0oPWXxQBLKQAAHicY2DACWqB8BeDHUMTQxNjOUMsUyEDA1MhiATRCDaDBAQCebUgDAAggQj+AAAAKABcAGcAvAENARgBcAF7Ad4CGQJhAmwCmwLaAwYDEQMcAy0DOANfA6kD0QPqA/UEAAQRBBwETwSdBLsE+QU0BT8FdAV/BYoFmwWmBhQGHwZdBo8GxAcDB2AHrAfNB/sIBggRCCIILQhvCLMJGQlcCWgJyQn9CggKXQquCrkLEQscC38LugwCDA0MPAx7DKcMsgy9DM4M2Q0ADUoNcg2LDbwN8g4kDlUOiA7WDvQPMg9tD3gPrQ+4EA0QXhBpENcQ4hEgEVIRhxHGEiMSbxK0EtUTAxMOE1sTpROwE/IUNhScFN8U6xT8FV0VjBW+FfMWGRZ/FsoW9RcyF3EXuxgJGEkYZxi0GPsZXRm8GiMaIxojGjsaYBqJGr4a7BsUGz0bfhu/G9ob9RxBHI0cwxzuHQkdEh0tHUgdZx2aHcseHh5xHpQetx7cHv8fIx9HH2AfeR/AIAcgNCBhIIUgniDqIV4hoCH6IlAipSLgIxkjNSNdI4UjuyPrJGMk3yU3JWglqyXSJg8mRSZwJrgnAicwJ2cnmSfIJ/woPyiNKLgo4CkIKTcpbimOKbspwynLKdMp2ynjKesp8wAAeJztXEtzXMd17se93fc1LwIzoAg+NIZAxqZCSoJh2pIlX9uyWLIEUnHKBdEVZ5iSVEMHSZVILoCFrsKYJadUWWjhOJVs8gOyknZZ+Ad4FUmJf4RWLFeFyuKWSeY8uu9jMCRAhZIrCkcFYuZibvfp0+fxne/0lVCiEELeVJeFFlZ8LT8hhNBK6KlQUqpNoZScBPBOnhfCmjCAr+lBaJZOrg3GgxNrg5VCbpdFoS7feg/eCUHjqcswXip6Yj1/ShgRaBNMhbVyIqQcbigJU0yiUGk90ueE6HU7GXw7HeArtssw9Nr62hDGXl8ZwtuVooAZyrKUN4vidga/t3fu3OF59CfquIDxYJaP4IL2V2G8JbEsXs1/DH8NhAymMGsXprS4tEwkcZZMRadjJyDXcCOSxsSTXqrieBSfyzIhlg89cjBbypZGw8WFA4M+yFiJ2AcRF9ZJxCGKqOeK64VWNwp63TrQEF+ATCzpFul9AaT9cf4jkLGjMy1DIaciEZFNoqlI03AiwnC4YWQQ2Ek3VtaO7LkoCgIhUL5oIXIiZmkSBzaAfaJt6tmlkxJEG8PPrJAgzO2saAvZEnGOhj8sark/gRkqHWs0EBFIMRVBV4Y2CPeh4yDYreMgDUDH8B51LFfW18L1FdKznBFfXQZB3wXZ4R9egt7CN7N6nrOK/xDeSm+AlQ7EULzz0vunXnk1PxVLbWUQ6mCaSLR7sla8Z5JGypjRBmyDf2/OLedfhz3SKrlS3yf2vu1CPhQCFwxTD/wrs0fJ6sfDFTSqIe6ZNyZ5E1aH68S3bEvOhnTla5a87dn8adgfqwI7hbUqI9UUphYTkAUEMgacTjZ8Dowlgjsted0j6NCo4KGGWWlmuQ0zfQovUDZMDx/h3/asPbDcjfyHoVSJ1JEC14qyWAUmAlVAWHGqMMZOUklW2+9HkRD9hT5seNSLnBhOig5IIf2GoxCDteE6fEIN8G6TTAVLQxopaIfvGJQHdjihHf7tfzfjEOrmyfwUaiRoayQI9MR4jTS0YRvacCGONMF6wDBX208KGvhu/lwkZew8QIvAaIp3agL2QOsPJ4kMw1EI83hXrcJJihEPV71ez4hGUBZ+Xlx46aYuMFxXMTsVp/PHQRodoO6lrFcWwsrcjNVMlmJrbWMYutH5LxeVJnFc8pWuj6lzrn4s6qtbfFUa8e/iuog/kOrf7lx/f/3kvPs+FE3ZY/HV/Phu2UOIcqMAxY5FjFIbe6ghNctMIpdVNHLZqwMu9Sf5ORFCxgnRGVOTaG1h+FjWurE2mmQyikbRuW6Xo2h30IVNiTrRbATtQARdgH2RaItkmSH8gPdB7Cwx+HBOepetEeOPW6HLgCfyxzD/aM4/MLupst7MroDNr3N445isLpflrffK0o0nSlrjI/mIFgG5WY4kjoJSaovpeG3Aadhrfps0P+J9pB0TM1c/vl5f3eKruI/XZ/cRX2Xjvg+vN/0L88AZiIWQBPSmMqpthZatELWMwR2czOs2AqlXQeoqxN/0Pn6TMlPRijU9nyNF0pWpTFIMcTCZgW0WoVbhtCPjWECEo6m1DiYZREI0pcXFfl+IxaVFSDQceWC0nou8PXuEtT906Rx2AJL70MJP4V7ofbAf/N7F4KYlh+JYfpiXDInwoobkMVK4OaEIB4PAHqTdwfFgF2FtYiZ+PJc/Y6WE9K4kWq3QYCRTXkgaJ5oUiJgkW8hmsUjmjYeFB0yGgQRNAV8ULdm90UYhOb7n7RPnXhDP5N+Ez5GSEc5rtbIYv0yozTSg+ZNY+SiyIBaqfJXaY7XWBmS3+N+gqF40q9wGVIhz+zx8g6zoGOfhYg5aBCOCiUOMo+34GflothstUrpaq2Jnhbxo+WBSP5d/3cYB43Zsa139+Ln66hZfRZ94Dn1CtWNb674PZ3Z1SfzjS+8ngCq+IaI0SaMrwsTWmiuQomKTxNPGQtPUL9Ta8GLGC12GnaEbk2t4Z2zstf3eeiE/xMBwV6rpVOriVLOKUWdliCbj0w3jqaJSXrkD6Wdn3orbOAr98zv5tyEiQ/QN0TmFAQkBGmknIuI/C3mQcADnwYYfpuiHTjSfn1Z5Nx0AGuzsuPRfFG9XOfgyZZIOVRqQ4UPK8JBTQsgpxrhIZH1Owfju80rk8grO5bBWnefR0bcLoRqZtie+BZspAV9JS15qMG9BdRMaQju4wrvaqHdR+G+dUB2+dnZc6i1ppjvXq0gHEQGwSgihVEElYEBpxr4m4lhPILLBbgOIwpWNxHxFhuSM3ilYkaBFzlCQtMqi2uziDnhHQ5c4+9+w5Z4SodEmvIJ6jVCvEUSGCAIuxllRhdnEhdnl/DTfoK/t844L+eJ8O3C74uNxtTf44u0p/Q7pRh5C2TFDQJEaBhrtQEG+Bf0B+IyMBVRf56YoigEFUCnS72N24swQ9AIPjWcxgKYYx/sYwmecnlR6O5PbDAOq6NfMDlaM86OY/R0cDjzidGgzJLS5gnY/ILfj+mUGwT6R/7ENFQ4C1TiCaqUq8IqQOkqjGkhHOKQH0PAv7zznVfy35c2n50bD03Oj4ek9o+HpKhrqRjTEnflB/j0oP6A8ADwkpqlFfgFWQNELkYJDyP0+pjrejayXNZMdhq9xYxd8nvam4eJ+6XBt2ZTBUqZ9If8+vBN206ShrpUJtgrazCDXkj6zDDVaZ9uWbjsN3Z6Bf617XzqgUDQVXXCd4nAq59xlyH8r4i9zMMdwoaOciQpwcTntSrvYU1D4i2km0VVSiFzDjb5Ecx0ODmgy2EcfPXx4EVzn0ZVHV74yPnzs8LGjRxaXF7GchrDfStQjSNSsslpt+J8m/mKF4Cyq0AUip0SfBfyLFcxXPq99HVQCDubsq6/7PPpq4Ykz5EsfiV8Lt+MOlcUg37sulsEWw9/UFREGBmOajl3F3ipTL7r0tJw/4W6AYAZ3QIy/tsctF/IjSQIZRyS9hEKIiQ0EEcaAKWBArGzHtC7LeHe7ROqspDjGAa0s5mGiL4BBe/ohg/Y5M2hPfykYtKcfMmhfcgbt+w8ZtAfEoJ2dy6Cdncugnd2TQTv7kEH74hg0V/dtQ4xG2/96/iTavkDbh6pWB5gBEZc5cmsu/l5wCEsPPBvjUwRNoTwbR3Hnm/l6I8tiYjeCCtjhRsxuT/xPGzYlsOxBI4sOZmaq5tKe46M9ThFTwCxWG9Aq5klcSuSXgrvJi2nsYgz6GayThv2CyjnrQZawoTEoXsGbFWpM7UtjHLjuqrGHjONDxvGzMY6v/oEZx5/cB76OBEZbm0iTWoPoL5qIKAKhsiyd9GSajtL7xdeDGYHvhq+b8t8bX0O9OgVHi2QQY55IUzOhaCWSJJ50mVe5T3w9K+R8fN0QcY6G/z+xsD95yMI+ZGEfsrB7s7Ak15J4MX+BeJMYkVUsoiCGFASOQrV2kvhSC8LDJEu50gQhl9iHqzzU4TzEoZVtaZ1rD8xFFWnmkza9bmf/l/jgn83NVRj/sTKoc1XmuKAuKotDitvRDu/o4iKmKUZBc4BEb16ScgLWCaqZnCp5Z3ITWty5/CWQKdUJcz8dqKEDzuGufM7q8rk2tLqAbiSl7mxSckLVCalORrVIc3T4kAV/yIJ/cSz4ZceCl6K+vsXXkVsokFvQnlv4kvDkpeNBOhgPAZEJBmQhkonB6zAtoZ5VMws6okMnz1DQxglOMO6CpLazU7Kidxqj4/4+lZ9u4D0DYcWEr/l6YdXuAhvRcmP8lXqGYufqVWdSP7/6xdUiLc6IGBXpRtegsmAynzEiNoWryIJxKVWSbc50IP6eLec0gAyC1ir29T7YvMHkWtuBtdHFlJkjMB28I0RIvc9bLuRHXVAaZLtDUkbc5xhBNTk0/oyhxqc6FOAWUZ61M5VFsZuJ/dP8FYFELJbDCKENFjceuDSEauGWPflYlsk6z0bU4qRCKQi3NCILiFWjlg4yfIRBhVHikqY4baUHJx3R4QAX2cMY4JgDI5OjU9kMz9AYytnKN8+fRV4Ig5UIIqf1mnvHjHlv5ml1fQwAcTxsLQl+eEVFhRSLWVz7fP5dgbzTJuSDGFAYKBpqFBsi8eurFFDwfooUhmAND8OlVsbvvKypzUtsq6siUFIFiKkBLNDCPabHpoNT8HJ+nL8nr937ixfy3uxmrKHv08ZjsVH6oO3SY8PuFogFOJ+/HEoN6UJqhX2rTpZ0oL6WkRQxV5jBhPgzkWVVfc0UAMLTVrLrIUJd96i0xqm+K1FWtkbwoJHf3rp6+5dX5+wWSrUJVYSEwDcFOSFPayonvFCQePbYrTXOvC5QoSi8VW7LOK2hBByrYP8aLBcZ5eoGslzHGyxXdPDkGYpQJadGAGF8702SfYv3+jjsGwxxBaG+DOotXN0AqxP2YgRyH8dMdgK/CI52bY9vXsj7IEK9wDg6AsGel+i17QJM/WpIloo3WLLHGtal5Jv1dLhCaj4cRwZitfU18fb8713Iu01GnBLQmoc7AwennZ4dtwOogbt3j4gj4q8+MK8/n/8FBmKj0TVVGKhLqQwHWT8JYBIz7UB+jeSmlVj44i8RXTzQ7elIRK8cOjQaHTggxKEjh44cXh49MgLDPDA8AJVTs0m3YI+e/MCI58fOOwA7epJhwEWVR2EOev3+V5Wtvi1dOCPKFmBtWK0BawBex89oFX+OTBoEcSTSrAr7SUdLE8ppCjgoiEwAyzImuggATEcmegUl56KgKXc0iOYxVgegOMAlIOgFDQ8daQ1WuFpxuCVv/q33GLTgWlyXFcNjUWPy4A2SfghY+CpJfgmgiwg3hZUJoHOotfpKgt9Nu5mKRKyjGDZBp1IlWl3Ca1EyOdAb6CRKzo9GtIJjI4DBhykwBMOgZp+atfSCWwMWOdJFCg+LV7Bv6lZE3TrnpgUxF1RT1BV2yWuqOiyiqDoscjKnw+IYbsE8i9ym71vuYkFcUfo14csRuJMY/Frzxo2AOi69NVO0IqRGI2EvT+DjStjWUFrTA0x6QmOfnzNek3d36NIh4203YkqMtTB+TFs1MypUdo+eiR+fZfYQ3pUQvoZwWqBOZKCopo9Cq8FiUQv3GH3sitQ1dhpivls51/W8rHg8/2rQoF2cTrDKm6MUrudYKWS+pdPzZxxvoW4aOS4DB93Vez+bP498lVNvvAsINnhLhwIJlLQwYDqDAal+qhBgDQuKGfTHLMKL+QsERZvoL2ossyGB25V59EHq6APe9xVGoduc55ii8sGY/aYE692GO4/mywKbuJveewQ+c1c5zxkXWj7zXTVLjIjj5fzFTAa9jiKSzsowQd5WwHpBAzIlwtMMN7rcbzh4UIiDywchrOwixPpcW3sazAFQpnQWmuVrpXifHEvuxddyIScAVmBB/71IERbqxkpmiRIpoCDDLEBHkUhCYKCDezwIqkUazCKgFlvH3G8rO+96lUWzQmbvxLMh3v2NbPcc944EshEJiLJkfoZR2K7zCB6ZV3XaPc4jNLkgp2xKQDwqRMb92cl4yE9y3rlz5wdgXb+Gu6z4p9u/cScOwPP3Pw6mCr5Lb+37LnXD3YUMPP3xOOXEe96JaWyJ5/TZQBB2xB4xdtW1UAHY0eyG3btJTBGrwWj5uOV4rHoWtItv5Gt1j7sOV41Z5vKxBF2HHGk5NVTNgKJhDz2xw2jxcTwEoaFmCYXhdgATBXXNhCRL5HmZU/x1QI3wfYG0zD1v2AVqucZy/BhXWT5s+eQ7X0oIKFpdQcoJQTdMjtCRGg5VrdCUkr4eXsPvG8Te97xhvpRrDQCzMmxEGa61ZGO3VvOviHbams++w0DIe9CDzXxma/coXot4FHPuKI4/8WnfnSt5gLgHdC9KGumxfMwdhVDDtt9lEA8X+BgaUcNKUEpyMQ5G4fvFhM7azB1FtkZhjvmBjSNxnPqkjqq8toUjFyip4S3iM9xBiFHdAN1hf/iH+VlKeVBcYhqUhs+gJVAAhsEm/AI37qQZZMnwPDEh8zvuXWJEvL+sMYh27x2r7I6YtF4szVsgzX81pAExNCRgEArmFzqA6IVn7rAgEE4asQ9p6oJ03HjvuGMnzVttaVztTbp5Kj+N/VVJZ8uEO5uKSvG5CDQyk4rONFZd7jUyrRHP+gniinGBvqAVc0Zea3t4S4N/INurewAx1Z/n85eFjhWUz3EK9hPjximo6FQI9tRiNP3xuSThk6zJIHFlmmdgO/YQGRT3YENKRmMiTrAj6BIF/N7ZKV1QptyEzVifQ6GyWBb/wnH528KEoXmzKztCdC6jzuOA2qCJihPsozPkiyJOWiBlmgqQkrrby4B8TGjewbs7v7jPm+kcxFyYhkRV6HghshvaX17wmChMv8mI3WdwZNHqwXRI/3jyAYSx5DsWkl1g0ZOjyHF2qVGO5/YuU5N2GTOofIqUThGirpdY35z/P93Z+ZRTCx4rpMuiOjFwg9DrxfynkG2R1Maue09HPfZi1UVerwuK6oKiRB8V+Rr5kXD6yiYiy0bZXrB2qWLSak9rEnyOTXO/Pv3d72Z0RvahUWb9CciMddeS+CnhrB/heSypSPI4ieKpSDoyFUnqjroYan2AFW1SSyLDHsh5POfWX+q7INTqTmAQYnjWOOoyHowX3ElUTq7FrX9Q01v/vMNgmC68h7+ZRC72lBWMMEIjjFDW6IHL6pTclrWYFRWREceP4A2QdIinE0GQ2IbanYEMAqIigkmaRAY+BBjghqK5vZk5Rkcgx9U5Vv+DZwF94mdeHx2Ce7vtPlVO2llH7RjUDiK/zWbnintJBuPrrlYS0Vp7d60oukCcvLE3rl/iql9v/f5X/ytGgvG4p2mQgZqpmU7kjzVWibDsbme3q+UNSu8QRYPDovEWsBdX8/ENHh4BaeLZ993nE+ew7vPqTNFCzk/mpzBdBaEMLtWtED6r5Sj13cB8sGuOsqx7Lo3IqCku/ZKfz/gjCM+ZTFSYTHuaiCYTKMxJXSi3VTzpd1LurS/n36m/qmXjm6LXq993u9U9dHqgMcKF/GgQYCgLloOZUAaRF0MZPpwiGSNzhT5Yqyz+DFzGQxYNSot362ZB/58h3jO2SHdyykVhC+v9V855zyJskW/2ZCcIIOdFwkKVhxHDJLHBLksaQnDrgtU5rICPmQQQLNxBqmfgfvkO3g5J7z7vvpA/RlXfclQtviZiXZHQr06JMw3bLORd18jBE8/AFkVbJa2THcjqTPI/g7AIQhk6z2ti2ESDjxL1OypIslZPPkmq46QHD6KwTPBES9F8cQftZ0tadr5ODKR7xsTDkcYmteze9ZbW8idEdbgOHyShHE2tlsndLb9dlDq7r06dNNkkDRpBRGAjGWgbuEScgkOHbNDUTM1YA0EQLAVtG+1UNloz5P5UJNqmW5qHB619qU887z82jRsapWxIfVusYBX3joiRphPUEDm1Gy8UkQojBJrWqk08cGInSayssudhTQtBvZ609rnazNa5IKi2iRFxtV8PlgNjNnzsK1k07JoOba3yszD5Y2pDEwzmjnrZXgGf08EzbFXf2582qDr8dVXf6vDf88zOasNtGwxf4Wxypg5qa5RPSqpU+mOsoUV3tSKIbHC3k5JOw3c7U91psI2DNjfoMYQrAd+trRWiJ58fprzs3dJgww+8/3V0Tb3bHZdm0p0z2dIzkL46q06GfovwCeY7Q83MKc4Vqk34pcCCoFJSIdptL+g5q8VpqFdFjzLWpwWpSZj55ZT1SqC2R6YyFC9wFliCChZjDiWDTcf+SHF+OR/hX9QvZv9wIU+bD1KsUjuBTzcUHueBlRIiXaf1fK1xKhLSwSY/nosPKgk9W8jiWpqnHls70qgnmFt8Oj+DFZWY8LMR3j5HG4byDl29y3nPJWIvV+h0nisI1A3UWEnHFeecX/N+0Tq8FhEDkvmHQu7jXCICNuow3iw4kFELtGTHZwjn+tBblRRL4pV8w8vRacqRRHSgs5d24ywkUfAhm/qA6VyR+ncRaVyJ5VPsjGQOWestfPqOPPUp2ukTsLUIdxtkGyDeOeHIt1drlkFveQtVN9yTZU/QmKv1k1JVmAsozLXoRDYcXgnKzWdQ7zKee1YNjdHzk2LueJxKi9nxbtKZ3wdl3f4ML+kUbY50+jlFA/8UM68FIy3Fnn32Eha4l4D+iNL+3R0j/5a+YsUZGuH+shIOWfUhgyoX4PlMZ1v41Ced13S7SM94tj5/VH3m73/sP5Me6QkQ/OxWasR/0mcvuRG/FeJ/AKw5C/wAAHicnVJLTgJBFKwWNKLCyoXLiTFGE0Uh/iIrYmKiEPGD7offgAyMYQaJF/AgLl16BBeewEN4DKvftGHAcWM6r7u6+vX7VDeANF6QgEqmALzRQqywzF2IZ5DBh8EJHOPT4CTWVMbgWVRV3uA58s8Gz2NFvRqciuBFrKt3g5ciOK1W1ZfBGRwkFnACDw94wgAdOGgjgIU8djlyRFUyTa43PHXRhY86PZscfbKXxB7uuavLvSKGXNvkBvS0sCHxAsb32dcOh8M42mOIGrK85aEnrMfhMEMTLaI+fXzyfkzWzdharjk7jOrCJpdj7LCDAm5xgRKtEHtve+pmfJ+TPnfcDaQ2T06j2Szpt0U/rYTHTjvSjdbiUTyPaHu0Q+beR4NW+6On6drOOfsmZ4mrztSlx7TG3chZVt7CIVuVWvSrnBqF9at6rDXASOLrdw49XK51qcHn3CAzJG5I35a8X/gnzlDmWmHuUKdx5PJEhC0ycappsyKVTeb9qcamcrboY1MrV05G5h/pU523iCvBATWwfikSqtoho/9V1vyrsTYVRij/79Y3X66eggB4nGNgZgCD/3MYjBiwAAAqgwHRAAABAAH//wAPeJxjYGRgYOBhgAAmBlYGRjBmB4oxMvAx2AFJBwZ/IBnKUMLACAASwgHXAAAAAQAAAAoAHAAeAAFERkxUAAgABAAAAAD//wAAAAAAAAABAAAACgAcAB4AAURGTFQACAAEAAAAAP//AAAAAAAA"),
}


def make_handler(app: WebApp):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # ---- plumbing --------------------------------------------------------------------------
        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            if not isinstance(body, bytes):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj))

        def _err(self, msg, code=400):
            self._json({"error": msg}, code)

        def _host_ok(self):
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        def _auth(self, q):
            if not self._host_ok():
                return False
            tok = self.headers.get("X-Token") or (q.get("t") or [""])[0]
            return hmac.compare_digest(tok, app.token)

        def _body_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1 << 20:
                raise ValueError("Request too large.")
            d = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(d, dict):
                raise ValueError("Bad request.")
            return d

        # ---- routes ----------------------------------------------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path.startswith("/font/") and self._host_ok():
                f = FONTS.get(u.path[6:].removesuffix(".woff"))
                if f:
                    return self._send(200, f, "font/woff", {"Cache-Control": "max-age=86400"})
            if not self._auth(q):
                return self._send(403, "Forbidden", "text/plain")
            app.last_seen = time.time()
            try:
                r = api_get_extra(self, app, u.path, q)
                if r is not None:
                    return
                if u.path == "/":
                    return self._send(200, PAGE_HTML, "text/html; charset=utf-8", {
                        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; font-src 'self'; "
                                                   "script-src 'unsafe-inline'; connect-src 'self'; "
                                                   "img-src 'self' data: https://cdn.modrinth.com https://mc-heads.net",
                        "X-Frame-Options": "DENY"})
                if u.path == "/api/ping":
                    return self._json({"ok": True})
                if u.path == "/api/info":
                    return self._json({"home": str(Path.home()), "ram_mb": system_ram_mb(), "win": IS_WIN,
                                       "mode": app.mode, "version": APP_VERSION, "native": app.window is not None,
                                       "frameless": bool(app.window is not None and app.window.frameless),
                                       "native_capable": IS_WIN,
                                       "installed": installed_version(), "install_dir": str(install_dir()),
                                       "settings": load_settings()})
                if u.path == "/api/settings":
                    return self._json(load_settings())
                if u.path == "/api/focus":
                    if app.window:
                        app.window.focus()
                    elif not open_app_window(app.url, lambda: None):
                        webbrowser.open(app.url)
                    return self._json({"ok": True})
                if u.path == "/api/fs":
                    exts = tuple(x for x in (q.get("exts") or [""])[0].split(",") if x)
                    return self._json(list_dir((q.get("path") or [""])[0], (q.get("files") or ["0"])[0] == "1", exts))
                if u.path == "/api/latestlog":
                    raw = (q.get("dir") or [""])[0].strip()
                    try:
                        p = user_path(raw) / "logs" / "latest.log" if raw else None
                    except ValueError:
                        p = None
                    if p is None:
                        return self._json({"path": ""})
                    return self._json({"path": str(p) if p.is_file() else ""})
                if u.path == "/api/summary":
                    d = user_path((q.get("dir") or [""])[0])
                    if not d.is_dir():
                        return self._err("Folder not found.", 404)
                    return self._json(summarize_server(d))
                if u.path == "/api/events":
                    try:
                        since = max(0, int((q.get("since") or ["0"])[0]))
                    except ValueError:
                        since = 0
                    job = app.job
                    if not job:
                        return self._json({"lines": [], "next": 0, "state": "idle"})
                    with job.lock:
                        out = {"lines": job.lines[since:], "next": len(job.lines), "state": job.state,
                               "server_id": getattr(job, "server_id", None),
                               "stage": job.stage, "dl": job.dl, "result": job.result, "kind": job.kind}
                    return self._json(out)
                return self._err("Not found.", 404)
            except ValueError as e:
                return self._err(str(e))
            except Exception as e:  # noqa: BLE001
                return self._err(str(e), 500)

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._auth(q):
                return self._send(403, "Forbidden", "text/plain")
            app.last_seen = time.time()
            try:
                if u.path == "/api/upload":
                    name = _safe_name(Path((q.get("name") or ["upload"])[0]).stem, "upload") + \
                        re.sub(r"[^\w.]", "", Path((q.get("name") or [""])[0]).suffix)[:12]
                    kind = (q.get("kind") or ["file"])[0][:10]
                    old = app.uploads.pop(kind, None)
                    if old:
                        shutil.rmtree(old, ignore_errors=True)
                    d = Path(tempfile.mkdtemp(prefix="msb-upload-"))
                    app.uploads[kind] = d
                    dest = d / name
                    left = int(self.headers.get("Content-Length") or 0)
                    with open(dest, "wb") as f:
                        while left > 0:
                            chunk = self.rfile.read(min(left, 1 << 20))
                            if not chunk:
                                break
                            f.write(chunk)
                            left -= len(chunk)
                    return self._json({"path": str(dest)})
                body = self._body_json()
                r = api_post_extra(self, app, u.path, body)
                if r is not None:
                    return
                if u.path == "/api/inspect":
                    path = user_path(body.get("path"), "file")
                    if not path.is_file():
                        return self._err("File not found.")
                    try:
                        with zipfile.ZipFile(path) as zf:
                            p = parse_modpack(zf)
                    except (zipfile.BadZipFile, ValueError, KeyError) as e:
                        if path.parent.name.startswith("msb-upload-"):
                            shutil.rmtree(path.parent, ignore_errors=True)
                        if isinstance(e, zipfile.BadZipFile):
                            return self._err("That file isn't a modpack archive (.zip or .mrpack).")
                        return self._err(str(e) if isinstance(e, ValueError) else "That modpack's manifest is incomplete.")
                    if path.parent.name.startswith("msb-upload-"):
                        sug = Path.home() / "MinecraftServers" / (_safe_name(p.name) + "-server")
                    else:
                        sug = path.with_suffix("").with_name(path.stem + "-server")
                    return self._json({"name": p.name, "mc": p.mc, "loader": p.loader, "loader_ver": p.loader_ver,
                                       "kind": p.kind, "mods": len(p.files), "size": path.stat().st_size,
                                       "suggested_out": str(sug)})
                if u.path == "/api/build":
                    return self._start_build(body)
                if u.path == "/api/fix":
                    return self._start_fix(body)
                if u.path == "/api/restore":
                    restore_mod(user_path(body.get("dir")), str(body.get("bucket") or ""), str(body.get("jar") or ""))
                    return self._json({"ok": True})
                if u.path == "/api/open":
                    d = user_path(body.get("dir"))
                    if not d.is_dir():
                        return self._err("Folder not found.", 404)
                    open_in_file_manager(d)
                    return self._json({"ok": True})
                if u.path == "/api/quit":
                    self._json({"ok": True})

                    def stop_then_quit():
                        if app.busy():
                            CANCEL.set()
                            for _ in range(60):
                                if not app.busy():
                                    break
                                time.sleep(0.5)
                        app.request_quit()
                    threading.Thread(target=stop_then_quit, daemon=True).start()
                    return
                if u.path == "/api/cancel":
                    CANCEL.set()
                    return self._json({"ok": True})
                if u.path == "/api/skiptest":
                    SKIP_TEST.set()
                    return self._json({"ok": True})
                if u.path == "/api/settings":
                    cur = save_settings(body if isinstance(body, dict) else {})
                    if "theme" in body and app.window:
                        app.window.set_theme(theme_is_dark(body.get("theme")))
                    return self._json(cur)
                if u.path == "/api/install":
                    exe = install_app(bool(body.get("desktop", True)))
                    return self._json({"ok": True, "exe": str(exe)})
                if u.path == "/api/launch_installed":
                    exe = installed_exe()
                    if not exe.is_file():
                        return self._err("The app isn't installed yet.")
                    launch_detached(exe)
                    self._json({"ok": True})
                    app.request_quit()
                    return
                return self._err("Not found.", 404)
            except (ValueError, FileNotFoundError, RuntimeError) as e:
                return self._err(str(e))
            except Exception as e:  # noqa: BLE001
                return self._err(f"{type(e).__name__}: {e}", 500)

        def _start_build(self, b):
            if not b.get("pack") or not str(b.get("out") or "").strip():
                return self._err("Choose a modpack and a server folder first.")
            pack, out = user_path(b.get("pack"), "file"), user_path(b.get("out"))
            ram = str(b.get("ram") or "6G")
            excl = [e.strip() for e in str(b.get("exclude") or "").split(",") if e.strip()]

            def fn(job):
                res = build_server(pack, out, ram, bool(b.get("eula")), (b.get("key") or None), excl, False,
                                   bool(b.get("test", True)), bool(b.get("aggressive")), 8, True, job.log)
                job.finish(dir=str(res.server_dir), ok=res.test_ok, failures=res.failures)
            app.start_job("build", fn)
            self._json({"ok": True})

        def _start_fix(self, b):
            if not b.get("log") or not str(b.get("dir") or "").strip():
                return self._err("Choose the server folder and its log file first.")
            log_path, sdir = user_path(b.get("log"), "file"), user_path(b.get("dir"))
            retest, report_only = bool(b.get("retest")), bool(b.get("report_only"))

            def fn(job):
                info = {}
                ok = fix_from_log(log_path, sdir, bool(b.get("eula")), bool(b.get("aggressive")),
                                  report_only, retest, True, job.log, info)
                job.finish(dir=str(sdir), ok=ok, retest=retest, report_only=report_only, eula=bool(b.get("eula")),
                           **info)
            app.start_job("fix", fn)
            self._json({"ok": True})

    return H


# ============================================================================
# Desktop shell: native window (Windows WebView2), single instance, install/uninstall
# ============================================================================

APP_ID = "ModpackServerBuilder.App"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ModpackServerBuilder"
THEME_BG = {"dark": (0x0D, 0x13, 0x22), "light": (0xEC, 0xEF, 0xF8)}


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def resource_path(name) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for p in (base / name, base / "assets" / name, base / "vendor" / name):   # source checkout layout
        if p.exists():
            return p
    return base / name


def _no_proxy_open(url, timeout=2.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url, timeout=timeout)


# ---- single instance ---------------------------------------------------------------------

def instance_file() -> Path:
    return app_data_dir() / "instance.json"


def focus_existing_instance() -> bool:
    """If the app is already open, bring its window forward and return True."""
    try:
        d = json.loads(instance_file().read_text())
        if IS_WIN:
            ctypes.windll.user32.AllowSetForegroundWindow(-1)   # let the running copy take focus
        with _no_proxy_open(f"http://127.0.0.1:{int(d['port'])}/api/focus?t={d['token']}") as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def quit_existing_instance() -> bool:
    try:
        d = json.loads(instance_file().read_text())
        req = urllib.request.Request(f"http://127.0.0.1:{int(d['port'])}/api/quit", data=b"{}", method="POST",
                                     headers={"X-Token": d["token"], "Content-Type": "application/json"})
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=2)
        time.sleep(1.5)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- Windows plumbing ----------------------------------------------------------------------

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32")
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.CallWindowProcW.restype = LRESULT
    _user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.SendMessageW.restype = LRESULT
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.UINT]
    _user32.IsZoomed.argtypes = [wintypes.HWND]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    _shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON),
                                        ctypes.POINTER(wintypes.HICON), wintypes.UINT]

    def message_box(text, flags=0x40, hwnd=None):
        try:
            return _user32.MessageBoxW(hwnd, text, APP_NAME, flags | 0x10000)   # MB_SETFOREGROUND
        except Exception:  # noqa: BLE001
            return 0

    def set_app_id():
        try:
            _shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001
            pass
else:
    def message_box(text, flags=0x40, hwnd=None):
        print(text)
        return 6

    def set_app_id():
        pass


class NativeWindow:
    """A WebView2 window hosted through webview.dll (https://github.com/webview/webview)."""
    WM_CLOSE, WM_SETICON = 0x0010, 0x0080

    def __init__(self, app, lib, handle):
        self.app, self.lib, self.handle = app, lib, handle
        self.hwnd = lib.webview_get_window(handle)
        self.force_close = False
        self.frameless = False
        self._old = None
        self._proc = WNDPROC(self._wndproc)      # keep a reference: the window calls it
        self._bind_cb = None

    WM_NCCALCSIZE, WM_NCACTIVATE, WM_NCLBUTTONDOWN = 0x0083, 0x0086, 0x00A1
    HT = {"l": 10, "r": 11, "t": 12, "tl": 13, "tr": 14, "b": 15, "bl": 16, "br": 17}

    def _close_question(self):
        runner = getattr(self.app, "runner", None)
        if runner is not None and getattr(runner, "state", "offline") in ("starting", "online", "stopping"):
            return "Your server is still running.\n\nStop it safely (saving the world) and close the app?"
        if self.app.busy():
            return "A task is still running.\n\nStop it and close Modpack Server Builder?"
        return None

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == self.WM_CLOSE and not self.force_close:
            q = self._close_question()
            if q:
                r = message_box(q, 0x4 | 0x30 | 0x100, hwnd)      # Yes/No, warning, default No
                if r != 6:
                    return 0
                CANCEL.set()
                runner = getattr(self.app, "runner", None)
                if runner is not None:
                    threading.Thread(target=self._stop_then_close, args=(runner,), daemon=True).start()
                    return 0
        if self.frameless:
            if msg == self.WM_NCCALCSIZE and wp:
                # The whole window is our page; when maximized, keep it inside the screen edges.
                if _user32.IsZoomed(hwnd):
                    try:
                        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd) or 96
                        f = (ctypes.windll.user32.GetSystemMetricsForDpi(32, dpi) +
                             ctypes.windll.user32.GetSystemMetricsForDpi(92, dpi))
                    except Exception:  # noqa: BLE001
                        f = _user32.GetSystemMetrics(32) + _user32.GetSystemMetrics(92)
                    rc = ctypes.cast(lp, ctypes.POINTER(wintypes.RECT)).contents
                    rc.left += f
                    rc.top += f
                    rc.right -= f
                    rc.bottom -= f
                return 0
            if msg == self.WM_NCACTIVATE:
                return _user32.CallWindowProcW(self._old, hwnd, msg, wp, -1)   # don't repaint a caption
        return _user32.CallWindowProcW(self._old, hwnd, msg, wp, lp)

    def _stop_then_close(self, runner):
        try:
            runner.shutdown()
        except Exception:  # noqa: BLE001
            pass
        for _ in range(120):
            if not self.app.busy():
                break
            time.sleep(0.25)
        self.close()

    # ---- custom title bar ---------------------------------------------------------------------
    def make_frameless(self):
        """Hide the Windows caption so the app's own glass title bar is the only one."""
        try:
            self.frameless = True
            _user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, 0x0020 | 0x0001 | 0x0002 | 0x0004)  # FRAMECHANGED
            dwm = ctypes.windll.dwmapi
            pref = ctypes.c_int(2)                                   # Windows 11: rounded corners
            dwm.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(pref), 4)

            class MARGINS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]
            dwm.DwmExtendFrameIntoClientArea(self.hwnd, ctypes.byref(MARGINS(0, 0, 0, 1)))   # keeps the shadow
        except Exception:  # noqa: BLE001
            self.frameless = False
        return self.frameless

    def bind_window_controls(self):
        CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p)

        def cb(seq, req, _arg):
            result = "null"
            try:
                args = json.loads((req or b"[]").decode("utf-8"))
                cmd = str(args[0]) if args else ""
                result = json.dumps(self.window_command(cmd))
            except Exception:  # noqa: BLE001
                pass
            try:
                self.lib.webview_return(self.handle, seq, 0, result.encode("utf-8"))
            except Exception:  # noqa: BLE001
                pass
        self._bind_cb = CB(cb)                                   # keep it alive for the window's lifetime
        self.lib.webview_bind(self.handle, b"msbWindow", ctypes.cast(self._bind_cb, ctypes.c_void_p), None)

    def _cursor_lparam(self):
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        return (pt.x & 0xFFFF) | ((pt.y & 0xFFFF) << 16)

    def window_command(self, cmd):
        h = self.hwnd
        if cmd == "drag" or cmd.startswith("resize:"):
            ht = 2 if cmd == "drag" else self.HT.get(cmd.split(":", 1)[1])
            if ht and not (ht != 2 and _user32.IsZoomed(h)):
                _user32.ReleaseCapture()
                _user32.PostMessageW(h, self.WM_NCLBUTTONDOWN, ht, self._cursor_lparam())
        elif cmd == "min":
            _user32.ShowWindow(h, 6)
        elif cmd == "max":
            _user32.ShowWindow(h, 9 if _user32.IsZoomed(h) else 3)
        elif cmd == "close":
            _user32.PostMessageW(h, self.WM_CLOSE, 0, 0)
        return {"max": bool(_user32.IsZoomed(h))}

    def setup(self, dark):
        try:
            self._old = _user32.SetWindowLongPtrW(self.hwnd, -4, ctypes.cast(self._proc, ctypes.c_void_p))
        except Exception:  # noqa: BLE001
            self._old = None
        try:   # our icon for the title bar and taskbar
            big, small = wintypes.HICON(), wintypes.HICON()
            src = sys.executable if is_frozen() else str(resource_path("app.ico"))
            if _shell32.ExtractIconExW(src, 0, ctypes.byref(big), ctypes.byref(small), 1):
                _user32.SendMessageW(self.hwnd, self.WM_SETICON, 1, big.value or 0)
                _user32.SendMessageW(self.hwnd, self.WM_SETICON, 0, small.value or 0)
        except Exception:  # noqa: BLE001
            pass
        self.set_theme(dark)
        try:   # size to fit the screen and centre it
            wa = wintypes.RECT()
            _user32.SystemParametersInfoW(0x30, 0, ctypes.byref(wa), 0)
            scale = 1.0
            try:
                scale = (ctypes.windll.user32.GetDpiForWindow(self.hwnd) or 96) / 96
            except Exception:  # noqa: BLE001
                pass
            aw, ah = (wa.right - wa.left) / scale, (wa.bottom - wa.top) / scale
            w, h = int(min(1120, aw - 60)), int(min(900, ah - 40))
            self.lib.webview_set_size(self.handle, max(w, 640), max(h, 520), 0)
            self.lib.webview_set_size(self.handle, 720, 560, 1)       # minimum size
            r = wintypes.RECT()
            _user32.GetWindowRect(self.hwnd, ctypes.byref(r))
            x = wa.left + ((wa.right - wa.left) - (r.right - r.left)) // 2
            y = wa.top + ((wa.bottom - wa.top) - (r.bottom - r.top)) // 2
            _user32.SetWindowPos(self.hwnd, None, max(wa.left, x), max(wa.top, y), 0, 0, 0x0001 | 0x0004)
        except Exception:  # noqa: BLE001
            pass

    def set_theme(self, dark):
        try:
            dwm = ctypes.windll.dwmapi
            on = ctypes.c_int(1 if dark else 0)
            dwm.DwmSetWindowAttribute(self.hwnd, 20, ctypes.byref(on), 4)          # dark title bar
            r, g, b = THEME_BG["dark" if dark else "light"]
            col = ctypes.c_uint(r | (g << 8) | (b << 16))
            dwm.DwmSetWindowAttribute(self.hwnd, 35, ctypes.byref(col), 4)         # Windows 11 caption colour
        except Exception:  # noqa: BLE001
            pass

    def focus(self):
        try:
            if _user32.IsIconic(self.hwnd):
                _user32.ShowWindow(self.hwnd, 9)
            _user32.SetForegroundWindow(self.hwnd)
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        self.force_close = True
        try:
            _user32.PostMessageW(self.hwnd, self.WM_CLOSE, 0, 0)
        except Exception:  # noqa: BLE001
            pass


def load_webview():
    """-> ctypes library for webview.dll, or None when the native window isn't available."""
    if not IS_WIN:
        return None
    dll = resource_path("webview.dll")
    if not dll.is_file():
        return None
    try:
        lib = ctypes.CDLL(str(dll))
        c = ctypes
        lib.webview_create.argtypes, lib.webview_create.restype = [c.c_int, c.c_void_p], c.c_void_p
        lib.webview_get_window.argtypes, lib.webview_get_window.restype = [c.c_void_p], c.c_void_p
        for name, args in (("webview_set_title", [c.c_void_p, c.c_char_p]),
                           ("webview_set_size", [c.c_void_p, c.c_int, c.c_int, c.c_int]),
                           ("webview_navigate", [c.c_void_p, c.c_char_p]),
                           ("webview_run", [c.c_void_p]), ("webview_destroy", [c.c_void_p]),
                           ("webview_terminate", [c.c_void_p]),
                           ("webview_bind", [c.c_void_p, c.c_char_p, c.c_void_p, c.c_void_p]),
                           ("webview_return", [c.c_void_p, c.c_char_p, c.c_int, c.c_char_p])):
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, c.c_int
        return lib
    except Exception:  # noqa: BLE001
        return None


def run_native_window(app, url, dark) -> bool:
    """Show the UI in a native window until the user closes it. False if WebView2 isn't usable here."""
    lib = load_webview()
    if not lib:
        return False
    r, g, b = THEME_BG["dark" if dark else "light"]
    os.environ["WEBVIEW2_DEFAULT_BACKGROUND_COLOR"] = f"0xFF{r:02X}{g:02X}{b:02X}"   # no white flash
    # webview initialises COM (single-threaded) itself; it must own this thread's message loop.
    created = threading.Event()

    def rescue():
        # WebView2 can stall during start-up on a broken install; show the app another way then.
        if not created.wait(25):
            app.native_failed = True
            if not open_app_window(app.url, lambda: setattr(app, "window_closed", True)):
                webbrowser.open(app.url)
    threading.Thread(target=rescue, daemon=True).start()
    try:
        handle = lib.webview_create(0, None)
    except Exception:  # noqa: BLE001
        handle = None
    created.set()
    if handle and app.native_failed:          # it came back after we already showed another window
        lib.webview_destroy(handle)
        return False
    if not handle:
        return False
    win = NativeWindow(app, lib, handle)
    win.setup(dark)
    if load_settings().get("titlebar", "custom") != "standard":
        try:
            win.bind_window_controls()
            win.make_frameless()
        except Exception:  # noqa: BLE001
            win.frameless = False
    lib.webview_set_title(handle, APP_NAME.encode("utf-8"))
    lib.webview_navigate(handle, url.encode("utf-8"))
    app.window = win
    try:
        lib.webview_run(handle)
    finally:
        app.window = None
        try:
            lib.webview_destroy(handle)
        except Exception:  # noqa: BLE001
            pass
    return True


# ---- install / uninstall (per user, no admin) ------------------------------------------------

def install_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Programs" / APP_NAME


def installed_exe() -> Path:
    return install_dir() / f"{APP_NAME}.exe"


def is_install_mode(flag=False) -> bool:
    return flag or (is_frozen() and IS_WIN and "setup" in Path(sys.executable).stem.lower())


def installed_version():
    if not IS_WIN:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            return winreg.QueryValueEx(k, "DisplayVersion")[0]
    except OSError:
        return None


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def _guid(s):
    import uuid
    return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(s).bytes_le)


def known_folder(folder_id: str):
    """Path of a Windows known folder (handles OneDrive-redirected desktops)."""
    try:
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(_guid(folder_id)), 0, None, ctypes.byref(p)) != 0:
            return None
        val = p.value
        ctypes.windll.ole32.CoTaskMemFree(p)
        return val
    except Exception:  # noqa: BLE001
        return None


FOLDER_PROGRAMS = "A77F5D77-2E2B-44C3-A6A2-ABA601054A51"
FOLDER_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"


def _com_method(ptr, index, *argtypes):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])
    return lambda *a: fn(ptr, *a)


def _shell_link(lnk: str, exe: Path, icon=None):
    """Create a .lnk with IShellLinkW (and stamp our AppUserModelID so the taskbar groups correctly)."""
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    psl = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(_guid("00021401-0000-0000-C000-000000000046")), None, 1,
                           ctypes.byref(_guid("000214F9-0000-0000-C000-000000000046")), ctypes.byref(psl))
    if not psl.value:
        raise OSError("ShellLink unavailable")
    try:
        W = ctypes.c_wchar_p
        _com_method(psl, 20, W)(str(exe))                                  # SetPath
        _com_method(psl, 9, W)(str(exe.parent))                            # SetWorkingDirectory
        _com_method(psl, 7, W)("Build Minecraft servers from modpacks")    # SetDescription
        _com_method(psl, 17, W, ctypes.c_int)(str(icon or exe), 0)         # SetIconLocation
        try:   # IPropertyStore -> System.AppUserModel.ID
            pps = ctypes.c_void_p()
            _com_method(psl, 0, ctypes.c_void_p, ctypes.c_void_p)(
                ctypes.byref(_guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")), ctypes.byref(pps))

            class PROPERTYKEY(ctypes.Structure):
                _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", ctypes.c_ulong)]

            class PROPVARIANT(ctypes.Structure):
                _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                            ("r3", ctypes.c_ushort), ("val", ctypes.c_wchar_p), ("pad", ctypes.c_void_p)]
            key = PROPERTYKEY(_guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5)
            pv = PROPVARIANT(31, 0, 0, 0, APP_ID, None)                     # VT_LPWSTR
            _com_method(pps, 6, ctypes.c_void_p, ctypes.c_void_p)(ctypes.byref(key), ctypes.byref(pv))
            _com_method(pps, 7)()                                          # Commit
            _com_method(pps, 2)()                                          # Release
        except OSError:
            pass
        ppf = ctypes.c_void_p()
        _com_method(psl, 0, ctypes.c_void_p, ctypes.c_void_p)(
            ctypes.byref(_guid("0000010B-0000-0000-C000-000000000046")), ctypes.byref(ppf))
        _com_method(ppf, 6, W, ctypes.c_int)(lnk, 1)                       # IPersistFile::Save
        _com_method(ppf, 2)()
    finally:
        _com_method(psl, 2)()


def make_shortcuts(exe: Path, desktop=True, icon=None):
    """Start menu (and desktop) shortcuts. -> list of created .lnk paths"""
    targets = [known_folder(FOLDER_PROGRAMS)] + ([known_folder(FOLDER_DESKTOP)] if desktop else [])
    made = []
    try:
        for folder in targets:
            if folder:
                lnk = os.path.join(folder, APP_NAME + ".lnk")
                _shell_link(lnk, exe, icon)
                if os.path.isfile(lnk):
                    made.append(lnk)
        if made and len(made) == len([t for t in targets if t]):
            return made
    except Exception:  # noqa: BLE001
        pass
    return made or _shortcuts_via_script(exe, desktop)


def _shortcuts_via_script(exe: Path, desktop=True):
    """Fallback: Windows Script Host through PowerShell, then VBScript."""
    folders = "@([Environment]::GetFolderPath('Programs'))" + (", [Environment]::GetFolderPath('Desktop')" if desktop else "")
    ps = (f"$ws=New-Object -ComObject WScript.Shell; foreach($f in {folders}){{"
          f"$p=[IO.Path]::Combine($f,{_ps_quote(APP_NAME + '.lnk')}); $s=$ws.CreateShortcut($p);"
          f"$s.TargetPath={_ps_quote(exe)}; $s.WorkingDirectory={_ps_quote(exe.parent)};"
          f"$s.IconLocation={_ps_quote(str(exe) + ',0')}; $s.Description='Build Minecraft servers from modpacks';"
          f"$s.Save(); Write-Output $p }}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps],
                           capture_output=True, text=True, timeout=60, **NOWIN)
        paths = [l.strip() for l in r.stdout.splitlines() if l.strip().lower().endswith(".lnk")]
        if paths:
            return paths
    except Exception:  # noqa: BLE001
        pass
    # Fallback: VBScript through cscript (present on every Windows)
    vbs = Path(tempfile.mkdtemp(prefix="msb-")) / "lnk.vbs"
    lines = ['Set ws = CreateObject("WScript.Shell")', "Dim fs: fs = Array(ws.SpecialFolders(\"Programs\")"
             + (', ws.SpecialFolders("Desktop")' if desktop else "") + ")", "For Each f In fs",
             f'  p = f & "\\{APP_NAME}.lnk"', "  Set s = ws.CreateShortcut(p)", f'  s.TargetPath = "{exe}"',
             f'  s.WorkingDirectory = "{exe.parent}"', f'  s.IconLocation = "{exe},0"', "  s.Save",
             "  WScript.Echo p", "Next"]
    vbs.write_text("\r\n".join(lines), encoding="utf-16")
    try:
        r = subprocess.run(["cscript", "//nologo", str(vbs)], capture_output=True, text=True, timeout=60, **NOWIN)
        return [l.strip() for l in r.stdout.splitlines() if l.strip().lower().endswith(".lnk")]
    except Exception:  # noqa: BLE001
        return []
    finally:
        shutil.rmtree(vbs.parent, ignore_errors=True)


# ---- keeping the icon fresh after an update ----------------------------------------------------
# Windows caches icons by file path. An update replaces the exe at the same path, so shortcuts, pinned taskbar
# buttons and Installed apps kept showing the old icon. Each version therefore writes its icon to a new file
# (icons/app-<version>.ico), points every shortcut at it, and tells Explorer to redraw.

def write_versioned_icon(folder: Path):
    src = resource_path("app.ico")
    if not src.is_file():
        return None
    d = Path(folder) / "icons"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"app-{APP_VERSION}.ico"
    if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
        shutil.copyfile(src, dst)
    for old in d.glob("app-*.ico"):
        if old != dst:
            try:
                old.unlink()
            except OSError:
                pass
    return dst


def pinned_taskbar_dir():
    a = os.environ.get("APPDATA")
    return Path(a) / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "TaskBar" if a else None


def refresh_shell_icons(exe: Path):
    """Re-point shortcuts (Start menu, desktop if present, pinned taskbar) at this version's icon. -> icon path"""
    icon = None
    try:
        icon = write_versioned_icon(exe.parent)
    except Exception as e:  # noqa: BLE001
        _ulog(f"icons: couldn't write the icon file: {e!r}")
    _ulog(f"icons: refreshing shortcuts for {exe} (icon {icon})")
    links = []
    for folder, always in ((known_folder(FOLDER_PROGRAMS), True), (known_folder(FOLDER_DESKTOP), False),
                           (pinned_taskbar_dir(), False)):
        if not folder:
            continue
        lnk = os.path.join(str(folder), APP_NAME + ".lnk")
        if always or os.path.isfile(lnk):
            try:
                _shell_link(lnk, exe, icon)
                links.append(lnk)
            except Exception:  # noqa: BLE001
                pass
    try:
        register_uninstaller(exe, icon)
    except Exception:  # noqa: BLE001
        pass
    try:
        info = exe.parent / "install.json"
        d = json.loads(info.read_text(encoding="utf-8")) if info.is_file() else {}
        d["icons"] = APP_VERSION
        write_file(info, json.dumps(d, indent=1))
    except Exception:  # noqa: BLE001
        pass
    try:   # SHCNE_ASSOCCHANGED + SHCNF_FLUSH: Explorer drops its cached icons and redraws
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
    except Exception:  # noqa: BLE001
        pass
    return icon


def refresh_icons_if_needed():
    """Installed copy started after an update: make sure Windows shows this version's icon."""
    if not (IS_WIN and is_frozen()):
        return
    exe = Path(sys.executable)
    try:
        if not _same_path(exe, installed_exe()):
            _ulog(f"icons: not the installed copy ({exe} vs {installed_exe()})")
            return
        info = exe.parent / "install.json"
        d = json.loads(info.read_text(encoding="utf-8")) if info.is_file() else {}
        if d.get("icons") == APP_VERSION:
            return
    except Exception:  # noqa: BLE001
        return
    threading.Thread(target=refresh_shell_icons, args=(exe,), daemon=True).start()


def register_uninstaller(exe: Path, icon=None):
    import winreg
    size_kb = max(1, exe.stat().st_size // 1024)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, val in (("DisplayName", APP_NAME), ("DisplayVersion", APP_VERSION), ("Publisher", APP_NAME),
                          ("DisplayIcon", f"{icon or exe},0"), ("InstallLocation", str(exe.parent)),
                          ("UninstallString", f'"{exe}" --uninstall'),
                          ("QuietUninstallString", f'"{exe}" --uninstall --quiet'),
                          ("InstallDate", datetime.now().strftime("%Y%m%d"))):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        for name, val in (("NoModify", 1), ("NoRepair", 1), ("EstimatedSize", size_kb)):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, val)


def install_app(desktop=True) -> Path:
    """Copy this program to the user's Programs folder, add shortcuts and an entry in Installed apps."""
    if not (IS_WIN and is_frozen()):
        raise RuntimeError("Installing is only available in the Windows app (.exe).")
    dst = installed_exe()
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = Path(sys.executable)
    if src.resolve() != dst.resolve():
        quit_existing_instance()                     # an older copy may still be open
        tmp = dst.with_name(dst.name + ".new")
        shutil.copy2(src, tmp)
        for _ in range(10):
            try:
                os.replace(tmp, dst)
                break
            except PermissionError:
                time.sleep(0.5)
        else:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("Modpack Server Builder is still open. Close it and try again.")
    icon = None
    try:
        icon = write_versioned_icon(dst.parent)
    except OSError:
        pass
    links = make_shortcuts(dst, desktop, icon)
    register_uninstaller(dst, icon)
    write_file(dst.parent / "install.json", json.dumps({"version": APP_VERSION, "shortcuts": links,
                                                        "icons": APP_VERSION}, indent=1))
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
    except Exception:  # noqa: BLE001
        pass
    save_settings({"desktop_shortcut": bool(desktop)})
    return dst


def uninstall_app(quiet=False):
    if not IS_WIN:
        raise RuntimeError("Uninstall is only available on Windows.")
    if not quiet and message_box("Remove Modpack Server Builder from this PC?\n\n"
                                 "Servers you built are not touched.", 0x4 | 0x20) != 6:
        return False
    quit_existing_instance()
    d = install_dir()
    links = []
    try:
        links = json.loads((d / "install.json").read_text()).get("shortcuts", [])
    except Exception:  # noqa: BLE001
        pass
    for base in (known_folder(FOLDER_PROGRAMS), known_folder(FOLDER_DESKTOP),
                 os.environ.get("APPDATA", "") + r"\Microsoft\Windows\Start Menu\Programs",
                 os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")):
        if base:
            links.append(os.path.join(base, APP_NAME + ".lnk"))
    for p in links:
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    shutil.rmtree(app_data_dir(), ignore_errors=True)
    for n in (f"{APP_NAME}.exe", f"{APP_NAME} Setup.exe"):          # WebView2 keeps its data per exe name
        shutil.rmtree(os.path.join(os.environ.get("APPDATA", ""), n), ignore_errors=True)
    # This program is running from the folder being removed: let cmd finish the job after we exit.
    subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 4 >nul & rmdir /s /q "{d}"', shell=False,
                     creationflags=0x08000000 | 0x00000008, close_fds=True)   # no window, detached
    if not quiet:
        message_box("Modpack Server Builder has been removed.", 0x40)
    return True


def launch_detached(exe: Path):
    flags = (0x00000008 | 0x00000200) if IS_WIN else 0   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True, creationflags=flags)


def find_app_browser():
    """A Chromium-based browser that can open the UI as a standalone app window."""
    cands = []
    if IS_WIN:
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands += [Path(base, "Microsoft/Edge/Application/msedge.exe"),
                          Path(base, "Google/Chrome/Application/chrome.exe"),
                          Path(base, "BraveSoftware/Brave-Browser/Application/brave.exe"),
                          Path(base, "Chromium/Application/chrome.exe")]
    elif sys.platform == "darwin":
        cands += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                  Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                  Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
                  Path("/Applications/Chromium.app/Contents/MacOS/Chromium")]
    else:
        for n in ("microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable",
                  "chromium", "chromium-browser", "brave-browser"):
            w = shutil.which(n)
            if w:
                cands.append(Path(w))
    for p in cands:
        if p.is_file():
            return str(p)
    return None


def open_app_window(url, on_close, browser=None, extra_args=()):
    """Open the UI in its own window (no tabs or address bar). Calls on_close when the window is closed."""
    exe = browser or find_app_browser()
    if not exe:
        return False
    profile = app_data_dir() / "window"
    profile.mkdir(parents=True, exist_ok=True)
    args = [exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1120,880",
            "--no-first-run", "--no-default-browser-check", "--disable-sync", "--disable-extensions",
            "--disable-features=Translate,msEdgeSidebarV2,msHubApps,msUndersideButton", *extra_args]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False

    def wait():
        started = time.time()
        proc.wait()
        # If the browser handed the window to an already-running copy it exits at once;
        # then we can't see the window close and fall back to the idle timer instead.
        if time.time() - started > 4:
            on_close()
    threading.Thread(target=wait, daemon=True).start()
    return True


def system_prefers_dark():
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        except OSError:
            pass
    return True


def run_web(port=0, open_browser=True, app_window=True, browser=None, browser_args=(), mode="app"):
    app = WebApp(mode)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    app.httpd, app.port = httpd, httpd.server_address[1]
    url = app.url = f"http://127.0.0.1:{app.port}/?t={app.token}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    own_instance = mode == "app"
    if own_instance:
        try:
            instance_file().parent.mkdir(parents=True, exist_ok=True)
            write_file(instance_file(), json.dumps({"port": app.port, "token": app.token, "pid": os.getpid()}))
        except OSError:
            own_instance = False

    def watchdog():
        while not app.stop_evt.is_set():
            time.sleep(1)
            idle = time.time() - app.last_seen > IDLE_LIMIT
            if app.window is None and not app.busy() and (app.window_closed or idle):
                app.stop_evt.set()
                if app.native_failed:           # main thread may be stuck inside WebView2 start-up
                    time.sleep(1)
                    os._exit(0)
    threading.Thread(target=watchdog, daemon=True).start()

    def closed():
        app.window_closed = True
        if app.busy():
            CANCEL.set()                 # closing the window stops the work
    print(f"{APP_NAME} is running at: {url}", flush=True)
    dark = theme_is_dark(load_settings().get("theme"))
    if mode == "app":
        refresh_icons_if_needed()
        try:
            app.updater = AutoUpdater(first_delay=1.0)   # check as soon as the app opens
            app.updater.start()
        except Exception:  # noqa: BLE001
            app.updater = None
    try:
        shown = False
        if open_browser and app_window and browser is None:
            shown = run_native_window(app, url, dark)          # blocks while the window is open
            if shown:
                if app.busy():
                    CANCEL.set()
                    for _ in range(60):
                        if not app.busy():
                            break
                        time.sleep(0.5)
                app.stop_evt.set()
        if not shown:
            opened = app.native_failed or bool(open_browser and app_window and
                                               open_app_window(url, closed, browser, browser_args))
            if open_browser and not opened:
                try:
                    webbrowser.open(url)
                except Exception:  # noqa: BLE001
                    pass
            if not opened:
                print("Close the browser tab, or press Ctrl+C here, to quit.", flush=True)
            while not app.stop_evt.wait(0.5):
                pass
    except KeyboardInterrupt:
        CANCEL.set()
    finally:
        if app.runner is not None:
            try:
                app.runner.shutdown()           # stop a running Minecraft server cleanly (saves the world)
            except Exception:  # noqa: BLE001
                pass
        httpd.shutdown()
        httpd.server_close()
        for d in app.uploads.values():
            shutil.rmtree(d, ignore_errors=True)
        if own_instance:
            try:
                if json.loads(instance_file().read_text()).get("token") == app.token:
                    instance_file().unlink()
            except Exception:  # noqa: BLE001
                pass
    return app


def theme_is_dark(theme):
    if theme in ("dark", "light"):
        return theme == "dark"
    h = time.localtime().tm_hour          # "auto": the app's sky follows the time of day
    return not (5 <= h < 17)


def run_gui(**kw):
    run_web(**kw)


PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#121a3d" id="theme-color">
<title>Modpack Server Builder</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='4 3 24 26'%3E%3Cpath d='M6.5 10.7L16 15.9v10.6l-9.5-5.2z' fill='%238a5a36'/%3E%3Cpath d='M25.5 10.7L16 15.9v10.6l9.5-5.2z' fill='%236b4428'/%3E%3Cpath d='M16 5.5l9.5 5.2L16 15.9 6.5 10.7z' fill='%235fe08c'/%3E%3C/svg%3E">
<script>(function(){var m='auto';try{m=localStorage.getItem('msb-theme-mode')||'auto'}catch(e){}var h=new Date().getHours(),sky=h>=5&&h<8?'dawn':h>=8&&h<17?'day':h>=17&&h<20?'dusk':'night';var t=m==='auto'?(sky==='day'||sky==='dawn'?'light':'dark'):m;if(t==='dark'&&(sky==='day'||sky==='dawn'))sky='night';if(t==='light'&&(sky==='night'||sky==='dusk'))sky='day';var r=document.documentElement;r.dataset.theme=t;r.dataset.sky=sky})()</script>
<style>
@font-face{font-family:"Brico";src:url(/font/brico.woff) format("woff");font-weight:200 800;font-display:swap}
@font-face{font-family:"Pix";src:url(/font/pix.woff) format("woff");font-display:swap}
:root{
  --display:"Brico","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;
  --ui:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Roboto,sans-serif;
  --mono:"Cascadia Mono","Cascadia Code",ui-monospace,Consolas,"SF Mono",Menlo,monospace;
  --pix:"Pix",var(--mono);
  --emerald:#35d48d;--lapis:#5b80ff;--gold:#f6c453;--redstone:#ff5e62;--amethyst:#a68cff;--diamond:#62e4ee;
  --r-xl:30px;--r-lg:22px;--r-md:15px;--r-sm:11px;
  --tb:46px;
}
:root[data-theme=dark]{color-scheme:dark;
  --ink:#f3f6fd;--ink-2:rgba(236,241,252,.76);--ink-3:rgba(226,233,248,.56);
  --glass-hi:rgba(255,255,255,.14);--glass-lo:rgba(255,255,255,.05);--glass-solid:rgba(20,26,46,.62);
  --field:rgba(255,255,255,.08);--field-hi:rgba(255,255,255,.14);--well:rgba(4,8,20,.42);
  --line:rgba(255,255,255,.12);--edge:rgba(255,255,255,.42);
  --t-ok:#5ff0ab;--t-warn:#ffd977;--t-bad:#ff8c8f;--t-acc:#9cb4ff;
  --shadow:0 34px 70px -34px rgba(0,0,0,.75),0 10px 26px -14px rgba(0,0,0,.5);
  --dim:rgba(4,7,18,.32);
}
:root[data-theme=light]{color-scheme:light;
  --ink:#121a2f;--ink-2:rgba(18,26,47,.78);--ink-3:rgba(18,26,47,.62);
  --glass-hi:rgba(255,255,255,.74);--glass-lo:rgba(255,255,255,.42);--glass-solid:rgba(250,252,255,.8);
  --field:rgba(255,255,255,.62);--field-hi:rgba(255,255,255,.92);--well:rgba(255,255,255,.6);
  --line:rgba(18,26,47,.12);--edge:rgba(255,255,255,.95);
  --t-ok:#0f7a4c;--t-warn:#8a5a00;--t-bad:#bf2232;--t-acc:#2f55d6;
  --shadow:0 30px 60px -32px rgba(30,50,110,.42),0 10px 24px -14px rgba(30,50,110,.25);
  --dim:rgba(255,255,255,0);
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
html{background:#0d1428}
body{color:var(--ink);font:400 14.5px/1.5 var(--ui);-webkit-font-smoothing:antialiased;overflow:hidden;background:transparent}
button,input,select,textarea{font:inherit;color:inherit}
[hidden]{display:none!important}
:focus-visible{outline:2px solid var(--t-acc);outline-offset:3px}
::selection{background:color-mix(in srgb,var(--lapis) 40%,transparent)}

/* ================= the world behind the glass ================= */
.scene{position:fixed;inset:0;z-index:-2;overflow:hidden;background:linear-gradient(180deg,var(--sky1) 0%,var(--sky2) 55%,var(--sky3) 100%)}
.scene svg{position:absolute;left:0;right:0;bottom:0;width:100%;height:46vh;min-height:260px}
.scene .sun{position:absolute;width:74px;height:74px;border-radius:6px;background:var(--sun);box-shadow:0 0 0 14px color-mix(in srgb,var(--sun) 22%,transparent),0 0 90px 30px color-mix(in srgb,var(--sun) 45%,transparent)}
.scene .sun::after{content:"";position:absolute;inset:14px;border-radius:3px;background:color-mix(in srgb,white 30%,var(--sun))}
[data-sky=night] .scene .sun{width:50px;height:50px;opacity:.85;box-shadow:0 0 0 8px color-mix(in srgb,var(--sun) 10%,transparent),0 0 60px 14px color-mix(in srgb,var(--sun) 18%,transparent)}[data-sky=night] .scene .sun::after{inset:9px}
.scene .star{position:absolute;width:3px;height:3px;background:#fff;opacity:.75}
.scene .cloud{position:absolute;height:22px;background:rgba(255,255,255,var(--cloud-a));border-radius:2px}
.scene .cloud::before,.scene .cloud::after{content:"";position:absolute;background:inherit;border-radius:2px}
.scene .cloud::before{left:18%;width:44%;height:22px;top:-18px}
.scene .cloud::after{left:52%;width:26%;height:12px;top:-10px}
.dimmer{position:fixed;inset:0;z-index:-1;background:var(--dim);pointer-events:none}
[data-sky=day]{--sky1:#3d8fe8;--sky2:#7cbcf3;--sky3:#cfe8fb;--sun:#fff3b0;--cloud-a:.6;--h1:#a7c8e6;--h2:#71b179;--h3:#4a944f;--h4:#2f6e3a;--grass:#8fd46a}
[data-sky=dawn]{--sky1:#5b6fcf;--sky2:#e9a0a8;--sky3:#ffd6a6;--sun:#ffd08a;--cloud-a:.5;--h1:#b9a2c6;--h2:#7f8c86;--h3:#566c5b;--h4:#3a4c41;--grass:#a8b77a}
[data-sky=dusk]{--sky1:#2c2f78;--sky2:#a35a8e;--sky3:#ffa07a;--sun:#ffb170;--cloud-a:.35;--h1:#6c4f7e;--h2:#4a3f63;--h3:#352f4d;--h4:#231f36;--grass:#7c6aa0}
[data-sky=night]{--sky1:#050918;--sky2:#0f1736;--sky3:#1d2852;--sun:#e8ecff;--cloud-a:.08;--h1:#1d2850;--h2:#162042;--h3:#111a35;--h4:#0b1226;--grass:#2b3d6e}

/* ================= glass ================= */
.glass{position:relative;isolation:isolate;overflow:hidden;border-radius:var(--r-xl);
  background:linear-gradient(165deg,var(--glass-hi),var(--glass-lo));
  -webkit-backdrop-filter:blur(20px) saturate(165%);backdrop-filter:blur(20px) saturate(165%);
  box-shadow:inset 0 0 0 1px var(--line),inset 0 1px 0 var(--edge),var(--shadow)}
.glass>.shine{position:absolute;left:0;top:0;width:520px;height:520px;margin:-260px 0 0 -260px;border-radius:50%;pointer-events:none;z-index:-1;
  background:radial-gradient(closest-side,rgba(255,255,255,.17),transparent);opacity:0;transition:opacity .35s;will-change:transform}
.glass.lit>.shine{opacity:1}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){.glass{background:var(--glass-solid)}}
@media (prefers-reduced-transparency:reduce){.glass,.tb,.toast,.sheet{-webkit-backdrop-filter:none!important;backdrop-filter:none!important;background:var(--glass-solid)!important}}

/* ================= title bar ================= */
.tb{position:fixed;top:0;left:0;right:0;height:var(--tb);z-index:30;display:flex;align-items:center;gap:6px;padding:0 0 0 12px;
  background:linear-gradient(180deg,color-mix(in srgb,var(--glass-hi) 80%,transparent),var(--glass-lo));
  -webkit-backdrop-filter:blur(24px) saturate(170%);backdrop-filter:blur(24px) saturate(170%);
  box-shadow:inset 0 -1px 0 var(--line),inset 0 1px 0 color-mix(in srgb,var(--edge) 60%,transparent)}
.tb .drag{flex:1;align-self:stretch;display:flex;align-items:center;gap:12px;min-width:0}
.brand{all:unset;display:flex;align-items:center;gap:10px;cursor:pointer;padding:6px 8px 6px 4px;border-radius:12px}
.brand:hover{background:var(--field)}.brand:focus-visible{outline:2px solid var(--t-acc)}
.logo{width:24px;height:24px;flex:none}
.brand b{font:650 14.5px/1 var(--display);letter-spacing:-.01em;white-space:nowrap}
.crumb{display:flex;align-items:center;gap:8px;min-width:0;color:var(--ink-3);font-size:13.5px}
.crumb span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.crumb svg{width:14px;height:14px;flex:none;opacity:.6}
.tbtn{all:unset;display:grid;place-items:center;height:32px;min-width:32px;padding:0 9px;border-radius:10px;cursor:pointer;color:var(--ink-2);gap:6px;grid-auto-flow:column;font-size:13px;font-weight:600}
.tbtn:hover{background:var(--field-hi);color:var(--ink)}.tbtn:focus-visible{outline:2px solid var(--t-acc)}
.tbtn svg{width:17px;height:17px}
.tbtn.update{background:color-mix(in srgb,var(--emerald) 22%,transparent);color:var(--t-ok);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--emerald) 45%,transparent)}
.running-pill{display:flex;align-items:center;gap:7px;font-size:12.5px;font-weight:600;color:var(--t-ok);padding:5px 10px;border-radius:99px;background:color-mix(in srgb,var(--emerald) 16%,transparent);cursor:pointer;border:0}
.running-pill i{width:8px;height:8px;border-radius:2px;background:var(--emerald);box-shadow:0 0 10px var(--emerald)}
.wc{display:flex;align-self:stretch;margin-left:6px}
.wc button{all:unset;width:46px;display:grid;place-items:center;cursor:default;color:var(--ink-2)}
.wc button:hover{background:var(--field-hi);color:var(--ink)}
.wc button.close:hover{background:#e81123;color:#fff}
.wc svg{width:11px;height:11px}
.rz{position:fixed;z-index:40}
.rz.t{top:0;left:6px;right:6px;height:4px;cursor:n-resize}.rz.b{bottom:0;left:6px;right:6px;height:5px;cursor:s-resize}
.rz.l{left:0;top:6px;bottom:6px;width:5px;cursor:w-resize}.rz.r{right:0;top:6px;bottom:6px;width:5px;cursor:e-resize}
.rz.tl{top:0;left:0;width:8px;height:8px;cursor:nw-resize}.rz.tr{top:0;right:0;width:8px;height:8px;cursor:ne-resize}
.rz.bl{bottom:0;left:0;width:8px;height:8px;cursor:sw-resize}.rz.br{bottom:0;right:0;width:10px;height:10px;cursor:se-resize}

/* ================= layout ================= */
.main{position:fixed;top:var(--tb);left:0;right:0;bottom:0;overflow-y:auto;overflow-x:hidden;scroll-behavior:smooth}
.page{width:min(1060px,100%);margin:0 auto;padding:34px 28px 80px}
.view{animation:none}
h1,h2,h3{font-family:var(--display);margin:0;letter-spacing:-.02em}
h1{font-weight:700;font-size:clamp(30px,4.4vw,44px);line-height:1.05;letter-spacing:-.035em;text-wrap:balance}
.lede{color:var(--ink-2);font-size:16px;max-width:58ch;margin:10px 0 0}
.row-between{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;flex-wrap:wrap}
.muted{color:var(--ink-3)}
.sec-title{font:650 19px/1.2 var(--display);margin:0 0 4px}
.sec-sub{color:var(--ink-3);margin:0 0 16px;font-size:13.5px}

/* ================= controls ================= */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:40px;padding:0 17px;border-radius:13px;cursor:pointer;font-weight:600;white-space:nowrap;border:0;
  background:var(--field);box-shadow:inset 0 0 0 1px var(--line),inset 0 1px 0 var(--edge);transition:background .18s,transform .12s}
.btn:hover{background:var(--field-hi)}.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn svg{width:17px;height:17px;flex:none}
.btn.quiet{background:transparent;box-shadow:none;color:var(--ink-2)}.btn.quiet:hover{background:var(--field);color:var(--ink)}
.btn.sm{min-height:32px;padding:0 12px;font-size:13px;border-radius:10px}
.btn.primary{color:#fff;background:linear-gradient(135deg,#5b80ff,#8b6dff);box-shadow:inset 0 1px 0 rgba(255,255,255,.45),0 12px 26px -12px #6a74ff}
.btn.primary:hover{filter:brightness(1.07)}
.btn.go{color:#04281a;background:linear-gradient(135deg,#5ff0ab,#2ec486);box-shadow:inset 0 1px 0 rgba(255,255,255,.6),0 14px 30px -12px #25c27f}
.btn.go:hover{filter:brightness(1.05)}
.btn.stop{color:#fff;background:linear-gradient(135deg,#ff7a7d,#e2414c);box-shadow:inset 0 1px 0 rgba(255,255,255,.4),0 14px 30px -12px #e2414c}
.btn.big{min-height:58px;padding:0 30px;font-size:17px;border-radius:18px;font-family:var(--display);font-weight:650;letter-spacing:-.01em}
.btn.big svg{width:20px;height:20px}
input[type=text],input[type=password],input[type=search],select,textarea{width:100%;min-width:0;padding:11px 14px;border-radius:13px;border:0;background:var(--field);box-shadow:inset 0 0 0 1px var(--line);outline:none;transition:background .15s,box-shadow .15s}
input:hover,select:hover,textarea:hover{background:var(--field-hi)}
input:focus,select:focus,textarea:focus{background:var(--field-hi);box-shadow:inset 0 0 0 1px var(--t-acc),0 0 0 3px color-mix(in srgb,var(--lapis) 28%,transparent)}
select{appearance:none;padding-right:38px;background-image:linear-gradient(45deg,transparent 50%,var(--ink-2) 50%),linear-gradient(135deg,var(--ink-2) 50%,transparent 50%);background-position:calc(100% - 20px) 52%,calc(100% - 15px) 52%;background-size:5px 5px;background-repeat:no-repeat}
select option{background:#1b2340;color:#fff}
::placeholder{color:var(--ink-3)}
label.field{display:grid;gap:7px;font-weight:600;font-size:13.5px}
label.field small{font-weight:400;color:var(--ink-3)}
.sw{position:relative;flex:none;width:50px;height:30px}
.sw input{position:absolute;inset:0;margin:0;opacity:0;cursor:pointer;z-index:1}
.sw span{position:absolute;inset:0;border-radius:99px;background:var(--field);box-shadow:inset 0 0 0 1px var(--line);transition:background .25s}
.sw span::after{content:"";position:absolute;top:3px;left:3px;width:24px;height:24px;border-radius:8px;background:#fff;box-shadow:0 2px 6px rgba(0,0,0,.35);transition:transform .3s cubic-bezier(.3,1.4,.5,1)}
.sw input:checked+span{background:linear-gradient(120deg,var(--emerald),#2fbf9a)}
.sw input:checked+span::after{transform:translateX(20px)}
.sw input:focus-visible+span{outline:2px solid var(--t-acc);outline-offset:3px}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:30px;background:transparent;cursor:pointer;--p:50%}
input[type=range]::-webkit-slider-runnable-track{height:8px;border-radius:9px;background:linear-gradient(90deg,var(--emerald),var(--lapis) var(--p),var(--field) var(--p));box-shadow:inset 0 0 0 1px var(--line)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:24px;height:24px;margin-top:-8px;border-radius:7px;background:#fff;box-shadow:0 3px 10px rgba(0,0,0,.4)}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:8px;font-size:12.5px;background:var(--field);box-shadow:inset 0 0 0 1px var(--line);color:var(--ink-2);white-space:nowrap}
.chip.pix{font-family:var(--pix);font-size:11.5px;letter-spacing:.02em;padding:4px 9px}
.dot{width:9px;height:9px;border-radius:2.5px;flex:none;background:var(--ink-3)}
.dot.online{background:var(--emerald);box-shadow:0 0 10px var(--emerald)}.dot.starting,.dot.stopping{background:var(--gold);box-shadow:0 0 10px var(--gold)}.dot.crashed{background:var(--redstone);box-shadow:0 0 10px var(--redstone)}.dot.setting_up{background:var(--lapis);box-shadow:0 0 10px var(--lapis);animation:blink 1.2s steps(2) infinite}
@keyframes blink{50%{opacity:.35}}@media (prefers-reduced-motion:reduce){.dot.setting_up,.running-pill.job i{animation:none}}
.running-pill.job{color:var(--t-acc);background:color-mix(in srgb,var(--lapis) 18%,transparent)}.running-pill.job i{background:var(--lapis);box-shadow:0 0 10px var(--lapis);animation:blink 1.2s steps(2) infinite}
.running-pill.job.ready{color:var(--t-ok);background:color-mix(in srgb,var(--emerald) 16%,transparent)}.running-pill.job.ready i{background:var(--emerald);box-shadow:0 0 10px var(--emerald);animation:none}
.running-pill.job.bad{color:var(--t-bad);background:color-mix(in srgb,var(--redstone) 16%,transparent)}.running-pill.job.bad i{background:var(--redstone);box-shadow:none;animation:none}
.pfoot .acts{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}

/* ================= home ================= */
.servers{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:18px;margin-top:28px}
.scard{border:0;margin:0;font:inherit;color:inherit;text-align:left;box-sizing:border-box;display:flex;flex-direction:column;gap:16px;padding:20px;border-radius:var(--r-lg);cursor:pointer;min-height:180px}
.scard:focus-visible{outline:2px solid var(--t-acc);outline-offset:3px}
.scard .top{display:flex;gap:14px;align-items:center;min-width:0}
.picon{width:52px;height:52px;border-radius:14px;flex:none;object-fit:cover;background:var(--field);box-shadow:inset 0 0 0 1px var(--line);display:grid;place-items:center;overflow:hidden}
.picon img{width:100%;height:100%;object-fit:cover}
.picon svg{width:30px;height:30px}
.scard h3{font-size:19px;font-weight:650;line-height:1.2;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow-wrap:anywhere}
.scard .chips{display:flex;flex-wrap:wrap;gap:6px}
.scard .foot{margin-top:auto;display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:13.5px;color:var(--ink-2)}
.scard .state{display:flex;align-items:center;gap:8px}
.scard.new{border:1.5px dashed color-mix(in srgb,var(--ink) 22%,transparent);background:transparent;box-shadow:none;-webkit-backdrop-filter:none;backdrop-filter:none;align-items:center;justify-content:center;text-align:center;gap:10px}
.scard.new:hover{background:color-mix(in srgb,var(--glass-hi) 60%,transparent)}
.scard.new .plus{width:52px;height:52px;border-radius:16px;display:grid;place-items:center;background:linear-gradient(135deg,var(--emerald),var(--lapis));color:#fff;box-shadow:0 12px 28px -12px var(--lapis)}
.scard.new .plus svg{width:26px;height:26px}
.scard.missing{opacity:.6}
.empty{margin-top:34px;padding:44px 40px;border-radius:var(--r-xl);display:grid;grid-template-columns:auto 1fr;gap:36px;align-items:center}
.empty .art{width:150px;height:150px}
.empty h2{font-size:30px;font-weight:700;letter-spacing:-.03em;line-height:1.1}
.empty p{color:var(--ink-2);font-size:15.5px;max-width:48ch;margin:12px 0 22px}
.empty .acts{display:flex;gap:10px;flex-wrap:wrap}
.how{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:22px}
.how div{padding:16px 18px;border-radius:var(--r-md);background:var(--well);box-shadow:inset 0 0 0 1px var(--line)}
.how b{display:block;font:650 15px var(--display);margin:6px 0 2px}
.how span{color:var(--ink-3);font-size:13px}
.how i{font:12px var(--pix);color:var(--t-acc);font-style:normal}

/* ================= wizard ================= */
.wizard{margin-top:26px;display:grid;gap:18px}
.steps{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:18px}
.steps .s{display:flex;align-items:center;gap:9px;padding:6px 12px 6px 6px;border-radius:12px;color:var(--ink-3);font-weight:600;font-size:13.5px}
.steps .s i{width:26px;height:26px;border-radius:8px;display:grid;place-items:center;font:12px var(--pix);font-style:normal;background:var(--field);box-shadow:inset 0 0 0 1px var(--line)}
.steps .s.on{color:var(--ink);background:var(--field)}
.steps .s.on i{background:linear-gradient(135deg,var(--lapis),var(--amethyst));color:#fff;box-shadow:none}
.steps .s.done i{background:var(--emerald);color:#04281a;box-shadow:none}
.steps .bar{width:22px;height:2px;background:var(--line);border-radius:2px}
.choices{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.choice{border:0;margin:0;font:inherit;color:inherit;text-align:left;box-sizing:border-box;cursor:pointer;padding:20px;border-radius:var(--r-lg);display:grid;gap:10px;align-content:start;min-height:150px;transition:transform .15s}
.choice:hover{transform:translateY(-2px)}
.choice:focus-visible{outline:2px solid var(--t-acc);outline-offset:3px}
.choice .ci{width:44px;height:44px;border-radius:13px;display:grid;place-items:center;color:#fff}
.choice .ci svg{width:22px;height:22px}
.choice b{font:650 17px/1.2 var(--display)}
.choice span{color:var(--ink-3);font-size:13.5px}
.choice[aria-pressed=true]{box-shadow:inset 0 0 0 2px var(--t-acc),inset 0 1px 0 var(--edge),var(--shadow)}
.panel{padding:24px;border-radius:var(--r-xl)}
.search{display:flex;gap:10px;align-items:center}
.search input{padding-left:42px;background-image:none}
.search .sicon{position:relative;flex:1}
.search .sicon svg{position:absolute;left:14px;top:50%;width:17px;height:17px;transform:translateY(-50%);color:var(--ink-3);pointer-events:none}
.packs{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px;margin-top:16px}
.pack{all:unset;box-sizing:border-box;cursor:pointer;display:grid;grid-template-columns:auto 1fr;gap:12px;padding:13px;border-radius:16px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line);transition:background .15s,box-shadow .15s;min-width:0}
.pack:hover{background:var(--field-hi)}
.pack:focus-visible{outline:2px solid var(--t-acc)}
.pack[aria-pressed=true]{box-shadow:inset 0 0 0 2px var(--emerald);background:color-mix(in srgb,var(--emerald) 12%,var(--well))}
.pack .picon{width:46px;height:46px;border-radius:12px}
.pack b{display:block;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pack p{margin:2px 0 6px;color:var(--ink-3);font-size:12.5px;line-height:1.35;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.pack .meta{display:flex;gap:6px;flex-wrap:wrap;font-size:11.5px}
.pack .meta .chip{font-size:11.5px;padding:2px 7px}
.more{display:flex;justify-content:center;margin-top:14px}
.drop{border-radius:var(--r-lg);padding:34px;text-align:center;background:var(--well);border:1.5px dashed color-mix(in srgb,var(--ink) 25%,transparent);transition:background .2s,border-color .2s}
.drop.over{background:color-mix(in srgb,var(--emerald) 14%,var(--well));border-color:var(--emerald)}
.drop h3{font-size:19px}
.drop p{color:var(--ink-3);margin:6px 0 16px}
.picked{display:flex;gap:14px;align-items:center;padding:14px;border-radius:16px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line)}
.picked .t{flex:1;min-width:0}.picked b{display:block;overflow-wrap:anywhere}
.form{display:grid;gap:20px}
.kv{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 16px;border-radius:15px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line)}
.kv .t{min-width:0}.kv b{display:block;font-weight:650}.kv span{color:var(--ink-3);font-size:13px;overflow-wrap:anywhere}
.agree{display:flex;gap:14px;align-items:flex-start;padding:18px;border-radius:16px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line)}
.agree p{margin:0;color:var(--ink-2)}
.wnav{display:flex;justify-content:space-between;gap:10px;margin-top:4px}
.hint{color:var(--t-warn);font-size:13.5px;margin:6px 2px 0}

/* ================= build / progress ================= */
.progress{padding:30px;border-radius:var(--r-xl);margin-top:26px}
.progress h2{font-size:28px;font-weight:700;letter-spacing:-.03em}
.progress .now{color:var(--ink-2);margin:6px 0 24px;min-height:1.5em;font-size:15px}
.track{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:6px;margin-bottom:14px}
.track .st{display:flex;flex-direction:column;align-items:center;gap:8px;text-align:center;font-size:12.5px;color:var(--ink-3)}
.track .st .d{position:relative;width:30px;height:30px;border-radius:9px;display:grid;place-items:center;background:var(--field);box-shadow:inset 0 0 0 1px var(--line)}
.track .st .d svg{width:15px;height:15px;opacity:0}
.track .st .d::after{content:"";position:absolute;inset:-6px;border-radius:12px;border:2px solid var(--t-acc);opacity:0}
.track .st.on{color:var(--ink)}.track .st.on .d{box-shadow:inset 0 0 0 1.5px var(--t-acc)}
.track .st.on .d::after{animation:pulse 1.8s ease-out infinite}
.track .st.done{color:var(--ink-2)}.track .st.done .d{background:var(--emerald);color:#04281a;box-shadow:none}.track .st.done .d svg{opacity:1}
.track .st.bad .d{background:var(--redstone);color:#fff;box-shadow:none}.track .st.bad .d svg{opacity:1}
@keyframes pulse{0%{opacity:.7;transform:scale(.85)}100%{opacity:0;transform:scale(1.35)}}
.bar{height:8px;border-radius:4px;background:var(--field);overflow:hidden;box-shadow:inset 0 0 0 1px var(--line)}
.bar i{display:block;height:100%;width:var(--p,0%);background:repeating-linear-gradient(90deg,var(--emerald) 0 14px,#4be0a0 14px 28px);transition:width .6s}
.bar.ind i{width:30%;animation:slide 1.5s ease-in-out infinite}
@keyframes slide{from{transform:translateX(-100%)}to{transform:translateX(340%)}}
.pfoot{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:18px;flex-wrap:wrap}
details.log{margin-top:16px}
details.log summary{cursor:pointer;color:var(--ink-3);font-size:13.5px;list-style:none;display:inline-flex;gap:6px;align-items:center}
details.log summary::-webkit-details-marker{display:none}
.feed{max-height:320px;overflow:auto;margin-top:10px;padding:12px;border-radius:15px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line);display:grid;gap:1px;align-content:start}
.ln{font:12.5px/1.6 var(--mono);color:var(--ink-2);white-space:pre-wrap;overflow-wrap:anywhere}
.ln.dim{color:var(--ink-3)}.ln.h{font:600 13.5px/1.5 var(--ui);color:var(--ink);margin-top:8px}.ln.warn{color:var(--t-warn)}.ln.bad{color:var(--t-bad)}
.ev{display:flex;gap:10px;align-items:flex-start;padding:6px 4px;font-size:13.5px}
.ev .b{flex:none;width:22px;height:22px;border-radius:6px;display:grid;place-items:center;color:var(--tone);background:color-mix(in srgb,var(--tone) 18%,transparent)}
.ev .b svg{width:13px;height:13px}.ev .t{flex:1;min-width:0;overflow-wrap:anywhere}.ev small{display:block;color:var(--ink-3)}
.t-ok{--tone:var(--t-ok)}.t-warn{--tone:var(--t-warn)}.t-bad{--tone:var(--t-bad)}.t-info{--tone:var(--t-acc)}

/* ================= control room ================= */
.back{all:unset;cursor:pointer;display:inline-flex;align-items:center;gap:6px;color:var(--ink-2);font-weight:600;font-size:13.5px;padding:6px 10px 6px 6px;border-radius:10px;margin-bottom:14px}
.back:hover{background:var(--field);color:var(--ink)}.back:focus-visible{outline:2px solid var(--t-acc)}
.back svg{width:16px;height:16px}
.shead{display:flex;gap:18px;align-items:center;min-width:0}
.shead .picon{width:64px;height:64px;border-radius:18px}
.shead h1{overflow-wrap:anywhere}
.shead .chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.banner{display:flex;gap:16px;align-items:center;padding:16px 18px;border-radius:var(--r-lg);margin-top:22px}
.banner.bad{background:linear-gradient(120deg,color-mix(in srgb,var(--redstone) 26%,var(--glass-hi)),var(--glass-lo))}
.banner.good{background:linear-gradient(120deg,color-mix(in srgb,var(--emerald) 24%,var(--glass-hi)),var(--glass-lo))}
.banner .bi{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;flex:none;color:#fff}
.banner.bad .bi{background:var(--redstone)}.banner.good .bi{background:var(--emerald);color:#04281a}
.banner .bi svg{width:20px;height:20px}
.banner .t{flex:1;min-width:0}.banner b{display:block;font:650 16px var(--display)}.banner span{color:var(--ink-2);font-size:13.5px}
.room{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:18px;margin-top:22px}
.beacon{padding:26px;border-radius:var(--r-xl);display:flex;flex-direction:column;gap:22px}
.bstate{display:flex;gap:20px;align-items:center}
.orb{position:relative;width:86px;height:86px;flex:none;border-radius:24px;--c:var(--ink-3);
  background:radial-gradient(110% 110% at 30% 20%,rgba(255,255,255,.65),transparent 50%),linear-gradient(145deg,color-mix(in srgb,var(--c) 85%,white),color-mix(in srgb,var(--c) 75%,black));
  box-shadow:inset 0 1px 1px rgba(255,255,255,.8),0 18px 40px -14px var(--c)}
.orb::before{content:"";position:absolute;inset:22px;border-radius:9px;background:rgba(255,255,255,.28);box-shadow:inset 0 0 0 2px rgba(255,255,255,.35)}
.orb::after{content:"";position:absolute;inset:-8px;border-radius:30px;border:2px solid var(--c);opacity:0}
.orb.online{--c:var(--emerald)}.orb.starting,.orb.stopping{--c:var(--gold)}.orb.crashed{--c:var(--redstone)}.orb.offline{--c:#8a93b0}
.orb.starting::after,.orb.stopping::after{animation:pulse 1.6s ease-out infinite}
.bstate h2{font-size:32px;font-weight:700;letter-spacing:-.035em;line-height:1.05}
.bstate p{margin:6px 0 0;color:var(--ink-2)}
.bacts{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.stats div{padding:12px 14px;border-radius:14px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line)}
.stats b{display:block;font:15px var(--pix);margin-top:2px;letter-spacing:.02em}
.stats span{color:var(--ink-3);font-size:12px}
.join{padding:24px;border-radius:var(--r-xl);display:flex;flex-direction:column;gap:12px}
.addr{display:flex;align-items:center;gap:12px;padding:12px 12px 12px 14px;border-radius:15px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line)}
.addr .t{flex:1;min-width:0}
.addr small{display:block;color:var(--ink-3);font-size:12.5px}
.addr code{font:600 15px var(--mono);overflow-wrap:anywhere}
.addr .ai{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;background:var(--field);flex:none}
.addr .ai svg{width:17px;height:17px}
.join details{border-radius:14px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line);padding:0 14px}
.join details summary{cursor:pointer;padding:12px 0;font-weight:600;font-size:13.5px;list-style:none;display:flex;justify-content:space-between;align-items:center}
.join details summary::-webkit-details-marker{display:none}
.join details summary svg{width:15px;height:15px;transition:transform .25s}.join details[open] summary svg{transform:rotate(180deg)}
.join details ol{margin:0 0 14px;padding-left:20px;color:var(--ink-2);font-size:13.5px;display:grid;gap:6px}
.reach{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink-2)}
.tabs{display:flex;gap:4px;margin-top:22px;padding:4px;border-radius:15px;width:max-content;max-width:100%;overflow-x:auto;background:var(--field);box-shadow:inset 0 0 0 1px var(--line)}
.tabs button{all:unset;cursor:pointer;padding:8px 16px;border-radius:11px;font-weight:600;font-size:13.5px;color:var(--ink-2);white-space:nowrap;display:flex;gap:7px;align-items:center}
.tabs button[aria-selected=true]{background:var(--field-hi);color:var(--ink);box-shadow:inset 0 1px 0 var(--edge),0 4px 12px -6px rgba(0,0,0,.35)}
.tabs button:focus-visible{outline:2px solid var(--t-acc)}
.tabs .badge{min-width:18px;height:18px;padding:0 5px;border-radius:6px;display:grid;place-items:center;font:11px var(--pix);background:var(--redstone);color:#fff}
.tabpanel{margin-top:14px;padding:22px;border-radius:var(--r-xl)}
.console{height:340px;overflow:auto;padding:12px 14px;border-radius:15px;background:rgba(4,8,18,.62);box-shadow:inset 0 0 0 1px var(--line);display:grid;align-content:start;gap:1px}
:root[data-theme=light] .console{background:rgba(14,22,44,.86)}
.console .c{font:12.5px/1.6 var(--mono);color:#cfd8ee;white-space:pre-wrap;overflow-wrap:anywhere}
.console .c.warn{color:#ffd977}.console .c.error{color:#ff9a9d}.console .c.join{color:#7ff3bb}.console .c.leave{color:#9fb0d6}
.console .c.chat{color:#fff}.console .c.done{color:#7ff3bb;font-weight:700}.console .c.command{color:#a9c1ff}
.console .empty-c{color:#8e9ab8;font:13.5px var(--ui);padding:20px;text-align:center}
.cmd{display:flex;gap:10px;margin-top:12px}
.cmd input{font-family:var(--mono)}
.quick{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.players{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
.player{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:14px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line)}
.player img{width:36px;height:36px;border-radius:7px;image-rendering:pixelated;background:var(--field)}
.player b{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.setlist{display:grid;gap:12px}
.set{display:grid;grid-template-columns:minmax(0,1fr) minmax(170px,260px);gap:16px;align-items:center;padding:14px 16px;border-radius:15px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line)}
.set b{display:block;font-weight:650}.set span{color:var(--ink-3);font-size:13px}
.set .ctl{display:flex;justify-content:flex-end}
.item{display:flex;align-items:center;gap:12px;padding:11px 0;border-top:1px solid var(--line)}
.item:first-child{border-top:0}
.item .t{flex:1;min-width:0}.item b{display:block;font-weight:600;overflow-wrap:anywhere}.item small{display:block;color:var(--ink-3)}
.finding{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;border-radius:14px;background:var(--well);box-shadow:inset 0 0 0 1px var(--line);margin-top:8px}
.finding svg{width:18px;height:18px;flex:none;margin-top:2px;color:var(--tone)}

/* ================= settings ================= */
.seg{display:inline-flex;padding:4px;border-radius:13px;background:var(--field);box-shadow:inset 0 0 0 1px var(--line);gap:2px}
.seg button{all:unset;cursor:pointer;padding:7px 14px;border-radius:9px;font-weight:600;font-size:13px;color:var(--ink-2)}
.seg button[aria-pressed=true]{background:var(--field-hi);color:var(--ink);box-shadow:inset 0 1px 0 var(--edge)}
.seg button:focus-visible{outline:2px solid var(--t-acc)}

/* ================= modal, toast ================= */
.veil{position:fixed;inset:0;z-index:50;display:grid;place-items:center;padding:24px;background:rgba(5,8,20,.5)}
.sheet{width:min(560px,100%);max-height:min(660px,88vh);display:flex;flex-direction:column;padding:22px;border-radius:var(--r-xl)}
.sheet header{display:flex;align-items:center;justify-content:space-between;gap:10px}
.sheet h2{font-size:21px;font-weight:650}
.sheet .body{margin-top:12px;overflow:auto}
.sheet .body p{color:var(--ink-2);margin:0 0 10px}
.sheet footer{display:flex;justify-content:flex-end;gap:10px;margin-top:18px;align-items:center}
.sheet footer small{margin-right:auto;color:var(--ink-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.crumbs{display:flex;flex-wrap:wrap;gap:2px;margin:10px 0 8px;font-size:13px}
.crumbs button{all:unset;cursor:pointer;padding:3px 8px;border-radius:8px;color:var(--ink-2)}.crumbs button:hover{background:var(--field)}
.crumbs span{color:var(--ink-3);align-self:center}
.list{overflow:auto;flex:1;min-height:200px;display:grid;align-content:start}
.list[aria-busy=true]{opacity:.45;pointer-events:none}
.list button{all:unset;box-sizing:border-box;display:flex;align-items:center;gap:12px;padding:9px 12px;border-radius:12px;cursor:pointer;min-width:0}
.list button:hover{background:var(--field)}.list button:focus-visible{outline:2px solid var(--t-acc)}
.list button span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.list svg{width:18px;height:18px;flex:none;color:var(--t-acc)}
.notes{font-size:14px;color:var(--ink-2)}.notes h4{font:650 15px var(--display);color:var(--ink);margin:14px 0 6px}.notes ul{margin:0;padding-left:20px;display:grid;gap:4px}
.toast{position:fixed;left:50%;bottom:26px;z-index:60;transform:translate(-50%,30px);opacity:0;pointer-events:none;transition:all .35s cubic-bezier(.3,1.3,.5,1);padding:12px 20px;border-radius:14px;font-weight:600;
  background:var(--glass-hi);-webkit-backdrop-filter:blur(22px);backdrop-filter:blur(22px);box-shadow:inset 0 0 0 1px var(--line),var(--shadow);max-width:min(92vw,560px)}
.toast.show{transform:translate(-50%,0);opacity:1}.toast.bad{color:var(--t-bad)}

@media (max-width:820px){.room{grid-template-columns:1fr}.choices{grid-template-columns:1fr}.how{grid-template-columns:1fr}.empty{grid-template-columns:1fr;text-align:left}.empty .art{width:96px;height:96px}.set{grid-template-columns:1fr}.set .ctl{justify-content:flex-start}}
@media (max-width:560px){.page{padding:22px 14px 60px}.stats{grid-template-columns:1fr 1fr}.track{grid-auto-flow:row;grid-template-columns:repeat(3,1fr)}.tb .crumb{display:none}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
</style>
</head>
<body>
<div class="scene" id="scene" aria-hidden="true"></div>
<div class="dimmer" aria-hidden="true"></div>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="i-gblock" viewBox="4 3 24 26"><path d="M6.5 10.7L16 15.9v10.6l-9.5-5.2z" fill="#8a5a36"/><path d="M25.5 10.7L16 15.9v10.6l9.5-5.2z" fill="#6b4428"/><path d="M6.5 10.7L16 15.9v3l-9.5-5.2z" fill="#3fbf6e"/><path d="M25.5 10.7L16 15.9v3l9.5-5.2z" fill="#2f9e58"/><path d="M10.6 15.1v2.2M13.3 16.6v1.6M18.7 16.6v2.4M21.4 15.1v1.7" stroke="#2a9d5c" stroke-width="1.1"/><path d="M16 5.5l9.5 5.2L16 15.9 6.5 10.7z" fill="#5fe08c"/></symbol>
<symbol id="i-block" viewBox="0 0 32 32"><path d="M16 3l12 6.5v13L16 29 4 22.5v-13z" fill="#2fbf7d"/><path d="M16 3l12 6.5L16 16 4 9.5z" fill="#7ff0b4"/><path d="M16 16v13L4 22.5v-13z" fill="#1d8f5c"/><path d="M4 9.5L16 16l12-6.5v3.2L16 19.2 4 12.7z" fill="#5fdc9a" opacity=".55"/></symbol>
<symbol id="i-play" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13a1 1 0 001.5.86l10.4-6.5a1 1 0 000-1.72L9.5 4.64A1 1 0 008 5.5z"/></symbol>
<symbol id="i-stop" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5"/></symbol>
<symbol id="i-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></symbol>
<symbol id="i-file" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3h7l5 5v13H7zM14 3v5h5"/></symbol>
<symbol id="i-folder" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></symbol>
<symbol id="i-grass" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5L12 12l8-4.5M12 12v9"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7"/></symbol>
<symbol id="i-x" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></symbol>
<symbol id="i-alert" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4l9 16H3zM12 10v4M12 17.4v.1"/></symbol>
<symbol id="i-gear" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/></symbol>
<symbol id="i-back" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"/></symbol>
<symbol id="i-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></symbol>
<symbol id="i-down" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></symbol>
<symbol id="i-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 012-2h9"/></symbol>
<symbol id="i-home" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-7 9 7v9a1 1 0 01-1 1h-5v-6H9v6H4a1 1 0 01-1-1z"/></symbol>
<symbol id="i-wifi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 12.5a10 10 0 0114 0M8.5 16a5 5 0 017 0M2 9a14.5 14.5 0 0120 0"/><circle cx="12" cy="19.5" r="1" fill="currentColor"/></symbol>
<symbol id="i-globe" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18"/></symbol>
<symbol id="i-monitor" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></symbol>
<symbol id="i-wrench" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a4 4 0 005 5l-8.6 8.6a2.1 2.1 0 01-3-3z"/><path d="M14.7 6.3L17 4"/></symbol>
<symbol id="i-undo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14L4 9l5-5M4 9h10a6 6 0 010 12h-3"/></symbol>
<symbol id="i-sparkle" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l1.9 6.1L20 10l-6.1 1.9L12 18l-1.9-6.1L4 10l6.1-1.9z"/></symbol>
<symbol id="i-restart" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 11-2.3-5.7M20 4v5h-5"/></symbol>
<symbol id="i-min" viewBox="0 0 12 12"><path d="M1 6h10" stroke="currentColor" stroke-width="1.1"/></symbol>
<symbol id="i-max" viewBox="0 0 12 12"><rect x="1.5" y="1.5" width="9" height="9" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.1"/></symbol>
<symbol id="i-unmax" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.1"><rect x="1.5" y="3.5" width="7" height="7" rx="1.2"/><path d="M3.5 3.5V2.6c0-.6.5-1.1 1.1-1.1h4.8c.6 0 1.1.5 1.1 1.1v4.8c0 .6-.5 1.1-1.1 1.1h-.9"/></symbol>
<symbol id="i-close" viewBox="0 0 12 12"><path d="M1.5 1.5l9 9M10.5 1.5l-9 9" stroke="currentColor" stroke-width="1.1"/></symbol>
<symbol id="i-users" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0113 0M16 4.6a3.5 3.5 0 010 6.8M21.5 20a6.5 6.5 0 00-4-6"/></symbol>
<symbol id="i-terminal" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M12 15h5"/></symbol>
<symbol id="i-sliders" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></symbol>
<symbol id="i-heart" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 20s-7-4.4-7-10a4 4 0 017-2.6A4 4 0 0119 10c0 5.6-7 10-7 10z"/></symbol>
</defs></svg>

<!-- ======================= title bar ======================= -->
<header class="tb" id="tb">
  <div class="drag" id="drag">
    <button class="brand" id="go-home" title="All servers"><svg class="logo"><use href="#i-gblock"/></svg><b>Modpack Server Builder</b></button>
    <nav class="crumb" id="crumb" aria-label="Where you are"></nav>
  </div>
  <button class="running-pill job" id="job-pill" hidden><i></i><span></span></button><button class="running-pill" id="run-pill" hidden><i></i><span></span></button>
  <button class="tbtn update" id="upd-pill" hidden><svg><use href="#i-sparkle"/></svg><span>Update ready</span></button>
  <button class="tbtn" id="go-settings" aria-label="Settings" title="Settings"><svg><use href="#i-gear"/></svg></button>
  <div class="wc" id="wc" hidden>
    <button data-wc="min" aria-label="Minimize"><svg><use href="#i-min"/></svg></button>
    <button data-wc="max" aria-label="Maximize"><svg><use href="#i-max" id="max-ic"/></svg></button>
    <button data-wc="close" class="close" aria-label="Close"><svg><use href="#i-close"/></svg></button>
  </div>
</header>
<div id="rz" hidden><i class="rz t" data-rz="top"></i><i class="rz b" data-rz="bottom"></i><i class="rz l" data-rz="left"></i><i class="rz r" data-rz="right"></i><i class="rz tl" data-rz="topleft"></i><i class="rz tr" data-rz="topright"></i><i class="rz bl" data-rz="bottomleft"></i><i class="rz br" data-rz="bottomright"></i></div>

<main class="main" id="main">
<div class="page">

<!-- ======================= home ======================= -->
<section class="view" id="v-home" hidden>
  <div class="row-between">
    <div><h1 id="home-title">Your servers</h1><p class="lede" id="home-lede">Pick a server to start it, or make a new one.</p></div>
    <button class="btn primary" id="home-new"><svg><use href="#i-plus"/></svg>Create a server</button>
  </div>
  <div class="glass empty" id="home-empty" hidden data-shine>
    <svg class="art"><use href="#i-gblock"/></svg>
    <div>
      <h2>Let’s set up your first server</h2>
      <p>Pick a modpack, or plain Minecraft, and the app does the rest. It downloads everything, sets it up and checks that it works. No technical knowledge needed.</p>
      <div class="acts"><button class="btn primary big" id="empty-new"><svg><use href="#i-plus"/></svg>Create a server</button><button class="btn quiet" id="empty-import">I already have a server folder</button></div>
      <div class="how"><div><i>1</i><b>Choose a game</b><span>Search modpacks by name, or use plain Minecraft.</span></div><div><i>2</i><b>We set it up</b><span>Downloads, fixes and tests happen for you.</span></div><div><i>3</i><b>Press Start</b><span>Share the address with friends and play.</span></div></div>
    </div>
  </div>
  <div class="servers" id="servers"></div>
</section>

<!-- ======================= create ======================= -->
<section class="view" id="v-create" hidden>
  <button class="back" id="create-back"><svg><use href="#i-back"/></svg>All servers</button>
  <h1 id="create-title">Create a server</h1>
  <div class="steps" id="wsteps"><div class="s" data-s="1"><i>1</i>Choose a game</div><span class="bar"></span><div class="s" data-s="2"><i>2</i>Name and memory</div><span class="bar"></span><div class="s" data-s="3"><i>3</i>Create</div></div>
  <div class="wizard">
    <!-- step 1 -->
    <div id="w1">
      <div class="choices" role="group" aria-label="What do you want to play?">
        <button class="glass choice" data-src="modrinth" aria-pressed="false" data-shine><span class="ci" style="background:linear-gradient(135deg,#5ff0ab,#2fbf8d)"><svg><use href="#i-search"/></svg></span><b>Find a modpack</b><span>Search thousands of modpacks by name. Nothing to download first.</span></button>
        <button class="glass choice" data-src="file" aria-pressed="false" data-shine><span class="ci" style="background:linear-gradient(135deg,#7ea0ff,#8b6dff)"><svg><use href="#i-file"/></svg></span><b>Use a modpack file</b><span>A .zip from CurseForge or a .mrpack from Modrinth that you downloaded.</span></button>
        <button class="glass choice" data-src="vanilla" aria-pressed="false" data-shine><span class="ci" style="background:linear-gradient(135deg,#ffd166,#f39a4a)"><svg><use href="#i-grass"/></svg></span><b>Just Minecraft</b><span>The normal game with no mods. Great for a first server.</span></button>
      </div>
      <div class="glass panel" id="src-modrinth" hidden style="margin-top:18px" data-shine>
        <div class="search"><div class="sicon"><svg><use href="#i-search"/></svg><input type="search" id="q" placeholder="Search modpacks, like “All the Mods” or “Cobblemon”" autocomplete="off" spellcheck="false" aria-label="Search modpacks"></div></div>
        <p class="muted" id="q-note" style="margin:10px 2px 0;font-size:13px">Popular modpacks</p>
        <div class="packs" id="packs" aria-live="polite"></div>
        <div class="more"><button class="btn quiet sm" id="q-more" hidden>Show more</button></div>
      </div>
      <div class="glass panel" id="src-file" hidden style="margin-top:18px" data-shine>
        <div class="drop" id="drop"><h3>Drop your modpack file here</h3><p>A .zip from CurseForge or a .mrpack from Modrinth.</p><button class="btn" id="pick-file"><svg><use href="#i-folder"/></svg>Choose a file</button></div>
        <div class="picked" id="file-picked" hidden><div class="picon"><svg><use href="#i-block"/></svg></div><div class="t"><b id="fp-name"></b><span class="muted" id="fp-meta"></span></div><button class="btn quiet sm" id="fp-change">Change</button></div>
      </div>
      <div class="glass panel" id="src-vanilla" hidden style="margin-top:18px" data-shine>
        <label class="field">Minecraft version<small>The newest version is the right choice unless your friends play an older one.</small>
          <select id="v-ver"></select></label>
      </div>
      <div class="wnav" style="margin-top:18px"><span></span><button class="btn primary" id="w1-next" disabled>Next<svg><use href="#i-chev"/></svg></button></div>
    </div>
    <!-- step 2 -->
    <div id="w2" hidden>
      <div class="glass panel form" data-shine>
        <label class="field">Server name<small>Only you see this. Call it anything.</small><input type="text" id="w-name" maxlength="60" autocomplete="off"></label>
        <div class="kv"><div class="t"><b>Memory</b><span id="w-ram-text"></span></div><button class="btn quiet sm" id="w-ram-change">Change</button></div>
        <div id="w-ram-box" hidden><input type="range" id="w-ram" min="2" max="16" step="1" aria-label="Memory in gigabytes"><div class="muted" style="display:flex;justify-content:space-between;font-size:12.5px"><span>Less for your PC</span><b id="w-ram-val" style="color:var(--ink)"></b><span>Faster server</span></div></div>
        <div class="kv"><div class="t"><b>Where it’s saved</b><span id="w-dir-text"></span></div><button class="btn quiet sm" id="w-dir-change">Change</button></div>
      </div>
      <div class="wnav" style="margin-top:18px"><button class="btn quiet" id="w2-back"><svg><use href="#i-back"/></svg>Back</button><button class="btn primary" id="w2-next">Next<svg><use href="#i-chev"/></svg></button></div>
    </div>
    <!-- step 3 -->
    <div id="w3" hidden>
      <div class="glass panel form" data-shine>
        <div><h3 class="sec-title">One last thing</h3><p class="sec-sub" style="margin:0">Running a Minecraft server means agreeing to Mojang’s rules for servers (the “EULA”). In short: don’t sell things that change the game, and don’t pretend to be Mojang.</p></div>
        <label class="agree"><span class="sw"><input type="checkbox" id="w-eula"><span></span></span><p><b>I agree to the Minecraft EULA.</b> <a href="https://aka.ms/MinecraftEULA" target="_blank" rel="noopener noreferrer" style="color:var(--t-acc)">Read it</a></p></label>
        <div class="kv"><div class="t"><b>Test it once when it’s ready</b><span>Starts the server one time to check everything works, and fixes problems it finds. Recommended.</span></div><span class="sw"><input type="checkbox" id="w-test" checked aria-label="Test it once when it’s ready"><span></span></span></div>
        <div class="kv" id="w-summary"></div>
      </div>
      <p class="hint" id="w3-hint" hidden>Tick the agreement to continue. The server can’t run without it.</p>
      <div class="wnav" style="margin-top:18px"><button class="btn quiet" id="w3-back"><svg><use href="#i-back"/></svg>Back</button><button class="btn go big" id="w-create" disabled><svg><use href="#i-sparkle"/></svg>Create my server</button></div>
    </div>
  </div>
</section>

<!-- ======================= progress ======================= -->
<section class="view" id="v-build" hidden>
  <div class="glass progress" data-shine aria-live="polite">
    <h2 id="b-title" tabindex="-1">Setting up your server</h2>
    <p class="now" id="b-now">Getting started</p>
    <div class="track" id="b-track"></div>
    <div class="bar" id="b-bar"><i></i></div>
    <div class="pfoot"><span class="muted" id="b-tip">This usually takes a few minutes. You can look around the app meanwhile: the button at the top brings you back here.</span><div class="acts"><button class="btn sm quiet" id="b-leave"><svg><use href="#i-back"/></svg><span>All servers</span></button><button class="btn sm" id="b-skip" hidden><span>Skip the test</span></button><button class="btn sm" id="b-stop"><svg><use href="#i-x"/></svg><span>Stop</span></button></div></div>
    <details class="log" id="b-log"><summary><svg width="14" height="14"><use href="#i-down"/></svg>Show details</summary><div class="feed" id="feed" role="log"></div></details>
  </div>
  <div class="glass progress" id="b-result" hidden data-shine>
    <h2 id="r-title" tabindex="-1"></h2><p class="now" id="r-sub"></p>
    <div id="r-todos"></div>
    <div class="pfoot"><span></span><div style="display:flex;gap:10px"><button class="btn" id="r-home">All servers</button><button class="btn primary" id="r-open" hidden>Open server</button></div></div>
  </div>
</section>

<!-- ======================= server control room ======================= -->
<section class="view" id="v-server" hidden>
  <button class="back" id="srv-back"><svg><use href="#i-back"/></svg>All servers</button>
  <div class="shead"><div class="picon" id="srv-icon"></div><div style="min-width:0"><h1 id="srv-name"></h1><div class="chips" id="srv-chips"></div></div></div>
  <div class="glass banner bad" id="srv-crash" hidden><span class="bi"><svg><use href="#i-alert"/></svg></span><div class="t"><b>Your server stopped because of a problem</b><span id="srv-crash-text">The app can usually fix this for you.</span></div><button class="btn primary" id="srv-fix"><svg><use href="#i-wrench"/></svg>Fix it for me</button></div>
  <div class="glass banner good" id="srv-ready" hidden><span class="bi"><svg><use href="#i-check"/></svg></span><div class="t"><b>Your server is ready</b><span>Press Start, then share the address with your friends.</span></div></div>
  <div class="room">
    <div class="glass beacon" data-shine>
      <div class="bstate"><div class="orb offline" id="orb"></div><div><h2 id="st-title">Offline</h2><p id="st-sub">Press Start when you want to play.</p></div></div>
      <div class="bacts"><button class="btn go big" id="srv-start"><svg><use href="#i-play"/></svg>Start</button><button class="btn stop big" id="srv-stop" hidden><svg><use href="#i-stop"/></svg>Stop</button><button class="btn" id="srv-restart" hidden><svg><use href="#i-restart"/></svg>Restart</button><button class="btn quiet" id="srv-folder"><svg><use href="#i-folder"/></svg>Open folder</button></div>
      <div class="stats"><div><span>Players</span><b id="stat-players">0</b></div><div><span>Memory</span><b id="stat-ram">-</b></div><div><span>Version</span><b id="stat-ver">-</b></div></div>
    </div>
    <div class="glass join" data-shine>
      <div><h3 class="sec-title">How to join</h3><p class="sec-sub" style="margin:0">In Minecraft: Multiplayer, then Add Server, then paste the address.</p></div>
      <div class="addr"><span class="ai"><svg><use href="#i-monitor"/></svg></span><div class="t"><small>On this computer</small><code id="a-local">localhost</code></div><button class="btn sm" data-copy="a-local"><svg><use href="#i-copy"/></svg>Copy</button></div>
      <div class="addr"><span class="ai"><svg><use href="#i-wifi"/></svg></span><div class="t"><small>Friends on the same Wi-Fi</small><code id="a-lan">-</code></div><button class="btn sm" data-copy="a-lan"><svg><use href="#i-copy"/></svg>Copy</button></div>
      <div class="addr"><span class="ai"><svg><use href="#i-globe"/></svg></span><div class="t"><small>Friends anywhere else</small><code id="a-pub">Finding your address…</code></div><button class="btn sm" data-copy="a-pub"><svg><use href="#i-copy"/></svg>Copy</button></div>
      <div class="reach" id="reach"></div>
      <details id="help-net"><summary>Friends far away can’t connect?<svg><use href="#i-down"/></svg></summary><ol>
        <li>Your router has to let them in. The easiest way is a free tunnel like <a href="https://playit.gg" target="_blank" rel="noopener noreferrer" style="color:var(--t-acc)">playit.gg</a>, which needs no router setup.</li>
        <li>Or open your router’s settings page and “forward” port <b id="h-port">25565</b> (TCP) to this computer (<b id="h-lan">this PC</b>). Search your router’s brand plus “port forwarding” for a guide.</li>
        <li>If Windows asks whether Java can use the network, click <b>Allow</b>.</li>
        <li id="h-mods">Friends need the same modpack installed in their launcher (for example the CurseForge app or Modrinth), in the same version.</li></ol></details>
    </div>
  </div>
  <div class="tabs" role="tablist" id="tabs">
    <button role="tab" data-tab="console" aria-selected="true"><svg width="15" height="15"><use href="#i-terminal"/></svg>Console</button>
    <button role="tab" data-tab="players" aria-selected="false"><svg width="15" height="15"><use href="#i-users"/></svg>Players</button>
    <button role="tab" data-tab="settings" aria-selected="false"><svg width="15" height="15"><use href="#i-sliders"/></svg>Settings</button>
    <button role="tab" data-tab="problems" aria-selected="false"><svg width="15" height="15"><use href="#i-heart"/></svg>Health<span class="badge" id="prob-badge" hidden>!</span></button>
  </div>
  <div class="glass tabpanel" id="t-console" data-shine>
    <div class="console" id="console" role="log"><div class="empty-c" id="console-empty">The server’s messages appear here while it runs.</div></div>
    <form class="cmd" id="cmd-form"><input type="text" id="cmd" placeholder="Type a command, like: say Hello everyone" autocomplete="off" spellcheck="false" aria-label="Server command"><button class="btn" type="submit">Send</button></form>
    <div class="quick"><button class="btn sm" data-q="admin">Make someone an admin</button><button class="btn sm" data-q="time set day">Make it day</button><button class="btn sm" data-q="weather clear">Clear the weather</button><button class="btn sm" data-q="save-all">Save the world now</button><button class="btn sm" data-q="say">Send a message</button></div>
  </div>
  <div class="glass tabpanel" id="t-players" hidden data-shine><div class="players" id="players"></div></div>
  <div class="glass tabpanel" id="t-settings" hidden data-shine>
    <p class="sec-sub" style="margin-top:0">Changes take effect the next time the server starts.</p>
    <div class="setlist" id="setlist"></div>
    <div class="pfoot"><button class="btn quiet sm" id="srv-remove">Remove from my list</button><div style="display:flex;gap:10px"><button class="btn sm" id="srv-rename">Rename</button><button class="btn primary" id="set-save">Save changes</button></div></div>
  </div>
  <div class="glass tabpanel" id="t-problems" hidden data-shine>
    <div id="health"></div>
    <h3 class="sec-title" style="margin-top:22px">Check a log file</h3>
    <p class="sec-sub">If someone sent you a crash log, or the server misbehaves, give the app the log and it looks for the cause.</p>
    <button class="btn" id="srv-checklog"><svg><use href="#i-file"/></svg>Choose a log file</button>
    <h3 class="sec-title" style="margin-top:22px" id="rm-title">Mods that were taken out</h3>
    <p class="sec-sub">These mods only work on players’ computers, or caused problems, so they were moved out of the server. Put one back if you need it.</p>
    <div id="rm-list"></div>
  </div>
</section>

<!-- ======================= app settings ======================= -->
<section class="view" id="v-settings" hidden>
  <button class="back" id="set-back"><svg><use href="#i-back"/></svg>Back</button>
  <h1>Settings</h1>
  <div class="glass panel" style="margin-top:24px" data-shine>
    <div class="setlist">
      <div class="set"><div><b>Look</b><span>Automatic follows the time of day where you are.</span></div><div class="ctl"><div class="seg" id="theme-seg"><button data-v="auto">Automatic</button><button data-v="light">Light</button><button data-v="dark">Dark</button></div></div></div>
      <div class="set" id="tb-row"><div><b>Window frame</b><span>Use the app’s own glass title bar, or the standard Windows one. Takes effect next time the app opens.</span></div><div class="ctl"><div class="seg" id="tb-seg"><button data-v="custom">App</button><button data-v="standard">Windows</button></div></div></div>
      <div class="set"><div><b>Updates</b><span id="upd-text">Checking…</span></div><div class="ctl"><button class="btn sm" id="upd-check">Check now</button></div></div>
      <div class="set"><div><b>Stronger repairs</b><span>When a crash has no clear cause, also take out mods that are only suspected. Usually not needed.</span></div><div class="ctl"><span class="sw"><input type="checkbox" id="aggr" aria-label="Stronger repairs"><span></span></span></div></div>
      <div class="set"><div><b>CurseForge key</b><span>Optional. Helps when some CurseForge modpack files won’t download.</span></div><div class="ctl"><input type="password" id="cfkey" placeholder="Paste key" autocomplete="off"></div></div>
      <div class="set"><div><b>About</b><span id="about-text"></span></div><div class="ctl"><button class="btn sm quiet" id="whats-new">What’s new</button></div></div>
    </div>
  </div>
</section>

<!-- ======================= installer ======================= -->
<section class="view" id="v-install" hidden>
  <h1>Install Modpack Server Builder</h1>
  <p class="lede">Adds it to your Start menu so it opens like any other app. It installs for your Windows account only, so no administrator password is needed.</p>
  <div class="glass panel form" style="margin-top:26px" data-shine>
    <div class="kv"><div class="t"><b id="inst-title">Ready to install</b><span id="inst-sub"></span></div><svg width="44" height="44"><use href="#i-block"/></svg></div>
    <div class="kv"><div class="t"><b>Desktop shortcut</b><span>Also put a shortcut on your desktop.</span></div><span class="sw"><input type="checkbox" id="i-desktop" checked aria-label="Desktop shortcut"><span></span></span></div>
    <div class="kv"><div class="t"><b>Location</b><span id="inst-dir"></span></div></div>
  </div>
  <div class="wnav" style="margin-top:18px"><button class="btn quiet" id="skip-install">Use it without installing</button><button class="btn go big" id="go-install">Install</button></div>
</section>

</div>
</main>

<div class="veil" id="veil" hidden><div class="glass sheet" role="dialog" aria-modal="true" aria-labelledby="m-title">
  <header><h2 id="m-title"></h2><button class="tbtn" id="m-x" aria-label="Close"><svg><use href="#i-x"/></svg></button></header>
  <div class="body" id="m-body"></div>
  <footer id="m-foot"></footer>
</div></div>
<div class="toast" id="toast" role="status"></div>

<script>
(function(){
'use strict';
var $=function(s,r){return (r||document).querySelector(s)},$$=function(s,r){return Array.prototype.slice.call((r||document).querySelectorAll(s))};
var TOKEN=new URLSearchParams(location.search).get('t')||'';
var S={info:{},servers:[],view:'',sid:null,src:null,ram:4,sysgb:8,dir:'',dirTouched:false,sep:'/',home:'',page:0,q:'',jobKind:'build',pollT:null};
var root=document.documentElement;

/* ---------- helpers ---------- */
function api(path,json){var o={headers:{'X-Token':TOKEN}};if(json!==undefined){o.method='POST';o.headers['Content-Type']='application/json';o.body=JSON.stringify(json)}
  return fetch(path,o).then(function(r){return r.json().catch(function(){return {}}).then(function(d){if(!r.ok){var e=new Error(d.error||('Something went wrong ('+r.status+')'));e.data=d;e.status=r.status;throw e}return d})})}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function icon(id){var s=document.createElementNS('http://www.w3.org/2000/svg','svg'),u=document.createElementNS('http://www.w3.org/2000/svg','use');u.setAttribute('href','#i-'+id);s.appendChild(u);return s}
var toastT;function toast(m,bad){var t=$('#toast');t.textContent=m;t.className='toast show'+(bad?' bad':'');clearTimeout(toastT);toastT=setTimeout(function(){t.className='toast'},3800)}
function fmtNum(n){return n>=1e6?(n/1e6).toFixed(1).replace(/\.0$/,'')+'M':n>=1e3?Math.round(n/1e3)+'K':String(n)}
function fmtSize(n){return n>1048576?(n/1048576).toFixed(1)+' MB':Math.max(1,Math.round(n/1024))+' KB'}
function baseName(p){return String(p||'').split(/[\\/]/).pop()}
function isAbs(p){return /^([A-Za-z]:[\\/]|\\\\|\/|~)/.test(p||'')}
function parentDir(p){var m=(p||'').match(/^(.*?[\\/])[^\\/]+[\\/]?$/);if(!m)return '';var r=m[1];return /^([A-Za-z]:\\|\\\\[^\\]+\\[^\\]+\\|\/)$/.test(r)||/^[A-Za-z]:[\\/]$/.test(r)?r:r.replace(/[\\/]+$/,'')}
function joinPath(b,n){if(!b)return n;return b.charAt(b.length-1)===S.sep?b+n:b+S.sep+n}
function prettyDir(p){if(!p)return '';var h=S.home;if(h&&p.indexOf(h)===0)p=p.slice(h.length).replace(/^[\\/]/,'');return p.split(/[\\/]/).filter(Boolean).join('  \u203a  ')}
function loaderName(l){return ({forge:'Forge',neoforge:'NeoForge',fabric:'Fabric',quilt:'Quilt',vanilla:'Plain Minecraft'})[l]||l||''}
function packIcon(box,url){box.textContent='';if(url){var i=new Image();i.alt='';i.referrerPolicy='no-referrer';i.src=url;i.onerror=function(){box.textContent='';box.appendChild(icon('block'))};box.appendChild(i)}else box.appendChild(icon('block'))}

/* ---------- the world: time-of-day sky + blocky hills ---------- */
function skyNow(){var h=new Date().getHours();return h>=5&&h<8?'dawn':h>=8&&h<17?'day':h>=17&&h<20?'dusk':'night'}
function rng(seed){return function(){seed=(seed*1103515245+12345)&0x7fffffff;return seed/0x7fffffff}}
function hills(r,w,base,amp,block){var y=base,d='M0 400 L0 '+y;for(var x=0;x<w;x+=block){var k=r();if(k<.32)y-=block;else if(k>.68)y+=block;y=Math.max(base-amp,Math.min(base+amp,y));d+=' L'+x+' '+y+' L'+(x+block)+' '+y}return d+' L'+w+' 400 Z'}
function buildScene(){
  var sc=$('#scene');sc.textContent='';var sky=root.dataset.sky,r=rng(20251015+(sky==='night'?7:0));
  var hrs=new Date().getHours()+new Date().getMinutes()/60,sun=el('div','sun');
  var real=skyNow(),pos=sky!==real?.72:sky==='night'?((hrs+24-20)%24)/9:(hrs-5)/15;pos=Math.max(.04,Math.min(.96,pos));
  sun.style.left='calc('+(6+pos*84)+'% - 37px)';sun.style.top=(7+Math.pow(pos-.5,2)*56)+'%';sc.appendChild(sun);
  if(sky==='night'||sky==='dusk'){for(var i=0;i<(sky==='night'?90:25);i++){var s=el('i','star');s.style.left=(r()*100)+'%';s.style.top=(r()*55)+'%';s.style.opacity=(.3+r()*.6).toFixed(2);if(r()>.85){s.style.width='4px';s.style.height='4px'}sc.appendChild(s)}}
  for(var c=0;c<(sky==='night'?0:sky==='dusk'?3:6);c++){var cl=el('i','cloud');cl.style.left=(r()*92)+'%';cl.style.top=(8+r()*30)+'%';cl.style.width=(90+r()*170|0)+'px';sc.appendChild(cl)}
  var ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 1600 400');svg.setAttribute('preserveAspectRatio','xMidYMax slice');
  [['var(--h1)',150,60,48],['var(--h2)',215,55,40],['var(--h3)',275,45,32],['var(--h4)',335,30,24]].forEach(function(L,i){
    var p=document.createElementNS(ns,'path');p.setAttribute('d',hills(r,1600,L[1],L[2],L[3]));p.setAttribute('fill',L[0]);svg.appendChild(p);
    if(i>=2){var g=document.createElementNS(ns,'path');g.setAttribute('d',hills(rng(99+i),1600,L[1]-4,L[2],L[3]));g.setAttribute('fill','none');g.setAttribute('stroke','var(--grass)');g.setAttribute('stroke-width','5');g.setAttribute('opacity','.55');svg.insertBefore(g,p.nextSibling)}});
  sc.appendChild(svg);
}
function themeMode(){try{return localStorage.getItem('msb-theme-mode')||'auto'}catch(e){return 'auto'}}
function applyTheme(mode,save){
  var sky=skyNow(),t=mode==='auto'?(sky==='day'||sky==='dawn'?'light':'dark'):mode;
  if(t==='dark'&&(sky==='day'||sky==='dawn'))sky='night';if(t==='light'&&(sky==='night'||sky==='dusk'))sky='day';
  var changedSky=root.dataset.sky!==sky;root.dataset.sky=sky;root.dataset.theme=t;
  try{localStorage.setItem('msb-theme-mode',mode)}catch(e){}
  $('#theme-color').setAttribute('content',t==='dark'?'#121a3d':'#dcecfb');
  $$('#theme-seg button').forEach(function(b){b.setAttribute('aria-pressed',b.dataset.v===mode)});
  if(changedSky||!$('#scene').firstChild)buildScene();
  if(save)api('/api/settings',{theme:mode==='auto'?'auto':t}).catch(function(){});
}
setInterval(function(){applyTheme(themeMode(),false)},5*60*1000);

/* ---------- glass shine (one card, transform only) ---------- */
var shC=null,shR=null,shXY=null,shRaf=0;
function shEl(c){var e=c.firstElementChild;if(!e||!e.classList.contains('shine')){e=el('i','shine');c.insertBefore(e,c.firstChild)}return e}
document.addEventListener('pointerover',function(e){var c=e.target.closest&&e.target.closest('[data-shine]');if(c===shC)return;if(shC)shC.classList.remove('lit');shC=c;shR=null;if(c){shEl(c);c.classList.add('lit')}});
document.addEventListener('pointermove',function(e){if(!shC)return;shXY=[e.clientX,e.clientY];if(!shRaf)shRaf=requestAnimationFrame(function(){shRaf=0;if(!shC||!shXY)return;if(!shR)shR=shC.getBoundingClientRect();shEl(shC).style.transform='translate3d('+(shXY[0]-shR.left)+'px,'+(shXY[1]-shR.top)+'px,0)'})},{passive:true});
$('#main').addEventListener('scroll',function(){shR=null},{passive:true});window.addEventListener('resize',function(){shR=null});

/* ---------- window frame (native app window) ---------- */
function win(cmd){try{if(window.msbWindow)return window.msbWindow(cmd)}catch(e){}return Promise.resolve(null)}
function setupFrame(){
  if(!S.info.frameless)return;
  $('#wc').hidden=false;$('#rz').hidden=false;
  $('#drag').addEventListener('mousedown',function(e){if(e.button!==0||e.target.closest('button,a,input'))return;e.preventDefault();win('drag')});
  $('#drag').addEventListener('dblclick',function(e){if(e.target.closest('button,a,input'))return;win('max').then(paintMax)});
  $$('#wc button').forEach(function(b){b.addEventListener('click',function(){win(b.dataset.wc).then(paintMax)})});
  $$('#rz i').forEach(function(z){z.addEventListener('mousedown',function(e){if(e.button!==0)return;e.preventDefault();win('resize:'+z.dataset.rz)})});
  window.addEventListener('resize',function(){clearTimeout(S.maxT);S.maxT=setTimeout(paintMax,120)});paintMax();
}
function paintMax(){win('state').then(function(st){var m=st&&st.max;$('#max-ic').setAttribute('href',m?'#i-unmax':'#i-max');$('#rz').hidden=!!m||!S.info.frameless;$$('#wc button')[1].setAttribute('aria-label',m?'Restore':'Maximize')})}

/* ---------- modal ---------- */
var modalPrev=null,modalClose=null;
function modal(title,body,buttons,onClose){
  modalPrev=document.activeElement;modalClose=onClose||null;$('#m-title').textContent=title;var b=$('#m-body');b.textContent='';
  if(typeof body==='string'){b.appendChild(el('p',null,body))}else if(body)b.appendChild(body);
  var f=$('#m-foot');f.textContent='';(buttons||[{t:'OK',primary:true}]).forEach(function(x){var bt=el('button','btn'+(x.primary?' primary':x.danger?' stop':' quiet'),x.t);bt.onclick=function(){if(x.fn&&x.fn()===false)return;closeModal()};f.appendChild(bt)});
  $('#veil').hidden=false;var first=f.querySelector('.primary,.stop')||$('#m-x');setTimeout(function(){first.focus()},30);
}
function closeModal(){if($('#veil').hidden)return;$('#veil').hidden=true;pk=null;var c=modalClose;modalClose=null;if(c)c();if(modalPrev&&modalPrev.focus)modalPrev.focus()}
$('#m-x').onclick=closeModal;$('#veil').addEventListener('mousedown',function(e){if(e.target.id==='veil')closeModal()});
document.addEventListener('keydown',function(e){if($('#veil').hidden)return;if(e.key==='Escape'){closeModal();return}
  if(e.key==='Tab'){var f=$$('.sheet button:not([disabled]),.sheet input,.sheet select,.sheet a').filter(function(x){return x.offsetParent!==null});if(!f.length)return;var i=f.indexOf(document.activeElement);if(e.shiftKey&&i<=0){e.preventDefault();f[f.length-1].focus()}else if(!e.shiftKey&&i===f.length-1){e.preventDefault();f[0].focus()}}});
function ask(title,text,okText,danger){return new Promise(function(res){var done=false;modal(title,text,[{t:'Cancel',fn:function(){done=true;res(false)}},{t:okText,primary:!danger,danger:danger,fn:function(){done=true;res(true)}}],function(){if(!done)res(false)})})}
function prompt2(title,text,ph,okText){return new Promise(function(res){var box=el('div'),p=el('p',null,text),inp=el('input');inp.type='text';inp.placeholder=ph||'';inp.maxLength=60;box.appendChild(p);box.appendChild(inp);var done=false;
  modal(title,box,[{t:'Cancel',fn:function(){done=true;res(null)}},{t:okText,primary:true,fn:function(){var v=inp.value.trim();if(!v){inp.focus();return false}done=true;res(v)}}],function(){if(!done)res(null)});
  inp.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();$('#m-foot .primary').click()}});setTimeout(function(){inp.focus()},40)})}

/* ---------- file/folder picker ---------- */
var pk=null,pkSeq=0;
function pick(opt){return new Promise(function(resolve){
  var box=el('div');box.innerHTML='';var go=el('form','search');var gi=el('input');gi.type='text';gi.placeholder='Type a folder path and press Enter';gi.setAttribute('aria-label','Go to path');go.appendChild(gi);box.appendChild(go);
  var cr=el('nav','crumbs');var list=el('div','list');box.appendChild(cr);box.appendChild(list);
  var done=false,cur='';var foot=[{t:'Cancel'}];if(opt.kind==='dir')foot.push({t:'Use this folder',primary:true,fn:function(){if(!cur)return false;done=true;resolve(cur)}});
  modal(opt.title,box,foot,function(){if(!done)resolve(null)});pk={};
  go.onsubmit=function(e){e.preventDefault();if(gi.value.trim())load(gi.value.trim())};
  function load(path){var seq=++pkSeq;list.setAttribute('aria-busy','true');
    api('/api/fs?path='+encodeURIComponent(path)+(opt.kind==='file'?'&files=1&exts='+opt.exts.join(','):'')).then(function(d){
      if(seq!==pkSeq||!pk)return;list.removeAttribute('aria-busy');cur=d.path;cr.textContent='';
      var hb=el('button',null,'Home');hb.type='button';hb.onclick=function(){load(S.home)};cr.appendChild(hb);
      if(S.info.win){var pc=el('button',null,'This PC');pc.type='button';pc.onclick=function(){load('')};cr.appendChild(el('span',null,'\u203a'));cr.appendChild(pc)}
      var cs=d.crumbs.length>4?[d.crumbs[0],null].concat(d.crumbs.slice(-2)):d.crumbs;
      cs.forEach(function(c){cr.appendChild(el('span',null,'\u203a'));if(!c){cr.appendChild(el('span',null,'\u2026'));return}var b=el('button',null,c.name);b.type='button';b.onclick=function(){load(c.path)};cr.appendChild(b)});
      list.textContent='';
      if(d.parent!==null&&d.parent!==undefined){var up=el('button');up.type='button';up.appendChild(icon('back'));up.appendChild(el('span',null,'Up one level'));up.onclick=function(){load(d.parent)};list.appendChild(up)}
      d.dirs.forEach(function(n){var b=el('button');b.type='button';b.title=n;b.appendChild(icon('folder'));b.appendChild(el('span',null,n));b.onclick=function(){load(joinPath(d.path,n))};list.appendChild(b)});
      (d.files||[]).forEach(function(n){var b=el('button');b.type='button';b.title=n;b.appendChild(icon('file'));b.appendChild(el('span',null,n));b.onclick=function(){done=true;closeModal();resolve(joinPath(d.path,n))};list.appendChild(b)});
      if(!d.dirs.length&&!(d.files||[]).length)list.appendChild(el('p','muted',d.error?'This folder can\u2019t be opened.':(opt.kind==='file'?'No matching files here.':'No folders here.')));
      var f=list.querySelector('button');if(f)f.focus();
    }).catch(function(e){if(seq!==pkSeq)return;list.removeAttribute('aria-busy');toast(e.message,true)})}
  load(opt.start||S.home);
})}

/* ---------- uploads ---------- */
function upload(file,kind){return new Promise(function(res,rej){var x=new XMLHttpRequest();x.open('POST','/api/upload?kind='+kind+'&name='+encodeURIComponent(file.name));x.setRequestHeader('X-Token',TOKEN);
  x.onload=function(){var d={};try{d=JSON.parse(x.responseText)}catch(e){}x.status<300?res(d):rej(new Error(d.error||'Upload failed'))};x.onerror=function(){rej(new Error('Upload failed'))};x.send(file)})}
document.addEventListener('dragover',function(e){e.preventDefault()});document.addEventListener('drop',function(e){e.preventDefault()});

/* ---------- navigation ---------- */
var VIEWS=['home','create','build','server','settings','install'];
function go(v,arg){
  if(S.view==='server'&&v!=='server')stopServerPolling();
  S.view=v;VIEWS.forEach(function(x){$('#v-'+x).hidden=x!==v});$('#main').scrollTop=0;
  var cr=$('#crumb');cr.textContent='';
  function crumb(t){cr.appendChild(icon('chev'));cr.appendChild(el('span',null,t))}
  if(v==='home'){loadHome()}
  else if(v==='create'){crumb('New server');startWizard()}
  else if(v==='build'){crumb(S.jobKind==='fix'?'Fixing':'Setting up')}
  else if(v==='server'){S.sid=arg;var s=S.servers.filter(function(x){return x.id===arg})[0];crumb(s?s.name:'Server');openServer(arg)}
  else if(v==='settings'){crumb('Settings');loadSettings()}
  paintJobPill();paintRunPill();
  var h=$('#v-'+v+' h1, #v-'+v+' h2');if(h&&v!=='home'){h.setAttribute('tabindex','-1');h.focus({preventScroll:true})}
}
$('#go-home').onclick=function(){if(S.view!=='install')go('home')};$('#go-settings').onclick=function(){go('settings')};
$('#create-back').onclick=function(){go('home')};$('#srv-back').onclick=function(){go('home')};$('#set-back').onclick=function(){go(S.sid?'server':'home',S.sid)};
document.addEventListener('contextmenu',function(e){if(!(e.target.closest&&e.target.closest('input,textarea,.feed,.console,code')))e.preventDefault()});

/* ---------- home ---------- */
function stateText(s){var n=(s.players||[]).length;return({online:n?'Online, '+n+' playing':'Online',starting:'Starting\u2026',stopping:'Stopping\u2026',crashed:'Stopped with a problem',setting_up:'Setting up\u2026'})[s.state]||'Offline'}
function loadHome(){return api('/api/servers').then(function(d){S.servers=d.servers;renderHome();paintRunPill()}).catch(function(e){toast(e.message,true)})}
function renderHome(){
  var box=$('#servers');box.textContent='';var has=S.servers.length>0;
  $('#home-empty').hidden=has;$('#home-new').hidden=!has;$('#home-title').hidden=!has;$('#home-lede').hidden=!has;
  S.servers.forEach(function(s){
    var c=el('button','glass scard'+(s.missing?' missing':''));c.dataset.shine='';
    var top=el('div','top'),ic=el('div','picon');packIcon(ic,s.icon);top.appendChild(ic);var nm=el('div');nm.style.minWidth='0';nm.appendChild(el('h3',null,s.name));top.appendChild(nm);c.appendChild(top);
    var chips=el('div','chips');if(s.mc)chips.appendChild(el('span','chip pix',s.mc));if(s.loader)chips.appendChild(el('span','chip',loaderName(s.loader)));if(s.pack&&s.pack!==s.name)chips.appendChild(el('span','chip',s.pack));c.appendChild(chips);
    var f=el('div','foot'),st=el('span','state'),d=el('i','dot '+(s.missing?'':s.state));st.appendChild(d);st.appendChild(document.createTextNode(s.missing?'Folder not found':stateText(s)));f.appendChild(st);f.appendChild(icon('chev'));f.lastChild.setAttribute('width','16');f.lastChild.setAttribute('height','16');c.appendChild(f);
    c.onclick=function(){if(s.missing){ask('Folder not found','The folder for \u201c'+s.name+'\u201d can\u2019t be found. It may have been moved or deleted. Remove it from your list?','Remove',true).then(function(ok){if(ok)api('/api/servers/remove',{id:s.id}).then(loadHome)});return}if(s.state==='setting_up'){go('build');return}go('server',s.id)};
    box.appendChild(c)});
  if(has){var n=el('button','scard new');var p=el('span','plus');p.appendChild(icon('plus'));n.appendChild(p);n.appendChild(el('b',null,'Create a new server'));var im=el('span','muted','or add one you already have');n.appendChild(im);
    n.onclick=function(e){go('create')};im.onclick=function(e){e.stopPropagation();importServer()};box.appendChild(n)}
}
$('#home-new').onclick=$('#empty-new').onclick=function(){go('create')};$('#empty-import').onclick=importServer;
function importServer(){pick({kind:'dir',title:'Choose your server folder'}).then(function(p){if(!p)return;api('/api/servers/import',{dir:p}).then(function(s){toast('Added \u201c'+s.name+'\u201d');loadHome().then(function(){go('server',s.id)})}).catch(function(e){toast(e.message,true)})})}
function paintRunPill(){var r=S.servers.filter(function(s){return s.state&&s.state!=='offline'&&s.state!=='crashed'&&s.state!=='setting_up'})[0];var p=$('#run-pill');p.hidden=!r||(S.view==='server'&&S.sid===r.id);if(r){p.lastElementChild.textContent=r.name+': '+stateText(r);p.onclick=function(){go('server',r.id)}}}
setInterval(function(){if(S.view==='home'||S.view==='settings'||S.view==='create')api('/api/servers').then(function(d){S.servers=d.servers;if(S.view==='home')renderHome();paintRunPill()}).catch(function(){})},4000);

/* ---------- create wizard ---------- */
function setStep(n){[1,2,3].forEach(function(i){$('#w'+i).hidden=i!==n;var s=$('#wsteps [data-s="'+i+'"]');s.className='s'+(i===n?' on':i<n?' done':'')});
  var f=$('#w'+n+' input,#w'+n+' button');}
function startWizard(){S.src=null;S.dirTouched=false;$$('.choice').forEach(function(c){c.setAttribute('aria-pressed','false')});['modrinth','file','vanilla'].forEach(function(k){$('#src-'+k).hidden=true});
  $('#w-eula').checked=!!(S.info.settings&&S.info.settings.eula);$('#file-picked').hidden=true;$('#drop').hidden=false;setStep(1);validateW()}
$$('.choice').forEach(function(c){c.onclick=function(){$$('.choice').forEach(function(x){x.setAttribute('aria-pressed',x===c)});var k=c.dataset.src;['modrinth','file','vanilla'].forEach(function(x){$('#src-'+x).hidden=x!==k});
  S.src=null;$$('.pack').forEach(function(p){p.setAttribute('aria-pressed','false')});
  if(k==='modrinth'){if(!$('#packs').children.length)search('',0);setTimeout(function(){$('#q').focus()},30)}
  if(k==='vanilla')loadVanilla();if(k==='file'&&S.fileSrc)S.src=S.fileSrc;validateW()}});
var qT=null;$('#q').addEventListener('input',function(){clearTimeout(qT);qT=setTimeout(function(){search($('#q').value,0)},380)});
function search(q,offset){S.q=q;S.page=offset;var box=$('#packs');if(!offset){box.textContent='';box.setAttribute('aria-busy','true')}$('#q-note').textContent=q?'Searching\u2026':'Popular modpacks';
  api('/api/modpacks?q='+encodeURIComponent(q)+'&offset='+offset).then(function(d){if(S.q!==q)return;box.removeAttribute('aria-busy');
    $('#q-note').textContent=q?(d.total?fmtNum(d.total)+' modpack'+(d.total===1?'':'s')+' found':'No modpacks match \u201c'+q+'\u201d. Try a shorter name.'):'Popular modpacks';
    d.hits.forEach(function(h){var b=el('button','pack');b.setAttribute('aria-pressed','false');var ic=el('div','picon');packIcon(ic,h.icon);b.appendChild(ic);
      var t=el('div');t.style.minWidth='0';t.appendChild(el('b',null,h.title));t.appendChild(el('p',null,h.description));var m=el('div','meta');if(h.mc)m.appendChild(el('span','chip pix',h.mc));h.loaders.slice(0,2).forEach(function(l){m.appendChild(el('span','chip',l))});m.appendChild(el('span','chip',fmtNum(h.downloads)+' downloads'));t.appendChild(m);b.appendChild(t);
      b.onclick=function(){$$('.pack').forEach(function(p){p.setAttribute('aria-pressed',p===b)});S.src={type:'modrinth',id:h.id,title:h.title,icon:h.icon,mc:h.mc,loader:h.loaders[0]||''};validateW()};box.appendChild(b)});
    $('#q-more').hidden=offset+d.hits.length>=d.total||!d.hits.length;
  }).catch(function(e){box.removeAttribute('aria-busy');$('#q-note').textContent='Couldn\u2019t reach the modpack library. Check your internet connection, then try again.'})}
$('#q-more').onclick=function(){search(S.q,S.page+18)};
function loadVanilla(){var sel=$('#v-ver');if(sel.options.length){S.src={type:'vanilla',version:sel.value};validateW();return}
  api('/api/vanilla').then(function(d){sel.textContent='';d.versions.forEach(function(v,i){var o=el('option',null,i===0?v+' (newest)':v);o.value=v;sel.appendChild(o)});S.src={type:'vanilla',version:sel.value};validateW()}).catch(function(){toast('Couldn\u2019t load Minecraft versions. Check your internet connection.',true)})}
$('#v-ver').onchange=function(){S.src={type:'vanilla',version:this.value};validateW()};
function setFile(path){api('/api/inspect',{path:path}).then(function(d){S.fileSrc=S.src={type:'file',path:path,title:d.name,mc:d.mc,loader:d.loader,mods:d.mods};
  $('#drop').hidden=true;$('#file-picked').hidden=false;$('#fp-name').textContent=d.name;$('#fp-meta').textContent='Minecraft '+d.mc+', '+loaderName(d.loader)+', '+d.mods+' mods';validateW()}).catch(function(e){toast(e.message,true)})}
var drop=$('#drop');['dragenter','dragover'].forEach(function(t){drop.addEventListener(t,function(e){e.preventDefault();drop.classList.add('over')})});['dragleave','drop'].forEach(function(t){drop.addEventListener(t,function(e){e.preventDefault();drop.classList.remove('over')})});
drop.addEventListener('drop',function(e){var f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];if(!f)return;if(!/\.(zip|mrpack)$/i.test(f.name)){toast('Use a .zip or .mrpack modpack file.',true);return}upload(f,'pack').then(function(d){setFile(d.path)}).catch(function(e){toast(e.message,true)})});
$('#pick-file').onclick=$('#fp-change').onclick=function(){pick({kind:'file',exts:['zip','mrpack'],title:'Choose your modpack file'}).then(function(p){if(p)setFile(p)})};
function validateW(){$('#w1-next').disabled=!S.src;var ok=$('#w-eula').checked;$('#w-create').disabled=!ok;$('#w3-hint').hidden=ok}
$('#w-eula').onchange=validateW;
$('#w1-next').onclick=function(){if(!S.src)return;var name=S.src.type==='vanilla'?'Minecraft '+S.src.version:S.src.title||'My server';$('#w-name').value=name;
  api('/api/defaults?name='+encodeURIComponent(name)+'&mods='+(S.src.type==='vanilla'?0:(S.src.mods||120))).then(function(d){S.sysgb=Math.round(d.system_mb/1024);S.ram=Math.round(d.ram_mb/1024);if(!S.dirTouched)S.dir=d.dir;paintRam();paintDir()}).catch(function(){});setStep(2);$('#w-name').focus()};
$('#w-name').addEventListener('change',function(){if(S.dirTouched)return;api('/api/defaults?name='+encodeURIComponent($('#w-name').value||'My server')).then(function(d){S.dir=d.dir;paintDir()}).catch(function(){})});
function paintRam(){var r=$('#w-ram');r.max=Math.max(4,Math.floor(S.sysgb*.8));r.value=S.ram;r.style.setProperty('--p',((S.ram-2)/(r.max-2)*100)+'%');$('#w-ram-val').textContent=S.ram+' GB';
  $('#w-ram-text').textContent='The server will use up to '+S.ram+' GB of your computer\u2019s '+S.sysgb+' GB.'+(S.src&&S.src.type!=='vanilla'?' That suits this modpack.':'')}
$('#w-ram').oninput=function(){S.ram=+this.value;paintRam()};$('#w-ram-change').onclick=function(){$('#w-ram-box').hidden=!$('#w-ram-box').hidden};
function paintDir(){$('#w-dir-text').textContent=prettyDir(S.dir)}
$('#w-dir-change').onclick=function(){pick({kind:'dir',title:'Choose where to save the server',start:parentDir(S.dir)}).then(function(p){if(p){S.dir=joinPath(p,$('#w-name').value.trim()||'My server');S.dirTouched=true;paintDir()}})};
$('#w2-back').onclick=function(){setStep(1)};$('#w3-back').onclick=function(){setStep(2)};
$('#w2-next').onclick=function(){if(!$('#w-name').value.trim()){$('#w-name').focus();return}var sm=$('#w-summary');sm.textContent='';var t=el('div','t');t.appendChild(el('b',null,$('#w-name').value.trim()));
  t.appendChild(el('span',null,(S.src.type==='vanilla'?'Minecraft '+S.src.version:S.src.title+(S.src.mc?', Minecraft '+S.src.mc:''))+', '+S.ram+' GB memory'));sm.appendChild(t);setStep(3);validateW()};
$('#w-create').onclick=function(){var b=this;if(!$('#w-eula').checked)return;b.disabled=true;
  api('/api/create',{source:S.src,name:$('#w-name').value.trim(),dir:S.dir,ram_mb:S.ram*1024,eula:true,test:$('#w-test').checked,aggressive:!!(S.info.settings&&S.info.settings.aggressive),cfkey:(S.info.settings&&S.info.settings.cfkey)||''})
    .then(function(){S.jobName=$('#w-name').value.trim();S.jobSid=null;api('/api/settings',{eula:true}).catch(function(){});S.info.settings=S.info.settings||{};S.info.settings.eula=true;startProgress('build',S.src.type)}).catch(function(e){b.disabled=false;toast(e.message,true)})};

/* ---------- progress (create / fix jobs) ---------- */
var STAGES={build:[['read','Get the game files'],['mods','Download mods'],['detect','Take out client-only mods'],['loader','Set up the server'],['test','First start']],
  vanilla:[['read','Get ready'],['loader','Download Minecraft'],['test','First start']],
  fix:[['read','Read the log'],['diagnose','Find the cause'],['repair','Fix it'],['test','Start the server']]};
var cursor=0,polling=false,fails=0,todos=[],todoMode=false,stageKey='build',jobPct=0,jobStage='',doneJob=null;
function jobName(){var s=S.servers.filter(function(x){return x.id===S.jobSid})[0];return s?s.name:(S.jobName||'')}
function paintJobPill(){var p=$('#job-pill');if(!p)return;p.className='running-pill job';
  if(polling){p.hidden=S.view==='build';var n=jobName();p.lastElementChild.textContent=(S.jobKind==='fix'?'Fixing':'Setting up')+(n?' '+n:'')+(jobPct?' \u00b7 '+jobPct+'%':'');p.title='Show progress';p.onclick=function(){go('build')};return}
  if(doneJob&&S.view!=='build'){var r=doneJob.result||{},bad=doneJob.state==='error'||r.ok===false;p.classList.add(bad?'bad':'ready');p.hidden=false;
    p.lastElementChild.textContent=r.cancelled?'Setup stopped':doneJob.state==='error'?'Setup didn\u2019t finish':bad?'Needs a fix':(S.jobKind==='fix'?'Fixed':'Server ready');p.title='Show the result';
    p.onclick=function(){var d=doneJob;doneJob=null;go('build');showResult(d)};return}
  p.hidden=true}
function startProgress(kind,srcType){S.jobKind=kind;stageKey=kind==='fix'?'fix':(srcType==='vanilla'?'vanilla':'build');cursor=0;todos=[];todoMode=false;jobPct=0;jobStage='';doneJob=null;$('#feed').textContent='';$('#b-skip').hidden=true;
  $('#b-result').hidden=true;$('.progress').hidden=false;$('#b-title').textContent=kind==='fix'?'Fixing your server':'Setting up your server';
  var tr=$('#b-track');tr.textContent='';STAGES[stageKey].forEach(function(s){var d=el('div','st');var dd=el('span','d');dd.appendChild(icon('check'));d.appendChild(dd);d.appendChild(el('span',null,s[1]));tr.appendChild(d)});
  var st=$('#b-stop');st.disabled=false;st.lastElementChild.textContent='Stop';paintStage('',null,'running');go('build');if(!polling){polling=true;fails=0;pollJob()}}
var NOW={read:{build:'Reading the modpack',vanilla:'Getting ready',fix:'Reading the log'},mods:'Downloading mods',detect:'Taking out mods that only work on players\u2019 computers',loader:'Setting up the server software',test:'Starting the server once to check that it works',diagnose:'Working out what went wrong',repair:'Fixing the problem'};
function paintStage(stage,dl,state){var ids=STAGES[stageKey].map(function(x){return x[0]}),i=ids.indexOf(stage);
  $$('#b-track .st').forEach(function(n,k){n.className='st'+(state==='done'?' done':state==='error'&&k===Math.max(i,0)?' bad':k<i?' done':k===i?' on':'')});
  var frac=stage==='mods'&&dl&&dl[1]?dl[0]/dl[1]:0,p=state==='done'?1:(i<0?.03:(i+frac)/ids.length),bar=$('#b-bar');
  jobPct=Math.round(p*100);jobStage=stage;var sk=$('#b-skip');sk.hidden=!(state==='running'&&stage==='test'&&S.jobKind!=='fix');
  bar.classList.toggle('ind',state==='running'&&(stage==='test'||stage===''));bar.firstElementChild.style.setProperty('--p',Math.max(3,p*100)+'%');
  var n=NOW[stage];if(n&&typeof n==='object')n=n[stageKey]||n.build;$('#b-now').textContent=state==='running'?(n||'Getting started')+(stage==='mods'&&dl?' ('+dl[0]+' of '+dl[1]+')':''):''}
function feedLine(t){var m;if(!t.trim()||/^={5,}/.test(t)||/^\s+\d+\/\d+ done$/.test(t))return null;
  if(/^(Things you need to do yourself|I couldn.t fix the remaining problem)/.test(t)){todoMode=true;return el('div','ln h',t.replace(/:$/,''))}
  if(todoMode&&(m=t.match(/^\s+-\s+(.*)$/))){todos.push(m[1]);return null}if(todoMode&&!/^\s+-\s/.test(t))todoMode=false;
  if((m=t.match(/^\s*\[(high|medium|low)\]\s+(.*)$/)))return ev(m[1]==='high'?'t-bad':m[1]==='medium'?'t-warn':'t-info','alert',m[2]);
  if((m=t.match(/^\s*Removed (.+?): (.*)$/)))return ev('t-warn','x',m[1],m[2]);
  if(/^\s*(Installed missing dependency|Raised server RAM|Lowered server RAM|Set eula|Test launch succeeded|Switched to Java)/.test(t)||/The server started successfully/.test(t))return ev('t-ok','check',t.trim());
  if(/^STOPPED:/.test(t))return el('div','ln warn',t);if(/\bFAILED\b|^ERROR|ERROR:/.test(t)&&!/^\s+\|/.test(t))return el('div','ln bad',t);
  if(/^\s*(WARNING|NOTE)/.test(t))return el('div','ln warn',t);if(/^\s+\|/.test(t)||/^\s+\$ /.test(t)||/^\s+\.\.\./.test(t))return el('div','ln dim',t.trim());
  if(/^\S/.test(t))return el('div','ln h',t.replace(/\s*\.\.\.$/,''));return el('div','ln',t.trim())}
function ev(tone,ic,title,detail){var n=el('div','ev '+tone),b=el('span','b');b.appendChild(icon(ic));n.appendChild(b);var t=el('div','t');t.appendChild(document.createTextNode(title));if(detail)t.appendChild(el('small',null,detail));n.appendChild(t);return n}
function addFeed(lines){if(!lines.length)return;var feed=$('#feed'),frag=document.createDocumentFragment();lines.forEach(function(l){var n=feedLine(l);if(n)frag.appendChild(n)});
  var near=feed.scrollHeight-feed.scrollTop-feed.clientHeight<80;feed.appendChild(frag);var extra=feed.childElementCount-400;if(extra>0){var r=document.createRange();r.setStartBefore(feed.firstChild);r.setEndAfter(feed.children[extra-1]);r.deleteContents()}if(near)feed.scrollTop=feed.scrollHeight}
function pollJob(){api('/api/events?since='+cursor).then(function(d){fails=0;cursor=d.next;addFeed(d.lines);if(d.server_id)S.jobSid=d.server_id;
  if(d.state==='running'||d.state==='idle'){paintStage(d.stage,d.dl,'running');paintJobPill();setTimeout(pollJob,document.hidden?2000:450)}else{polling=false;finishJob(d)}})
  .catch(function(){if(++fails>8){polling=false;finishJob({state:'error',result:{error:'Lost contact with the app. Open it again to carry on.'}})}else setTimeout(pollJob,1200)})}
$('#b-leave').onclick=function(){go('home')};
$('#b-skip').onclick=function(){var b=this;ask('Skip the test start?','The server is already set up. Skipping only means the app won\u2019t check that it starts. You can press Start on its page whenever you like, and \u201cFix it for me\u201d helps if it doesn\u2019t.','Skip the test',false).then(function(ok){if(!ok)return;b.disabled=true;api('/api/skiptest',{}).then(function(){b.hidden=true;b.disabled=false}).catch(function(e){b.disabled=false;toast(e.message,true)})})};
$('#b-stop').onclick=function(){var b=this;ask('Stop setting up?','The app stops what it\u2019s doing. Anything already downloaded stays in the server folder.','Stop',true).then(function(ok){if(!ok)return;b.disabled=true;b.lastElementChild.textContent='Stopping\u2026';api('/api/cancel',{}).catch(function(){})})};
function finishJob(d){paintStage(d.stage,d.dl,d.state==='error'?'error':'done');$('#b-skip').hidden=true;loadHome();
  if(S.view!=='build'){doneJob=d;paintJobPill();var r=d.result||{};
    toast(r.cancelled?'Setup stopped.':d.state==='error'?'Setting up didn\u2019t finish. Click the button at the top for details.':r.ok===false?(jobName()||'Your server')+' needs a fix first. Click the button at the top.':(jobName()||'Your server')+' is ready!',d.state==='error'||r.ok===false);return}
  showResult(d)}
function showResult(d){var r=d.result||{};paintJobPill();
  if(d.state!=='error'&&r.server_id){loadHome().then(function(){S.justMade=r.ok!==false;go('server',r.server_id);if(r.ok===false)setTimeout(function(){selTab('problems')},300);
    toast(d.kind==='fix'?(r.ok?'Fixed. Your server starts again.':'Some problems still need you. See Health.'):(r.ok===false?'Your server was made, but it needs a fix first.':r.skipped?'Your server is ready. Press Start to play.':'Your server is ready!'),r.ok===false)});return}
  $('.progress').hidden=true;$('#b-result').hidden=false;
  var title,sub;if(r.cancelled){title='Stopped';sub='Nothing else will change. The server folder keeps what was done so far.'}else if(d.state==='error'){title='That didn\u2019t work';sub=r.error||'Something went wrong.'}else{title='Done';sub=''}
  $('#r-title').textContent=title;$('#r-sub').textContent=sub;var td=$('#r-todos');td.textContent='';todos.forEach(function(t){var f=el('div','finding t-warn');f.appendChild(icon('alert'));f.appendChild(el('div',null,t));td.appendChild(f)});
  var sid=r.server_id||(r.cancelled||d.state==='error'?S.jobSid:null),known=sid&&S.servers.some(function(x){return x.id===sid});
  $('#r-open').hidden=!known;$('#r-open').onclick=function(){go('server',sid)};$('#r-title').focus()}
$('#r-home').onclick=function(){go('home')};

/* ---------- server control room ---------- */
var srv=null,conSince=0,sPollT=null,conT=null,tab='console',lastState='';
function stopServerPolling(){clearTimeout(sPollT);clearTimeout(conT);sPollT=conT=null}
function openServer(id){stopServerPolling();srv=null;conSince=0;lastState='';$('#console').textContent='';var ce=el('div','empty-c','The server\u2019s messages appear here while it runs.');ce.id='console-empty';$('#console').appendChild(ce);
  $('#srv-crash').hidden=true;$('#srv-ready').hidden=!S.justMade;S.justMade=false;$('#a-pub').textContent='Finding your address\u2026';$('#reach').textContent='';selTab('console');
  refreshServer(true);api('/api/server/public?id='+id).then(function(d){$('#a-pub').textContent=d.address||'Not available right now'}).catch(function(){$('#a-pub').textContent='Not available right now'})}
function refreshServer(first){var id=S.sid;api('/api/server/status?id='+id).then(function(d){if(S.view!=='server'||S.sid!==id)return;srv=d;paintServer(d,first);
  sPollT=setTimeout(function(){refreshServer(false)},d.status.state==='starting'||d.status.state==='stopping'?1000:2500)}).catch(function(e){if(S.view!=='server')return;toast(e.message,true);go('home')})}
function paintServer(d,first){var s=d.server,st=d.status||{},state=st.state||'offline';
  if(first){$('#srv-name').textContent=s.name;packIcon($('#srv-icon'),s.icon);var ch=$('#srv-chips');ch.textContent='';if(s.mc)ch.appendChild(el('span','chip pix',s.mc));if(s.loader)ch.appendChild(el('span','chip',loaderName(s.loader)));if(s.pack&&s.pack!==s.name)ch.appendChild(el('span','chip',s.pack));
    $('#stat-ver').textContent=s.mc||'-';$('#h-mods').hidden=s.loader==='vanilla'||!s.loader;var cr=$('#crumb span');if(cr)cr.textContent=s.name}
  var j=d.join||{};$('#a-local').textContent=j.local||'localhost';$('#a-lan').textContent=j.lan||'Not connected to a network';$('#h-port').textContent=j.port||25565;$('#h-lan').textContent=j.lan?j.lan.split(':')[0]:'this PC';
  var orb=$('#orb');orb.className='orb '+state;var n=(st.players||[]).length;
  var T={offline:['Offline','Press Start when you want to play.'],starting:['Starting\u2026','Getting the world ready. Big modpacks can take a few minutes.'],online:['Online',n?(n===1?'1 person is playing.':n+' people are playing.'):'Ready for players. Share an address on the right.'],stopping:['Stopping\u2026','Saving the world before it shuts down.'],crashed:['Stopped','Something went wrong. See below.']}[state]||['Offline',''];
  $('#st-title').textContent=T[0];$('#st-sub').textContent=T[1];
  var running=state==='starting'||state==='online'||state==='stopping';$('#srv-start').hidden=running;$('#srv-stop').hidden=!running;$('#srv-restart').hidden=state!=='online';$('#srv-stop').disabled=state==='stopping';
  $('#srv-start').disabled=!!d.other_running;$('#srv-start').title=d.other_running?'Another server is running. Stop it first.':'';
  $('#stat-players').textContent=state==='online'?n+(st.max_players?' / '+st.max_players:''):'-';
  $('#srv-crash').hidden=state!=='crashed';$('#prob-badge').hidden=state!=='crashed';if(state==='crashed'&&lastState!=='crashed'){$('#srv-ready').hidden=true;api('/api/server/diagnose?id='+S.sid).then(function(dg){$('#srv-crash-text').textContent=dg.summary||'The app can usually fix this for you.';$('#srv-fix').hidden=!dg.fixable;S.diag=dg;if(tab==='problems')renderHealth()}).catch(function(){})}
  if(state==='online'&&lastState&&lastState!=='online')toast('Your server is online!');
  lastState=state;renderPlayers(st.players||[],state);
  if(running&&!conT)pollConsole();
  if(first)api('/api/server/settings?id='+S.sid).then(function(x){$('#stat-ram').textContent=Math.round(x.ram_mb/1024)+' GB'}).catch(function(){})}
function pollConsole(){var id=S.sid;api('/api/server/console?id='+id+'&since='+conSince).then(function(d){if(S.view!=='server'||S.sid!==id){conT=null;return}conSince=d.next;addConsole(d.lines);
  var st=srv&&srv.status&&srv.status.state;conT=(st==='offline'||st==='crashed')&&!d.lines.length?null:setTimeout(pollConsole,document.hidden?2500:700)}).catch(function(){conT=setTimeout(pollConsole,2000)})}
function addConsole(lines){if(!lines.length)return;var c=$('#console'),e=$('#console-empty');if(e)e.remove();var near=c.scrollHeight-c.scrollTop-c.clientHeight<80,frag=document.createDocumentFragment();
  lines.forEach(function(l){frag.appendChild(el('div','c '+(l.kind||'info'),l.t))});c.appendChild(frag);var extra=c.childElementCount-1500;if(extra>0){var r=document.createRange();r.setStartBefore(c.firstChild);r.setEndAfter(c.children[extra-1]);r.deleteContents()}if(near)c.scrollTop=c.scrollHeight}
$('#srv-start').onclick=function(){startServer(false)};
function startServer(eula){var b=$('#srv-start');b.disabled=true;api('/api/server/start',{id:S.sid,eula:eula}).then(function(){$('#srv-ready').hidden=true;conSince=0;clearTimeout(sPollT);refreshServer(false);if(!conT)pollConsole()})
  .catch(function(e){b.disabled=false;if(e.data&&e.data.eula){ask('Agree to the Minecraft rules','To run a server you need to agree to the Minecraft EULA (Mojang\u2019s rules for servers). In short: don\u2019t sell things that change the game.','I agree',false).then(function(ok){if(ok)startServer(true)})}else toast(e.message,true)})}
$('#srv-stop').onclick=function(){var n=srv&&srv.status&&(srv.status.players||[]).length;var go2=function(){api('/api/server/stop',{id:S.sid}).then(function(){clearTimeout(sPollT);refreshServer(false)}).catch(function(e){toast(e.message,true)})};
  if(n)ask('Stop the server?',n===1?'1 person is playing. They\u2019ll be disconnected. The world is saved first.':n+' people are playing. They\u2019ll be disconnected. The world is saved first.','Stop server',true).then(function(ok){if(ok)go2()});else go2()};
$('#srv-restart').onclick=function(){api('/api/server/restart',{id:S.sid}).then(function(){toast('Restarting\u2026');clearTimeout(sPollT);refreshServer(false)}).catch(function(e){toast(e.message,true)})};
$('#srv-folder').onclick=function(){api('/api/open',{dir:srv.server.dir}).catch(function(e){toast(e.message,true)})};
$('#srv-fix').onclick=function(){var b=this;b.disabled=true;b.lastChild.textContent='Fixing\u2026';api('/api/server/fix',{id:S.sid}).then(function(r){b.disabled=false;b.lastChild.textContent='Fix it for me';
  if(r.changed){toast('Fixed. Starting your server again\u2026');$('#srv-crash').hidden=true}else{selTab('problems');toast('This one needs you. See Health.',true)}clearTimeout(sPollT);refreshServer(false)}).catch(function(e){b.disabled=false;b.lastChild.textContent='Fix it for me';toast(e.message,true)})};
$$('[data-copy]').forEach(function(b){b.onclick=function(){var t=$('#'+b.dataset.copy).textContent;if(!/[0-9a-z]/i.test(t)||/Not |Finding/.test(t))return;(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){toast('Copied: '+t)}).catch(function(){toast('Couldn\u2019t copy. Select the address and copy it.',true)})}});
$('#help-net').addEventListener('toggle',function(){if(!this.open||!srv||srv.status.state!=='online')return;var r=$('#reach');r.textContent='Checking whether friends far away can reach you\u2026';
  api('/api/server/reach?id='+S.sid).then(function(d){r.textContent='';var dt=el('i','dot '+(d.reachable===true?'online':d.reachable===false?'crashed':''));r.appendChild(dt);r.appendChild(document.createTextNode(d.detail||''))}).catch(function(){r.textContent=''})});
/* tabs */
function selTab(t){tab=t;$$('#tabs button').forEach(function(b){b.setAttribute('aria-selected',b.dataset.tab===t)});['console','players','settings','problems'].forEach(function(x){$('#t-'+x).hidden=x!==t});
  if(t==='settings')loadSrvSettings();if(t==='problems')renderHealth()}
$$('#tabs button').forEach(function(b){b.onclick=function(){selTab(b.dataset.tab)}});
$('#cmd-form').onsubmit=function(e){e.preventDefault();var c=$('#cmd').value.trim();if(!c)return;sendCmd(c);$('#cmd').value=''};
function sendCmd(c){if(!srv||srv.status.state!=='online'){toast('Start the server first, then send commands.',true);return}api('/api/server/command',{id:S.sid,command:c}).then(function(){if(!conT)pollConsole()}).catch(function(e){toast(e.message,true)})}
$$('[data-q]').forEach(function(b){b.onclick=function(){var q=b.dataset.q;
  if(q==='admin')prompt2('Make someone an admin','Admins can use commands in the game, like changing the time or giving items. Type their exact Minecraft name.','Minecraft name','Make admin').then(function(n){if(n)sendCmd('op '+n)});
  else if(q==='say')prompt2('Send a message','Everyone on the server sees this in their chat.','Your message','Send').then(function(m){if(m)sendCmd('say '+m)});
  else sendCmd(q)}});
function renderPlayers(list,state){var box=$('#players');box.textContent='';if(!list.length){box.appendChild(el('p','muted',state==='online'?'No one is playing yet. Share an address from \u201cHow to join\u201d with your friends.':'Players show up here while the server is online.'));return}
  list.forEach(function(n){var p=el('div','player'),im=new Image();im.alt='';im.referrerPolicy='no-referrer';im.src='https://mc-heads.net/avatar/'+encodeURIComponent(n)+'/36';im.onerror=function(){im.style.visibility='hidden'};p.appendChild(im);p.appendChild(el('b',null,n));
    var a=el('button','btn sm quiet','Make admin');a.onclick=function(){sendCmd('op '+n)};p.appendChild(a);box.appendChild(p)})}
/* settings */
var setVals={};
function loadSrvSettings(){api('/api/server/settings?id='+S.sid).then(function(d){var box=$('#setlist');box.textContent='';setVals={};
  var ram=el('div','set'),rl=el('div');rl.appendChild(el('b',null,'Memory'));rl.appendChild(el('span',null,'How much of your computer\u2019s memory the server can use. More helps big modpacks.'));ram.appendChild(rl);
  var rc=el('div','ctl'),rs=el('select');var sys=Math.max(4,Math.floor(d.system_mb/1024*.8));for(var g=2;g<=sys;g++){var o=el('option',null,g+' GB');o.value=g*1024;rs.appendChild(o)}rs.value=Math.max(2048,Math.round(d.ram_mb/1024)*1024);rs.setAttribute('aria-label','Memory');rc.appendChild(rs);ram.appendChild(rc);box.appendChild(ram);setVals.__ram=rs;
  d.fields.forEach(function(f){var row=el('div','set'),l=el('div');l.appendChild(el('b',null,f.label));if(f.help)l.appendChild(el('span',null,f.help));row.appendChild(l);var c=el('div','ctl'),inp;
    if(f.type==='bool'){var w=el('span','sw');inp=el('input');inp.type='checkbox';inp.checked=String(f.value)==='true'||f.value===true;w.appendChild(inp);w.appendChild(el('span'));c.appendChild(w)}
    else if(f.type==='choice'){inp=el('select');(f.choices||[]).forEach(function(ch){var o=el('option',null,ch[1]);o.value=ch[0];inp.appendChild(o)});inp.value=f.value;c.appendChild(inp)}
    else{inp=el('input');inp.type='text';inp.value=f.value==null?'':f.value;if(f.type==='int')inp.inputMode='numeric';c.appendChild(inp)}
    inp.setAttribute('aria-label',f.label);setVals[f.key]=inp;row.appendChild(c);box.appendChild(row)})}).catch(function(e){toast(e.message,true)})}
$('#set-save').onclick=function(){var vals={},ram=null;Object.keys(setVals).forEach(function(k){var i=setVals[k];if(k==='__ram'){ram=+i.value;return}vals[k]=i.type==='checkbox'?i.checked:i.value});
  api('/api/server/settings',{id:S.sid,values:vals,ram_mb:ram}).then(function(){toast(srv&&srv.status.state==='online'?'Saved. Restart the server to use the new settings.':'Saved.');$('#stat-ram').textContent=Math.round(ram/1024)+' GB'}).catch(function(e){toast(e.message,true)})};
$('#srv-rename').onclick=function(){prompt2('Rename server','Give it a name you\u2019ll recognise.',srv.server.name,'Rename').then(function(n){if(!n)return;api('/api/servers/rename',{id:S.sid,name:n}).then(function(s){srv.server.name=s.name;$('#srv-name').textContent=s.name;var c=$('#crumb span');if(c)c.textContent=s.name;toast('Renamed')}).catch(function(e){toast(e.message,true)})})};
$('#srv-remove').onclick=function(){ask('Remove from your list?','The server disappears from the app, but its folder and world stay on your computer. You can add it back any time.','Remove',true).then(function(ok){if(ok)api('/api/servers/remove',{id:S.sid}).then(function(){toast('Removed from your list');go('home')}).catch(function(e){toast(e.message,true)})})};
/* health */
function renderHealth(){var h=$('#health');h.textContent='';var st=srv&&srv.status&&srv.status.state;
  if(st==='crashed'&&S.diag){h.appendChild(el('h3','sec-title','What went wrong'));h.appendChild(el('p','sec-sub',S.diag.summary||''));(S.diag.findings||[]).forEach(function(f){var x=el('div','finding '+(f.action==='manual'?'t-warn':'t-bad'));x.appendChild(icon('alert'));x.appendChild(el('div',null,f.message));h.appendChild(x)});
    var b=el('button','btn primary');b.style.marginTop='14px';b.appendChild(icon('wrench'));b.appendChild(document.createTextNode(S.diag.fixable?'Fix it for me':'Try to fix it'));b.onclick=function(){$('#srv-fix').click()};h.appendChild(b)}
  else{var ok=el('div','finding t-ok');ok.appendChild(icon('check'));ok.appendChild(el('div',null,st==='online'?'Everything looks good. Your server is running.':'No problems found. If the server stops unexpectedly, the app explains why here and offers to fix it.'));h.appendChild(ok)}
  api('/api/server/removed?id='+S.sid).then(function(s){var box=$('#rm-list');box.textContent='';$('#rm-title').hidden=!s.removed.length;box.previousElementSibling.hidden=!s.removed.length;
    s.removed.forEach(function(m){var it=el('div','item'),t=el('div','t');t.appendChild(el('b',null,m.jar));t.appendChild(el('small',null,m.reason));it.appendChild(t);var b=el('button','btn sm quiet');b.appendChild(icon('undo'));b.appendChild(document.createTextNode('Put back'));
      b.onclick=function(){b.disabled=true;api('/api/restore',{dir:srv.server.dir,bucket:m.bucket,jar:m.jar}).then(function(){it.remove();toast('Put back '+m.jar)}).catch(function(e){b.disabled=false;toast(e.message,true)})};it.appendChild(b);box.appendChild(it)})}).catch(function(){})}
$('#srv-checklog').onclick=function(){pick({kind:'file',exts:['log','txt'],title:'Choose the log file',start:srv?joinPath(srv.server.dir,'logs'):S.home}).then(function(p){if(!p)return;
  api('/api/server/fixlog',{id:S.sid,log:p,aggressive:!!(S.info.settings&&S.info.settings.aggressive)}).then(function(){startProgress('fix')}).catch(function(e){toast(e.message,true)})})};

/* ---------- app settings ---------- */
function loadSettings(){applyTheme(themeMode(),false);var st=S.info.settings||{};$$('#tb-seg button').forEach(function(b){b.setAttribute('aria-pressed',b.dataset.v===(st.titlebar||'custom'))});$('#tb-row').hidden=!S.info.native_capable;
  $('#aggr').checked=!!st.aggressive;$('#cfkey').value=st.cfkey||'';$('#about-text').textContent='Version '+S.info.version+'. Made to turn modpacks into servers without the headaches.';paintUpdate()}
$$('#theme-seg button').forEach(function(b){b.onclick=function(){applyTheme(b.dataset.v,true)}});
$$('#tb-seg button').forEach(function(b){b.onclick=function(){$$('#tb-seg button').forEach(function(x){x.setAttribute('aria-pressed',x===b)});S.info.settings.titlebar=b.dataset.v;api('/api/settings',{titlebar:b.dataset.v}).then(function(){toast('Saved. It changes the next time you open the app.')}).catch(function(){})}});
$('#aggr').onchange=function(){S.info.settings.aggressive=this.checked;api('/api/settings',{aggressive:this.checked}).catch(function(){})};
$('#cfkey').onchange=function(){S.info.settings.cfkey=this.value.trim();api('/api/settings',{cfkey:this.value.trim()}).catch(function(){})};

/* ---------- updates ---------- */
var upd={state:'idle'};
function paintUpdate(){var t={idle:'Up to date.',checking:'Checking for updates\u2026',downloading:'Downloading version '+(upd.version||'')+'\u2026',ready:'Version '+(upd.version||'')+' is ready to install.',up_to_date:'You have the newest version.',error:'Couldn\u2019t check for updates right now.',disabled:'Updates are switched off in this copy.'}[upd.state]||'';
  $('#upd-text').textContent=t;var p=$('#upd-pill');p.hidden=upd.state!=='ready';$('#upd-check').textContent=upd.state==='ready'?'Install now':'Check now'}
var updAsked={},updBoot=Date.now();
function pollUpdate(){api('/api/update').then(function(d){upd=d;paintUpdate();maybeAskUpdate();
  // right after opening, follow the first check closely so the prompt appears as soon as the update is downloaded
  if(Date.now()-updBoot<180000&&/^(idle|checking|downloading)$/.test(d.state))setTimeout(pollUpdate,1500)}).catch(function(){})}
function runningServer(){return S.servers.filter(function(s){return s.state==='online'||s.state==='starting'||s.state==='stopping'})[0]}
function maybeAskUpdate(){if(upd.state!=='ready'||!upd.version||updAsked[upd.version])return;
  if(!S.booted||S.view==='install'||!$('#veil').hidden||polling){clearTimeout(S.askT);S.askT=setTimeout(maybeAskUpdate,3000);return}   // wait for a quiet moment
  updAsked[upd.version]=true;installUpdate(true)}
$('#upd-pill').onclick=function(){installUpdate(false)};
$('#upd-check').onclick=function(){if(upd.state==='ready'){installUpdate(false);return}api('/api/update/check',{}).then(function(d){upd=d;paintUpdate();setTimeout(pollUpdate,1500)}).catch(function(){})};
function installUpdate(auto){api('/api/servers').then(function(d){S.servers=d.servers}).catch(function(){}).then(function(){showUpdate(auto)})}
function showUpdate(auto){var box=el('div','notes'),cur=upd.current||S.info.version,rs=runningServer();
  box.appendChild(el('p',null,'Version '+upd.version+' of Modpack Server Builder is ready'+(cur?'. You have version '+cur+'.':'.')+' Updating takes a few seconds: the app closes, updates and opens again.'));
  if(rs){var w=el('p',null,'\u201c'+rs.name+'\u201d is running. Updating stops it safely first (your world is saved). You can start it again afterwards.');w.style.color='var(--t-warn)';box.appendChild(w)}
  if(upd.notes){box.appendChild(el('h4',null,'What\u2019s new'));box.appendChild(renderNotes(upd.notes))}
  modal('Update available',box,[{t:'Remind me later',fn:function(){if(auto)toast('No problem. Click \u201cUpdate ready\u201d at the top whenever you like.')}},
    {t:'Update and restart',primary:true,fn:function(){setTimeout(function(){doUpdate(!!rs)},0)}}])}
function doUpdate(stopping){document.body.textContent='';var p=el('p',null,stopping?'Stopping your server safely, then updating\u2026':'Updating\u2026 The app will open again in a moment.');
  p.style.cssText='margin:30vh auto;max-width:520px;text-align:center;font:600 18px system-ui;color:#c9d3ea;padding:0 20px';document.body.style.background='#0d1428';document.body.appendChild(p);
  api('/api/update/install',{stop_server:true}).then(function(){p.textContent='Updating\u2026 The app will open again in a moment.'}).catch(function(e){p.textContent='The update couldn\u2019t be installed: '+e.message+' Close the app and open it again to try once more.'})}
function renderNotes(md){var box=el('div','notes'),ul=null;String(md).split(/\r?\n/).forEach(function(l){var m;
  if((m=l.match(/^#{1,4}\s+(.*)/))){ul=null;box.appendChild(el('h4',null,m[1]))}else if((m=l.match(/^\s*[-*]\s+(.*)/))){if(!ul){ul=el('ul');box.appendChild(ul)}ul.appendChild(el('li',null,m[1].replace(/\*\*/g,'')))}else if(l.trim()){ul=null;box.appendChild(el('p',null,l.replace(/\*\*/g,'')))}});return box}
$('#whats-new').onclick=function(){api('/api/whatsnew?force=1').then(function(d){modal('What\u2019s new in '+d.version,d.notes?renderNotes(d.notes):'Release notes aren\u2019t available right now.',[{t:'Close',primary:true}])}).catch(function(){})};

/* ---------- installer ---------- */
var installed=false;
$('#skip-install').onclick=function(){go('home')};
$('#go-install').onclick=function(){var b=this;if(installed){b.disabled=true;api('/api/launch_installed',{}).then(function(){document.body.textContent=''}).catch(function(e){b.disabled=false;toast(e.message,true)});return}
  b.disabled=true;b.textContent='Installing\u2026';api('/api/install',{desktop:$('#i-desktop').checked}).then(function(){installed=true;b.disabled=false;b.textContent='Open Modpack Server Builder';$('#inst-title').textContent='Installed';
    $('#inst-sub').textContent='You\u2019ll find it in your Start menu'+($('#i-desktop').checked?' and on your desktop.':'.')}).catch(function(e){b.disabled=false;b.textContent='Install';toast(e.message,true)})};

/* ---------- close protection (browser/Edge window only; the native window asks itself) ---------- */
window.addEventListener('beforeunload',function(e){if(S.info.native)return;var running=S.servers.some(function(s){return s.state==='online'||s.state==='starting'});if(polling||running){e.preventDefault();e.returnValue=''}});

/* ---------- boot ---------- */
api('/api/info').then(function(d){S.info=d;S.home=d.home;S.sep=d.win?'\\':'/';var st=d.settings||{};
  applyTheme(st.theme==='light'||st.theme==='dark'?st.theme:themeMode(),false);setupFrame();
  if(d.mode==='install'){$('#inst-dir').textContent=d.install_dir;if(d.installed){$('#inst-sub').textContent='Version '+d.installed+' is installed. This updates it to '+d.version+'.';$('#go-install').textContent='Update'}else $('#inst-sub').textContent='Version '+d.version+'. About 9 MB.';
    VIEWS.forEach(function(x){$('#v-'+x).hidden=x!=='install'});S.view='install';$('#go-settings').hidden=true;return}
  api('/api/events?since=0').then(function(ev){if(ev.state==='running'){S.jobKind=ev.kind;startProgress(ev.kind==='fix'?'fix':'build')}else go('home')}).catch(function(){go('home')}).then(function(){S.booted=true});
  pollUpdate();setInterval(pollUpdate,60000);
  api('/api/whatsnew').then(function(w){if(w.notes)modal('What\u2019s new in '+w.version,renderNotes(w.notes),[{t:'Got it',primary:true,fn:function(){api('/api/whatsnew/seen',{}).catch(function(){})}}],function(){api('/api/whatsnew/seen',{}).catch(function(){})})}).catch(function(){});
}).catch(function(){buildScene();go('home')});
setInterval(function(){if(!polling&&S.view!=='server')api('/api/ping').catch(function(){})},15000);
buildScene();
})();
</script>
</body>
</html>
"""


# ============================================================================


def _ensure_streams():
    """A windowed .exe has no console: send output to a log file instead of losing it."""
    if sys.stdout is None or sys.stderr is None:
        d = app_data_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = open(d / "app.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        f.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f


def main():
    if handle_update_args(sys.argv[1:]):
        return
    _ensure_streams()
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    set_app_id()
    ap = argparse.ArgumentParser(description="Build a Minecraft server from a CurseForge/Modrinth modpack, "
                                 "or fix a server from its log.")
    ap.add_argument("pack", nargs="?", help="Modpack .zip (CurseForge) or .mrpack (Modrinth)")
    ap.add_argument("-o", "--output", help="Server output folder (default: <pack>-server)")
    ap.add_argument("--ram", default="6G", help="Server memory, e.g. 4G, 8G (default 6G)")
    ap.add_argument("--accept-eula", action="store_true", help="You accept https://aka.ms/MinecraftEULA (sets eula=true)")
    ap.add_argument("--curseforge-key", default=os.environ.get("CURSEFORGE_API_KEY"),
                    help="Optional CurseForge API key (or env CURSEFORGE_API_KEY)")
    ap.add_argument("--exclude", action="append", default=[], help="Mod filename substring to leave out (repeatable)")
    ap.add_argument("--keep-client-mods", action="store_true", help="Don't remove client-only mods")
    ap.add_argument("--no-test-run", action="store_true", help="Skip the test launch / auto-fix loop")
    ap.add_argument("--aggressive", action="store_true", help="Also remove mods that are only suspected of causing a crash")
    ap.add_argument("--max-attempts", type=int, default=8, help="Max test launches (default 8)")
    ap.add_argument("--offline", action="store_true", help="Don't query Modrinth (offline detection only)")
    ap.add_argument("--fix-log", metavar="LOG", nargs="+", help="Diagnose and fix a server from latest.log / crash report(s)")
    ap.add_argument("--server-dir", help="Server folder for --fix-log (contains mods/)")
    ap.add_argument("--report-only", action="store_true", help="With --fix-log: only diagnose, change nothing")
    ap.add_argument("--retest", action="store_true", help="With --fix-log: test-launch afterwards (needs --accept-eula)")
    ap.add_argument("--gui", action="store_true", help="Open the interface in your browser (default when no pack is given)")
    ap.add_argument("--no-browser", action="store_true", help="Start the interface but don't open a browser tab")
    ap.add_argument("--port", type=int, default=0, help="Port for the local interface (default: any free port)")
    ap.add_argument("--browser-tab", action="store_true", help="Open the interface in a normal browser tab instead of its own window")
    ap.add_argument("--install", action="store_true", help="Install the app for this Windows user (shows the installer)")
    ap.add_argument("--uninstall", action="store_true", help="Remove the installed app")
    ap.add_argument("--quiet", action="store_true", help="With --install/--uninstall: no questions or windows")
    ap.add_argument("--no-desktop-shortcut", action="store_true", help="With --install --quiet: skip the desktop shortcut")
    ap.add_argument("--updated", help=argparse.SUPPRESS)   # set by the updater on relaunch
    a = ap.parse_args()

    if a.fix_log:
        if not a.server_dir:
            ap.error("--fix-log needs --server-dir")
        try:
            fix_from_log(a.fix_log, a.server_dir, a.accept_eula, a.aggressive, a.report_only, a.retest,
                         not a.offline)
        except Exception as e:  # noqa: BLE001
            print(f"\nERROR: {e}", file=sys.stderr)
            sys.exit(1)
        return
    if a.uninstall:
        try:
            uninstall_app(a.quiet)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {e}", file=sys.stderr)
            if not a.quiet:
                message_box(f"Couldn't remove the app: {e}", 0x10)
            sys.exit(1)
        return
    if a.install and a.quiet:
        try:
            print(f"Installed to {install_app(not a.no_desktop_shortcut)}")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        return
    if a.gui or not a.pack:
        mode = "install" if is_install_mode(a.install) else "app"
        if mode == "app" and not a.port and focus_existing_instance():
            return                                   # already open: we just brought it to the front
        run_web(a.port, not a.no_browser, not a.browser_tab, mode=mode)
        return
    out = a.output or str(Path(a.pack).with_suffix("")) + "-server"
    try:
        build_server(a.pack, out, a.ram, a.accept_eula, a.curseforge_key, a.exclude, a.keep_client_mods,
                     not a.no_test_run, a.aggressive, a.max_attempts, not a.offline)
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
