# What's new

Every update to Minecraft Server Manager is listed here, newest first.
The app installs updates by itself, and shows you this list the first time it opens after an update.

<!--
How to write an entry (for whoever makes the next update):
  * Start with "## X.Y.Z - Month Day, Year". Pushing a new entry to main publishes that version automatically.
  * Write for someone who has never run a server: say what they'll notice, not what changed in the code.
  * Group lines under "### New", "### Better" and "### Fixed". Skip groups that are empty.
  * One short sentence per line. Bold the thing they'll look for, e.g. **Fix it for me**.
-->

## 3.7.1 - October 17, 2026

A full check-up of the app, with lots of fixes for things that could go wrong.

### Fixed
- **Your server list could disappear** if antivirus or OneDrive was reading the app's settings file at the wrong moment. The app now waits and tries again, keeps a backup, and never saves over settings it couldn't read.
- **Adding a player could empty your whitelist or operator list** if the file had been edited by hand (for example saved with Notepad's "UTF-8 with BOM", or with a small typo). Those files are now read correctly. If one really can't be read, the app tells you instead of overwriting it.
- **"Delete server" is much more careful.** It won't delete a folder like your Desktop or Downloads (for example if you ran a server there once), a folder with another server inside it, or a folder with lots of other files. The confirmation now shows exactly which folder will go to the Recycle Bin.
- **A server could get stuck on "Stopping"** if it was stopped from inside the game and then hung while saving. It's now closed after a while, and pressing Stop closes it sooner.
- **A new server could show an old server's crash** if you deleted a server and made a new one with the same name. Every new server now starts fresh.
- **Two servers on the same port:** the check now looks at the port each server is really using.
- **"Put back" could replace a newer copy of a mod**, and removing the same mod twice could lose the first copy. Both copies are now kept, and nothing is overwritten.
- **Changing memory in Settings rewrote your own start.bat**, losing custom settings like your Java path. Now only the memory numbers change. The memory shown also comes from start.bat when that's where it's set.
- **"Try again" after a failed setup always failed.** It now uses a fresh folder. If the server was already built, you get "Open server" instead, so you can start or fix it.
- Some mods were wrongly flagged as "won't load" on NeoForge servers the app hadn't started yet, or as "for a different version" on snapshot servers.
- Mods with very long file names could lose their ".jar" when added.
- Re-adding a server that's already in your list no longer resets its name and icon.
- Typing `tick query` yourself no longer hides the answer.
- Pressing Stop while the app was still downloading Java left the server on "Stopping" until the download finished.
- If an update isn't ready yet, your servers are no longer stopped for nothing.
- Two servers that need the same Java no longer download it at the same time.
- Downloads that can't succeed (like a mod that was removed) now fail right away instead of retrying, and no half-downloaded files are left behind.
- A modpack updated under the same file name is now downloaded again instead of reusing the old one.
- Clicking the Settings tab again no longer throws away changes you haven't saved.
- Clicking "Start server" in the right-click menu and then quickly opening another server could start the wrong one.
- After switching servers quickly, a server's page could briefly show the other server's address or settings.
- The page checked a server's status more and more often each time you came back to it.
- Version numbers like `1.20.1.4` in the console were hidden as if they were IP addresses.
- The home screen and player list no longer lose keyboard focus every few seconds.
- A message popping up (like "Mods added") no longer closes a dialog you're typing in.
- The built-in file picker now accepts typed paths in any form (like `C:/Users/...` or `~/Documents`).
- If a setting can't be saved, you now see a message instead of it quietly not saving.
- The world's folder name can no longer be set to something that breaks the server.
- Smaller fixes to uninstalling (user names with a `%`), the update helper, and file uploads.

## 3.7.0 - October 17, 2026

### New
- **Run several servers at once.** Start as many as you like. Each one has its own console, players and stats, and the button at the top shows how many are running.
- If your computer may not have enough memory or processor power for another server, the app warns you before starting it, explains why it could lag, and lets you start it anyway.
- If two servers would use the same port, the app tells you which one and how to change the port in Settings.
- **Live memory and tick boxes** under Players, Memory and Version show how much memory the server is using and how smoothly it's running. Click them for more detail in the Health tab.

### Better
- **A new look for the server status.** The plain square is now a glowing beacon block: it charges up while starting, shoots a beam of light with sparks when online, powers down when stopping, and cracks red if the server crashes.
- Switches are now rounded squares, matching the knob inside them.
- When you close the app or update it, every running server is stopped safely (all worlds are saved).

## 3.6.0 - October 15, 2026

A big update with lots of things you asked for.

### New
- **A Mods tab for each modded server.** See every mod, add more with **Add mods** (or drop .jar files on it), turn mods off without deleting them, remove them and put them back. **Open mods folder** takes you straight there.
- **Mods are checked when you add them.** The app warns you if a mod only works inside the game (client-side), is made for a different loader (like a Fabric mod on a Forge server), is for another Minecraft version, is a plugin rather than a mod, is already installed, or needs another mod you don't have.
- **Live memory and tick speed.** Start a server and the Health tab shows how much memory it uses and its average tick time (and TPS), with small graphs, like Minecraft's own server window.
- **Make someone an operator in one click.** The Players tab has an **Operators** list. If your server uses a whitelist, you can add and remove players there too, or turn the whitelist on. It works whether the server is running or not.
- **Right-click a server** on the home screen to start or stop it, edit its settings, manage mods or players, open its folder, change its icon, rename it, remove it from the list or delete it.
- **Delete a server.** Deleting moves its folder to the Recycle Bin, so you can still get it back.
- **All the important server settings** are now in Settings, grouped into Basics, Players, World, Performance and Advanced. Any other lines in your server.properties are listed too.
- **Edit server.properties yourself.** A button opens the file in Notepad. Save it (Ctrl+S) before you close Notepad.

### Better
- **Your IP addresses are hidden by default**, so they don't end up in screenshots or streams. Press **Show** to see them. **Copy** always copies the real address, even while it's hidden.
- **The Save button in server settings is always on screen.** No more scrolling to the bottom. If you try to leave with unsaved changes, the app asks whether to save them.
- "Server name in the list" is now called **Server MOTD**.
- **Choosing files and folders uses the normal Windows "Open" window.** Prefer the one built into the app? Pick it in Settings → **Choosing files and folders**. The built-in one now looks and works like File Explorer.
- **Creating a server: the Next button is always visible**, and shows what you picked. Double-click a modpack to go straight to the next step.
- **When creating a server fails, the app tells you what to do, step by step**, with **Try again** and **Copy details** buttons.

### Fixed
- **Some modpacks couldn't be downloaded** ("The parameter is incorrect") because their name contains a character Windows doesn't allow in file names, like ":". Those names are now made safe.
- A failed setup no longer leaves an empty server behind in your list.
- The app could sometimes show an error or a blank list for a moment, because one of its answers got mixed up with the next one. That's fixed.

## 3.5.1 - October 15, 2026

A big round of fixes after a full check-up of the app.

### Fixed
- **Pressing Stop on a setup could make your next Start fail** with "cancelled". Stop now only stops what's running.
- **A server stopped from inside the game (`/stop`) showed up as a crash.** It's now shown as a normal stop.
- **Old modpacks (Minecraft 1.16 and earlier) could keep starting with a Java that's too new.** The app now gets the exact Java they need, and explains it correctly if something goes wrong.
- **Choosing your own save folder could build a new server on top of an existing one.** The app now always picks a free folder, and refuses folders that already have files.
- **If the first start of a new server failed, the list of things left to do wasn't shown.** You now see it right after setup.
- **The console could show the same lines several times** after stopping and starting a server.
- **Uninstalling removed the Java your servers use**, so their start files stopped working. It's now kept. Uninstalling also cleans up properly, even if the app is still open.
- A server with more memory than the Settings list offers no longer shows a blank memory setting (or "0 GB" after saving).
- Settings → Back no longer shows an error after you remove a server.
- The create wizard no longer reuses a modpack file from a previous attempt.
- Folder names keep their dots, so "Minecraft 1.21.4" no longer becomes "Minecraft 1214".
- Unusual characters in your server's message of the day (like "…") no longer break server.properties.
- Port problems now mention the port your server actually uses.
- A shaky internet connection no longer causes a scary "doesn't match its checksum" message when downloading an update; it simply tries again.
- Stopping a setup during a download now stops right away.
- Smaller fixes: "What's new" never covers the update prompt, the "Server ready" button clears once you've opened the server, and custom icons always refresh straight away.

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
