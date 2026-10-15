# What's new

Every update to Modpack Server Builder is listed here, newest first.
The app installs updates by itself, and shows you this list the first time it opens after an update.

<!--
How to write an entry (for whoever makes the next update):
  * Start with "## X.Y.Z - Month Day, Year". Pushing a new entry to main publishes that version automatically.
  * Write for someone who has never run a server: say what they'll notice, not what changed in the code.
  * Group lines under "### New", "### Better" and "### Fixed". Skip groups that are empty.
  * One short sentence per line. Bold the thing they'll look for, e.g. **Fix it for me**.
-->

## 3.5.0 - October 15, 2026

### Better
- **One download, clearly named.** The downloads page now has a single installer, **MinecraftServerManager-Setup.exe**. The extra copies under the old name and the separate "portable" version are gone.
- Updates now use the new installer name too. If you're on an older version, it still updates automatically as usual.

### Please note
- **On version 3.4 or older? Open the app before November 15, 2026** so it can update itself. After that date, older versions can't find updates on their own. If you miss it, just download the installer again from the [download page](https://github.com/ricoism/minecraft-server-manager/releases/latest) and run it. Your servers and settings are kept.

## 3.4.0 - October 15, 2026

### New
- **New name: Minecraft Server Manager.** The app used to be called Modpack Server Builder. Your Start menu and desktop shortcuts, the title bar and Installed apps now show the new name.
- Nothing else changes: your servers, settings and downloads all stay exactly where they were. A taskbar button you pinned keeps working.

## 3.3.0 - October 15, 2026

### New
- **Your own server icons.** Click a server's icon (or **Change icon** in its Settings tab) and pick any picture. It's cropped to a square automatically. It can also show up next to your server in your friends' Minecraft multiplayer list.
- Servers whose folder already has an icon (`server-icon.png`) now show it instead of the green cube.
- **The sky comes alive.** Clouds drift by during the day, stars twinkle at night, and every so often a shooting star goes past. You can turn this off in Settings → **Moving sky** if your computer is on the slow side.
- **A friendly warning about memory.** If you give a server much more memory than it needs, or most of your computer's memory, the app explains why that can make things worse and offers the recommended amount with one click.

### Fixed
- **Memory changes didn't stick for Forge and NeoForge servers.** The app could read an example line in the server's settings file instead of your choice, so it showed the wrong amount and could save it back. It now always shows and keeps what you picked.
- The memory you pick while creating a server is no longer reset if you go back a step.
- Changing a server's memory no longer forgets which modpack it came from.

## 3.2.0 - October 15, 2026

### New
- **The app tells you when there's an update.** Every time you open it, it checks for a new version. If there is one, it shows what's new and asks whether to **Update and restart** now or **Remind me later**.
- If a server is running when you choose to update, the app stops it safely first (your world is saved), then updates.

### Better
- The update prompt waits for a quiet moment. It never pops up while a server is being set up.
- Chose "Remind me later"? The **Update ready** button at the top lets you update whenever you like, and the app asks again next time you open it.

## 3.1.2 - October 15, 2026

### Fixed
- **The taskbar showed the old app icon after updating.** Windows remembers icons, so the taskbar, the Start menu, desktop shortcuts and Installed apps kept the old one. They now all show the grass-block icon, including a taskbar button you've pinned. It can take a few seconds to change after the update.

## 3.1.1 - October 15, 2026

### New
- **You can leave a setup and come back.** While a server is being set up, a button at the top of the window shows how far along it is. Click it from anywhere in the app to return to the progress screen.
- **New servers show up in "Your servers" right away**, marked "Setting up…". Click the card to see its progress.
- **Skip the test.** During "First start" you can skip the check and start the server yourself whenever you like.

### Fixed
- **The first start could keep trying the same fix over and over.** If a fix doesn't help, the app now stops and tells you plainly what still needs doing, instead of going round in circles.
- If another server is already using the same port, the app now says so instead of trying to start again and again.
- When a setup finishes while you're on another screen, the app no longer pulls you away. The button at the top changes to "Server ready" instead.
- Stopping a setup before anything was built no longer leaves a broken server in your list.

## 3.1.0 - October 15, 2026

### New
- **A whole new look.** The app now sits in a little blocky world whose sky follows the time of day: sunrise, daytime, sunset and a starry night.
- **The window's title bar is part of the app.** No more plain Windows bar on top. You can switch back to the standard one in Settings if you prefer.
- **Your servers in one place.** The home screen lists every server you've made. Click one to start it.
- **Find modpacks without leaving the app.** Search by name, pick one, and the app downloads it for you. No browser needed.
- **Plain Minecraft servers.** Want a server without mods? Choose "Just Minecraft" and pick a version.
- **A control room for each server.** Big Start and Stop buttons, who's online, and the addresses your friends type into Minecraft, each with a Copy button.
- **"Fix it for me".** If your server stops because of a problem, the app explains it in plain words and can fix it with one click.
- **Change server settings without editing files.** Difficulty, game mode, player limit, memory and more, with a short explanation for each.
- **Automatic updates.** The app checks for new versions on its own, downloads them in the background, and asks before installing.

### Better
- Creating a server takes three short steps, and the app picks sensible settings (like memory) for you.
- You only need to agree to the Minecraft rules (the EULA) once.
- Closing the app while a server is running now stops the server safely first, so your world is saved.

## 3.0.0 - October 15, 2026

### New
- Modpack Server Builder became a real Windows app with its own window, Start menu and desktop shortcuts, and a normal uninstaller.
- Java is downloaded for you when your computer doesn't have the right version.

### Better
- Much better at spotting mods that only work in the game itself (not on a server) and setting them aside.
- Reading a crash log now points at the mod that caused the problem, not at shared libraries.
