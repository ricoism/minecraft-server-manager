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
