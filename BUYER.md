# sNora — what you bought

An iOS client for an AI video render farm, plus the Python backend that runs
one. Job queue, photo/audio capture, reel player, background polling, and a
"signal" design system (retro-broadcast palette, mono labels, scanline
background) you're free to reuse.

## Pieces
- `Snora/` — the SwiftUI iOS app (xcodegen: edit `project.yml`, then
  `xcodegen generate`; never hand-edit the generated `.xcodeproj`)
- `backend/sora_service.py` — the render-farm service (stdlib-only Python)

## Run it

**Backend:**
```bash
cp backend/sora_config.example.json backend/sora_config.json
# fill in your own secret + your own model/gateway keys, then:
python3 backend/sora_service.py
```

**App:** set `API.token` in `Snora/API.swift` to the secret you chose, set the
backend hosts in `Snora/StudioAPI.swift` / `Snora/AssistAPI.swift`, then
`xcodegen generate` and run from Xcode with your own signing team.

## Notes
- Every credential from the author's deployment is a placeholder. **Bring your
  own video-model keys and compute** — this app is a client; the render spend
  is yours, not the author's.
- `sora_config.json` is gitignored on purpose. Never commit it.
- License: see `LICENSE.md`.
