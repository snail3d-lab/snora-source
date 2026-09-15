# sNora — source license

A complete AI-video setup: an iOS client, the Python backend that runs a
render farm queue, and an optional Discord bot so your community can request
clips by @mentioning the bot in your server.

## Pieces
| File | What it does |
|---|---|
| `Snora/` | the SwiftUI iOS app — job queue, photo/audio capture, reel player, background polling, "signal" design system (xcodegen: edit `project.yml`, run `xcodegen generate`, never hand-edit the `.xcodeproj`) |
| `backend/sora_service.py` | the render-farm service (stdlib-only Python): job queue, reference-image prep, prompt composition, model calls, watermark, delivery |
| `backend/discord_bot.py` | the Discord bot (@sNora → queue → clip back in chat) |
| `backend/sora_config.example.json` | config template — copy to `sora_config.json` |
| `backend/shack_manifest.example.json` | **your world** — copy to `shack_manifest.json` and describe your character, sets, and reference photos |

## What hardware it needs (read first)
Rendering runs **locally on your own Apple Silicon Mac** — no cloud, no
subscription. The video models are ~60 GB of 8-bit weights that must live in
unified memory:

| Tier | Spec | What to expect |
|---|---|---|
| Minimum | M-series **Max or Ultra with 96 GB** unified memory (e.g. M3 Max 96 GB, Mac Studio M2 Ultra) | works; 5 s clips in roughly 3-6 min |
| Recommended | **128 GB** Max/Ultra (M4/M5 Max 128 GB, Ultra 128-192 GB) | the author's class of box; ~2 min per clip |
| Not supported | any Mac with **64 GB or less** (incl. M3 32 GB) | the model physically does not fit |
| Also | ~150 GB free disk (weights + clips), macOS 14+, Python 3.9+ | weights download once, then it's all local |

The iOS app and Discord bot have no hardware needs — they're just remotes.

## 1. Backend
```bash
cp backend/sora_config.example.json backend/sora_config.json
cp backend/shack_manifest.example.json backend/shack_manifest.json
# fill both in, then:
python3 backend/sora_service.py
```

- **`sora_config.json`** — `sora_secret` authenticates every request (the app
  sends it as a bearer token); `snail_api_base`/`snail_api_key`/`snail_model`
  point at ANY OpenAI-compatible chat endpoint used to rewrite short user
  prompts into rich scene prompts. Bring your own keys and GPU — the render
  spend is yours.
- **`shack_manifest.json`** — the reference library: a recurring `character`,
  your `sets`, and `assets` (photos the model can cast). The prompt builder
  and the Discord bot both read it; everything they "know" about your world
  comes from this file. Mark an asset `"discord_ok": true` only if your
  community may render it; unmarked assets stay private.
- **Reference-image prep** (crop/zoom for best identity fidelity) shells out
  to a Python with Pillow: set `SNORA_PREP_PY=/path/to/python3` or it falls
  back to `python3`.
- `sora_config.json` and `shack_manifest.json` are gitignored on purpose.
  Never commit them.

## 2. iOS app
Set `API.token` in `Snora/API.swift` to the secret you chose, point the
backend hosts in `Snora/StudioAPI.swift` / `Snora/AssistAPI.swift` at your
service, set `DEVELOPMENT_TEAM` in `project.yml` to YOUR Apple team ID, then
`xcodegen generate` and run from Xcode with your signing.

## 3. Discord bot (optional)
Members @-mention the bot with an idea (plus optional photo/audio
attachments) and get the finished clip back in the channel, with queue
position updates.

**Setup**
1. [discord.com/developers](https://discord.com/developers/applications) →
   New Application → Bot → **enable MESSAGE CONTENT INTENT** → copy the bot
   token into a file (`chmod 600`). Invite the bot to your server with
   "Manage Messages".
2. `pip3 install discord.py pillow-heif`
3. In `sora_config.json`, the `discord` block: `token_file`, `service_url`
   (default `http://127.0.0.1:7789`), `allowed_guild_ids` (server IDs), and
   `snora_token` (a token with `s` scope for the companion in-app bot, if
   you run one).
4. Run it beside the service, e.g. macOS launchd:
```bash
cat > ~/Library/LaunchAgents/com.example.snorabot.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.snorabot</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>/path/to/backend/discord_bot.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/snorabot.log</string>
  <key>StandardErrorPath</key><string>/tmp/snorabot.log</string>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/com.example.snorabot.plist
```
(linux: any systemd unit works too — the bot is a plain python process.)

**Behavior worth knowing**
- @sNora text only → text-to-video. + photo → the photo becomes the shot's
  first frame or identity reference. + audio → becomes the character's voice
  (needs a face in play). HEIC/audio formats convert automatically.
- Aspect follows the member's picture when one is in play; otherwise wide.
- Members see their queue position and an ETA; the clip posts to their thread
  when rendered.
- `nanny: true` adds a content gate (no sexual content, no real-person
  smears, PG-13 language) with every rejection logged beside the bot.
- `nanny_off_guilds: ["<server-id>"]` runs those servers with the gate off
  (adults-only private channels you control); the illegal-content floor
  (anything involving minors) is enforced everywhere, no override.
- `await_approval: true` holds Discord jobs at YOUR approval gate — nothing
  hits the GPU until you tap approve in the sNora app.
- Per-member limits: one active job, min spacing between requests, and each
  member is rate-limited as a separate "IP" farm-side.
- Discord jobs are tagged `source: "discord"` + username in the queue API, so
  your app can badge them.

## Notes
- Every credential, path, hostname, and persona string from the author's own
  deployment is a placeholder or generic on purpose — the prompts describe
  YOUR world once you fill in the manifest.
- License: see `LICENSE.md`.
