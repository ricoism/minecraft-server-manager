"""Behaves like a (Forge) Minecraft server for tests. Mode comes from fake_mode.txt in the working dir:
normal | hang (ignores 'stop') | crash (crashes with a client-only-mod error while mods/fancyhud.jar exists)
| slow (never reaches Done)."""
import os
import sys
import time
from datetime import datetime

mode = open("fake_mode.txt").read().strip() if os.path.exists("fake_mode.txt") else "normal"
players = []


def out(thread, level, logger, msg):
    ts = datetime.now().strftime("%d%b%Y %H:%M:%S.%f")[:-3]
    print(f"[{ts}] [{thread}/{level}] [{logger}]: {msg}", flush=True)


def srv(msg, level="INFO", logger="minecraft/DedicatedServer"):
    out("Server thread", level, logger, msg)


out("main", "INFO", "cpw.mods.modlauncher.Launcher/MODLAUNCHER", "ModLauncher running: args [--launchTarget, forgeserver]")
out("main", "WARN", "net.minecraftforge.fml.loading.moddiscovery.ModFileParser/LOADING", "Mod file fancy has an invalid version")
os.makedirs("logs", exist_ok=True)

if mode == "crash" and os.path.exists("mods/fancyhud.jar"):
    out("main", "ERROR", "net.minecraftforge.fml.javafmlmod.FMLModContainer/LOADING", "Failed to create mod instance. ModID: fancyhud, class com.example.fancyhud.FancyHud")
    trace = ["java.lang.RuntimeException: Attempted to load class net/minecraft/client/gui/screens/Screen for invalid dist DEDICATED_SERVER",
             "\tat TRANSFORMER/fancyhud@1.0.0/com.example.fancyhud.FancyHud.<init>(FancyHud.java:20)",
             "\tat java.base/jdk.internal.reflect.NativeConstructorAccessorImpl.newInstance0(Native Method)",
             "\t... 12 more"]
    for t in trace:
        print(t, flush=True)
    with open("logs/latest.log", "w") as f:
        f.write("\n".join(trace) + "\n")
    sys.exit(1)

srv("Starting minecraft server version 1.20.1")
srv("Starting Minecraft server on *:" + os.environ.get("FAKE_PORT", "25570"))
srv("\x1b[33mLoading properties\x1b[m")
if mode == "slow":
    time.sleep(3600)
srv('Done (3.2s)! For help, type "help"')

for line in sys.stdin:
    cmd = line.strip()
    if not cmd:
        continue
    word, _, rest = cmd.partition(" ")
    if word == "list":
        srv(f"There are {len(players)} of a max of 20 players online: {', '.join(players)}", logger="minecraft/MinecraftServer")
    elif word == "say":
        srv(f"[Server] {rest}", logger="minecraft/MinecraftServer")
    elif word == "join":
        players.append(rest)
        srv(f"{rest}[/127.0.0.1:5555] logged in with entity id 1 at (0.5, 64.0, 0.5)", logger="minecraft/PlayerList")
        srv(f"{rest} joined the game", logger="minecraft/MinecraftServer")
    elif word == "leave":
        players.remove(rest)
        srv(f"{rest} lost connection: Disconnected", logger="minecraft/ServerGamePacketListenerImpl")
        srv(f"{rest} left the game", logger="minecraft/MinecraftServer")
    elif word == "chat":
        name, _, msg = rest.partition(" ")
        srv(f"<{name}> {msg}", logger="minecraft/MinecraftServer")
    elif word == "spam":
        for i in range(int(rest)):
            srv(f"spam line {i}")
    elif word == "err":
        srv("Something bad happened", level="ERROR")
        print("java.lang.IllegalStateException: boom", flush=True)
        print("\tat com.example.Foo.bar(Foo.java:1)", flush=True)
    elif word == "oldlist":
        srv(f"There are {len(players)}/20 players online:")
        srv(", ".join(players))
    elif word == "stop":
        if mode == "hang":
            continue
        srv("Stopping the server")
        srv("Saving worlds")
        sys.exit(0)
    else:
        srv('Unknown or incomplete command, see below for error', level="INFO")
