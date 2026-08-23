# Close Games Remotely

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that lets you remotely close games on other Steam devices so Steam Cloud saves can sync — without leaving your bed.

## The Problem

You're in bed with your Steam Deck, ready to play. But you left a game running on your Steam Machine in the other room. Steam Cloud won't sync the save file until that game closes. Normally you'd have to get up, walk to the other room, close the game, and come back.

## The Solution

Install this plugin on both devices. Press **Nuke** on your Steam Deck, and it will:

1. **Wake** the Steam Machine via Wake-on-LAN
2. **Close all running games** (so Steam Cloud can sync)
3. **Wait for cloud sync** to complete
4. **Turn off the TV** via HDMI-CEC
5. **Put the Steam Machine to sleep**

All from the Quick Access Menu on your Steam Deck.

## Features

- **Bidirectional** — works from any device to any other device
- **Wake-on-LAN** — wake sleeping devices with magic packets (subnet broadcast + direct IP + global broadcast)
- **Game closing** — finds and kills all Steam game processes (process groups, multi-round SIGTERM/SIGKILL)
- **HDMI-CEC TV control** — turn TV on/off via `cec-ctl` over HDMI
- **Sleep via Steam API** — uses Steam's own suspend mechanism
- **Step-by-step progress** — real-time status for each step of the nuke flow
- **Helper buttons** — individual Wake, Sleep, TV On, TV Off controls

## Requirements

- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) installed on all devices
- All devices on the same local network
- Wake-on-LAN configured on devices you want to wake remotely
- HDMI-CEC enabled on your TV (LG calls it "SimpLink") for TV control
- `cec-ctl` available on the device connected to the TV (part of `v4l-utils`, usually pre-installed on SteamOS)

## Install

1. Download the latest `close-games-remotely.zip` from [Releases](https://github.com/SafetZahirovic/decky-close-games-remotely/releases)
2. In Decky Loader, go to the plugin browser and use "Install from ZIP"
3. Install on **both** devices
4. Open the plugin on one device and add the other device (name + IP address)

## How It Works

Each device runs a lightweight HTTP server (port 55123). When you press Nuke on Device A, it sends HTTP requests to Device B's plugin, which executes the commands locally. Suspend is handled by emitting an event to the frontend which calls Steam's native `SteamClient.System.SuspendPC()` API.

## AI Transparency

This plugin was built collaboratively with [Claude Code](https://claude.ai/code) (Claude Opus 4.6). The entire development process — from initial scaffolding to iterative debugging — was done through conversation with an AI assistant. This includes:

- Project structure and build configuration
- Python backend (HTTP server, WOL, game process management, CEC control)
- TypeScript/React frontend (Decky QAM panel, step progress UI)
- Debugging WOL, TV control, and suspend issues through trial and error
- Researching MoonDeck and LG_Buddy implementations for reference

The AI wrote the code, the human tested it on real hardware and provided feedback. Multiple iterations were needed to get WOL, game closing, CEC TV control, and suspend working correctly on SteamOS.

## License

MIT
