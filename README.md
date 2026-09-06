# XGRS Account Manager

A Windows tool for managing many Roblox accounts: store cookies securely, launch
several clients at once, and keep them online.

Website: [xgrs.lol](https://xgrs.lol) · Discord: [ds.xgrs.lol](https://ds.xgrs.lol)

## Features

**Accounts**
Save accounts by cookie, browser login or user/password import. Group them, add
notes, and join any game by Place ID, VIP link, job ID or by following a user.

**Auto Connect**
Keeps one Roblox client alive per account. Each row shows state, RAM, ping, PID
and how long the client has been closed, then relaunches that exact account when
its client closes, crashes or hits a Roblox error (264, 266, 267, 268, 270, 277,
279 / ID 17, 280, 403, 524, 600, "Failed to Load Library"). Select several
accounts to start, stop or edit them together, or expand the arrow on a row for
its own Start, Restart Client, Edit and Remove buttons. Stop All force-closes the
clients and the bootstrapper, and cancels a launch that is still in flight.

**Auto-Rejoin**
Watches presence for a chosen Place ID and rejoins when the account leaves it.

**Anti AFK**
Sends key presses to every Roblox window on a timer so accounts are not kicked.

**Multi Roblox**
Runs several Roblox clients side by side, with a window grid hotkey and a headless
mode that hides client windows. The tab explains what the Default and Handle64
methods actually do and which one to pick.

**Window size**
Settings has a Resizable Roblox Windows option: pick any width and height, center
the window, or strip its frame for a borderless client. New clients are resized as
soon as their window appears, so a client that Auto Connect relaunches comes back
at the same size.

**Kill switch**
The skull in the title bar closes every Roblox process instantly on left click.
Right click opens a resizable panel listing each process with its PID, account,
RAM and uptime, so you can close them one by one.

**Themes**
Eight built-in palettes (Midnight, Carbon, Nord, Dracula, Ocean, Crimson, Forest,
Light) plus a colour picker for every interface role, saved between runs and
applied to every window and dialog without a restart.

**Settings**
Searchable, grouped into General, Roblox, Discord, Misc, Themes and Developer.
Includes a Roblox downloader, RAM trimming, Discord webhook alerts, a WebSocket
API and a slider for how often Auto Connect refreshes its status.

**Browser engine**
Sign in through Chrome, Firefox, Edge, Brave, Opera GX, Opera, Vivaldi, Yandex,
the built-in portable Chromium, or any executable you point at with Custom
Browser.

**Interface**
The main window is resizable and remembers its size. Dialogs carry their own
icons and window controls, and the Auto Connect editor switches between one and
two columns as you resize it.

## Install

Download the latest `XGRS Manager-vX.Y.Z.exe` from
[Releases](https://github.com/xgrs-owner/XGRS-Account-Manager/releases), put it in
its own folder and run it. No Python needed.

## Run from source

```bash
git clone https://github.com/xgrs-owner/XGRS-Account-Manager
cd XGRS-Account-Manager
pip install -r requirements.txt
python src/main.py
```

To build the executable yourself:

```bash
pip install pyinstaller
python scripts/build.py
```

Requirements: Windows, Python 3.12+ (or [uv](https://docs.astral.sh/uv/)) and
Google Chrome.

## Data and privacy

Everything is stored locally in `XGRSManagerData` next to the executable.
Cookies are encrypted with a hardware key or a password of your choice. There is
no telemetry and no analytics. Network traffic goes only to Roblox APIs, GitHub
(update check) and a Discord webhook if you enable one.

## Disclaimer

For educational use. You are responsible for complying with the Roblox Terms of
Service.

## Credits

Built on the open source [Roblox Account Manager by
evanovar](https://github.com/evanovar/RobloxAccountManager), which was itself
inspired by ic3w0lf22's original manager. Licensed under GPL-3.0, see `LICENSE`.
