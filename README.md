# Modpack Server Builder

Turn any Minecraft modpack into a working server in a few clicks, even when the modpack has no server files.
No knowledge of servers, Java or coding needed.

**[Download for Windows](https://github.com/ricoism/modpack-server-builder/releases/latest/download/ModpackServerBuilder-Setup.exe)**
· [What's new](CHANGELOG.md)

## Using it

1. Download and open **ModpackServerBuilder-Setup.exe**. It installs itself (no admin needed) and adds a Start menu shortcut.
   - Windows may show "Windows protected your PC" the first time. Click **More info**, then **Run anyway**.
2. Click **Create a server**, search for a modpack (or pick a file you downloaded, or plain Minecraft) and follow the three steps.
3. Press **Start**. Share the address shown under "How to join" with your friends.

If the server ever stops because of a problem, press **Fix it for me**.

## What it does for you

- Downloads the modpack, the right mod loader (Forge, NeoForge, Fabric or Quilt) and the right Java.
- Takes out mods that only work in the game itself, not on a server, using Modrinth's data and each mod's own info.
- Test-starts the server, reads the log when something goes wrong, and fixes what it can.
- Keeps itself up to date. New versions are signed, and the app refuses any download whose signature doesn't check out.

## For developers

The whole app is one Python file, `modpack_server_builder.py`, using only the standard library.

```
python modpack_server_builder.py            # run the app (opens in your browser outside Windows)
python modpack_server_builder.py --help     # command-line options
build_exe.bat                               # build the .exe on Windows (needs: pip install pyinstaller)
```

Tests: `MSB_SOURCE=modpack_server_builder.py python tests/test_<name>.py`

### Releasing an update

1. Make the change.
2. Add a new entry at the top of `CHANGELOG.md` (`## X.Y.Z - date`), written for people who don't know anything about servers.
3. Push to `main`.

GitHub Actions then runs the tests, builds the .exe, signs it with the `UPDATE_SIGNING_KEY` secret and publishes the release
with the changelog entry as its notes. Installed apps pick it up within a few hours (or straight away with "Check now").

## Credits

- Window: [webview](https://github.com/webview/webview) (MIT), see `vendor/`.
- Fonts: Bricolage Grotesque and Silkscreen (SIL Open Font License), see `licenses/`.
- Mod data: [Modrinth](https://modrinth.com). Java: [Eclipse Temurin](https://adoptium.net).

Not affiliated with Mojang or Microsoft.
