#!/usr/bin/env python3
"""sNora render service — public video generation for snail3d.com.

Bridges the Cloudflare worker (public API on snail3d.com) to the local render
farm. Pure stdlib; runs under launchd on :7789, exposed publicly via the
cloudflared tunnel (sora.snail3d.com).

Endpoints:
  GET  /health      -> {"ok": true}            (no auth — for tunnel checks)
  POST /jobs        -> {id, quality, backend}  (X-Sora-Secret required)
  GET  /jobs        -> {jobs: [...]}           (X-Sora-Secret required)
  GET  /jobs/<id>   -> {id, prompt, status}    (X-Sora-Secret required)
  GET  /jobs/<id>/refs     -> {refs:[{i,name,bytes}]}   (X-Sora-Secret required)
  GET  /jobs/<id>/ref/<i>  -> one reference picture's bytes (X-Sora-Secret required)
  POST /clear_queue        -> drop every waiting job record (X-Sora-Secret required)
  POST /jobs/<id>/delete   -> guarded sweep -> {ok, removed, kept}
  POST /jobs/<id>/approve  -> awaiting_approval -> queued, the public job
  POST /approve_all        -> {ok, approved: n}  (both X-Sora-Secret required)

Flow (vpipe backend, default):
  worker -> POST /jobs {prompt, quality}
    -> job stored as "queued" (or "awaiting_approval" when await_approval is
       true — held out of the render lane until approve flips it to queued)
  renderer thread (single, strictly sequential — ONE GPU render at a time):
    queued -> "rendering" -> vpipe CLI subprocess (turbo LoRA pipeline,
    native Metal, MiniMax H3 FL2VA 8-bit) -> mp4 lands in
    ~/vpipe/sandbox/sora/<id>.mp4 -> uploaded to worker -> R2 -> "done"

Quality map (all 124 frames ≈ 5s, square, turbo LoRA v4-600-ema):
  fast     480x480 4 steps   (~2 min on M5 Max)
  standard 704x704 6 steps   (~4 min)
  best     960x960 8 steps   (~9 min)

Fallback: set "backend": "comfy" in sora_config.json to restore the old
ComfyUI path (immediate submit + /history polling).
"""
import base64
import hashlib
import hmac
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # h3_prompt.py sits beside this file
from h3_prompt import (FIRE_RULE, CONTRACT, wants_fire, fire_slip_in,           # noqa: E402
                       fire_corrective, soundscape_for, music_for, SOUND_SPOKEN_VOICE,
                       negative_for, clean_body)
import sora_schedule as SCHED                                                 # noqa: E402

CONFIG = {
    "port": 7789,
    "secret": os.environ.get("SORA_SECRET", ""),
    "worker_base": "https://snail3d.com",
    "studio_url": os.environ.get("SNAILSTUDIO_URL", "http://100.120.41.45:7778"),
    "studio_generated": os.environ.get("SNAILSTUDIO_GENERATED",
                                         os.path.expanduser("~/SnailStudio/generated")),
    "backend": "vpipe",
    # Qwen-Image stills take a REAL negative prompt (CFG > 1). H3 video cannot
    # (CFG-distilled) — see h3_prompt.py. Off by default; flip fire_negative_stills.
    "fire_negative_stills": False,
    "fire_negative_prompt": None,
    "fire_negative_cfg": 3.0,
    # vpipe backend
    "vpipe_bin": "/Applications/Vpipe Manager.app/Contents/Helpers/vpipe",
    "vpipe_cwd": f"{HOME}/vpipe",
    "vpipe_pipeline": "pipelines/minimax-h3-text-to-video-turbo.vpipeline",
    "vpipe_sandbox": f"{HOME}/vpipe/sandbox",
    "vpipe_model": "local/MiniMax-H3-FL2VA-8bit",
    "frames": 124,         # 17n+5 grid (~5.2s @24fps)
    "qualities": {
        "fast":     {"steps": 4},
        "standard": {"steps": 6},
        "best":     {"steps": 8},
    },
    "aspects": {
        "square":  {"width": 704, "height": 704},
        "portrait": {"width": 480, "height": 704},
        "portrait-hd": {"width": 544, "height": 992},
        "vertical": {"width": 704, "height": 1280},   # phone-first 9:16 (the Jambo default)
        "wide":    {"width": 832, "height": 480},
    },
    "durations": {3: 73, 4: 90, 5: 124, 8: 192, 10: 243, 15: 362},   # seconds -> frames (17n+5)
    "vpipe_pipeline_fl2va": "pipelines/minimax-h3-first-last-to-video.vpipeline",
    "vpipe_pipeline_be2v": "pipelines/minimax-h3-begin-end-to-video.vpipeline",
    "vpipe_pipeline_ref2va": "pipelines/minimax-h3-ref2va-minimal.vpipeline",
    "vpipe_pipeline_i2v": "pipelines/minimax-h3-i2v-first.vpipeline",
    # comfy fallback
    "comfy_url": "http://127.0.0.1:8188",
    "comfy_output": f"{HOME}/ComfyUI/output",
    "workflow_template": os.path.join(HERE, "comfy_t2va_template.json"),
    # limits
    "max_active_per_ip": 2,      # rendering jobs per IP
    "max_pending_per_ip": 40,    # queued+rendering per IP (overnight runs)
    "max_total_pending": 60,     # global queue cap
    "prompt_max_chars": 2000,
    "stale_secs": 5400,          # rendering longer than 90 min -> failed
}


def _load_config():
    try:
        with open(os.path.join(HERE, "sora_config.json")) as f:
            CONFIG.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass


_load_config()

STATE_FILE = os.path.join(HERE, "sora_state.json")
_lock = threading.Lock()


def _load_state():
    try:
        return json.load(open(STATE_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"jobs": {}}


def _save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


# ══ the chron lane ══════════════════════════════════════════════════════
# Schedules produce ordinary queued jobs — same queue, same order, no priority.
# Delivery happens in the post lane once the clip is on R2.

def _enqueue_payload(data, source, ip="sched"):
    """Create a queued job straight from a payload dict.

    The scheduler's door into the shared queue. It skips the public /jobs
    handler's per-IP throttling (a chron is the operator's own traffic) but
    changes nothing about ORDER: the job lands at the back of the queue exactly
    like a Discord ask, so a chron can never jump ahead of a live request.
    """
    image_path, key_paths, voice_path, end_image_path = None, [], None, None
    mf = _mf(None)
    if data.get("image"):
        image_path = _save_dataurl(data["image"], "img")
    if data.get("image_key"):
        for a in mf.get("assets", []):
            if a["key"] == data["image_key"]:
                image_path = a["file"]
                break
    if not image_path and data.get("image_path"):
        # an explicit photo the schedule chose (the drop-in dog folder)
        cand = str(data["image_path"])
        if os.path.isfile(cand):
            image_path = cand
    if data.get("voice_key"):
        v = _mf_voice(mf)
        if data["voice_key"] == v.get("key"):
            voice_path = v.get("file")
    duration = int(data.get("duration") or 5)
    if duration not in CONFIG["durations"]:
        duration = 5
    quality = (data.get("quality") or "standard").lower()
    if quality not in CONFIG["qualities"]:
        quality = "standard"
    aspect = (data.get("aspect") or "vertical").lower()
    if aspect not in CONFIG["aspects"]:
        aspect = "vertical"
    prompt = str(data.get("prompt") or "")[:CONFIG["prompt_max_chars"]]
    with _lock:
        state = _load_state()
        job_id = time.strftime("%Y%m%d") + "_" + hashlib.sha1(
            f"{prompt}{time.time()}".encode()).hexdigest()[:8]
        job = {
            "id": job_id, "prompt": prompt,
            "quality": quality, "aspect": aspect, "duration": duration,
            "target_ratio": None, "source": str(source)[:60],
            "lang": data.get("lang"),
            # a sung shot: the render rewrites the scaffold's spoken clauses so
            # the subject sings in `sing_lang` instead of speaking English
            "sing": bool(data.get("sing")),
            "sing_lang": str(data.get("sing_lang") or "")[:40] or None,
            "has_image": bool(image_path), "has_voice": bool(voice_path),
            "soundscape": str(data.get("soundscape") or "")[:600],
            "music": str(data.get("music") or "")[:300],
            "ref_mode": data.get("ref_mode") or "scene",
            "zoom": float(data.get("zoom") or 1.0),
            "focus": data.get("focus") or "center",
            "image_path": image_path or (key_paths[0] if key_paths else None),
            "image_paths": key_paths,
            "end_image_path": end_image_path,
            "voice_path": voice_path,
            "backend": CONFIG["backend"],
            "status": "queued",
            "ip": ip,
            "created": time.time(),
            "seed": random.getrandbits(63),
        }
        state["jobs"][job_id] = job
        _save_state(state)
    print(f"[sched] queued {job_id} ({quality}/{aspect}/{duration}s) source={source}", flush=True)
    return job_id


def _load_schedules():
    with _lock:
        state = _load_state()
    return state.setdefault("schedules", [])


def _save_schedules(store):
    with _lock:
        state = _load_state()
        state["schedules"] = store
        _save_state(state)


def _deliver_clip(job):
    """Post a finished scheduled clip to its Discord channels. The app target
    needs nothing here — the clip is already in the shared gallery."""
    store = _load_schedules()
    for s in store:
        targets = s.get("targets") or []
        if not targets or s.get("delivered"):
            continue
        if job["id"] not in (s.get("pending_job"), s.get("last_job_id")):
            continue
        label = s.get("last_song_title") or s.get("name")
        hits, ok = [], True
        for t in targets:
            if t.get("kind") != "discord":
                continue                       # app targets need no upload
            try:
                SCHED.post_clip(t["channel_id"], job["clip_path"],
                                content=f"🎶 {label} — {s['name']}", cfg=CONFIG)
                hits.append(t.get("channel_name") or t["channel_id"])
            except Exception as e:
                ok = False
                print(f"[sched] {s['id']}: delivery to {t.get('channel_id')} failed: {e}",
                      flush=True)
        if ok:
            s["delivered"] = True
            s["delivered_at"] = time.time()
            s["delivered_to"] = hits
            print(f"[sched] {s['id']} {s['name']}: delivered {job['id']} to {hits}",
                  flush=True)
    _save_schedules(store)


def _sched_loop():
    """Tick the schedules every 30s. A schedule only ever ENQUEUES."""
    while True:
        try:
            store = _load_schedules()
            SCHED.tick({"schedules": store}, _enqueue_payload,
                       save=_save_schedules, log=lambda m: print(m, flush=True))
        except Exception as e:
            print(f"[sched] tick error: {e}", flush=True)
        time.sleep(30)


# ── prompt scaffolding ────────────────────────────────────────────────

# The H3 audio branch is text-conditioned. A bare one-liner leaves the
# soundscape unconditioned and it hallucinates a synthetic noise bed
# (measured: 26x noisier spectrum than the same scene with an explicit
# soundscape section). Always wrap prompts in the T2VA formula.
MUSIC_WORDS = re.compile(r"\b(music|song|score|underscore|soundtrack|melody)\b", re.I)

# A quoted line in the prompt is spoken intent: the audio branch must be
# told to deliver it, never told "nobody speaks" (that fight is the garble).
SPOKEN_RE = re.compile(r'"([^"]{3,200})"')
D_TAG_RE = re.compile(r"<d>\s*(?:\[[^\]]*\])?\s*(.*?)</d>", re.I | re.S)


def _has_dialogue(prompt):
    return bool(SPOKEN_RE.search(prompt) or D_TAG_RE.search(prompt))


def spoken_words(prompt):
    """Countable words across every quoted / <d>-tagged spoken line.
    Single-letter words ("a", "I") carry almost no speakable time and are the
    first thing whisper loses to garble, so they don't count against budget."""
    total = 0
    for ln in SPOKEN_RE.findall(prompt) + D_TAG_RE.findall(prompt):
        total += sum(1 for w in ln.split() if len(re.sub(r"[^\w']", "", w)) > 1)
    return total


def word_budget(d):
    """Max words H3 delivers cleanly in d seconds — measured ≈2.2/sec."""
    return max(4, round(d * 2.2))


SPOKEN_CLAUSE = ("The character delivers the quoted line clearly in natural English, "
                 "one clean breath, lips moving in sync with every word; "
                 "every other moment carries only the scene's own sound.")

def _scaffold(prompt, soundscape=None, music=None):
    if _has_dialogue(prompt):
        body = f"{prompt} {SPOKEN_CLAUSE}"
        sound = SOUND_SPOKEN_VOICE + soundscape_for(soundscape)
    else:
        body = f"{prompt} Nobody speaks — the characters make no words, only natural reactions."
        sound = soundscape_for(soundscape)
    return (f"integrated_multimodal_description: [Shot 1] {body}\n"
            f"overall_soundscape: {sound}\n"
            f"non_diegetic_music: {music_for(music)}")
def _upload_to_worker(job_id, clip_path):
    url = f"{CONFIG['worker_base']}/api/sora/upload/{job_id}"
    data = open(clip_path, "rb").read()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "X-Sora-Secret": CONFIG["secret"],
        "Content-Type": "video/mp4",
        # CF WAF on the zone 1010s non-browser UAs; a browser UA passes.
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.status == 200


def _upload_still_to_worker(still_id, data_bytes, ctype="image/png"):
    url = f"{CONFIG['worker_base']}/api/sora/upload_still/{still_id}"
    req = urllib.request.Request(url, data=data_bytes, method="POST", headers={
        "X-Sora-Secret": CONFIG["secret"],
        "Content-Type": ctype,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    })
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.status == 200


def _ffmpeg_bin():
    """launchd agents run with a thin PATH — never trust `ffmpeg` to resolve."""
    for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.exists(p):
            return p
    return "ffmpeg"


def _forget_studio_still(ref):
    """Jambo stills belong to the promo bucket on the website, not the studio's
    own gallery. Once the picture is safely in R2, drop the studio copy so it
    never shows up in the sNora app's reel (the studio prunes missing files
    from its index automatically).

    A 1280px JPEG is ~10x smaller than the raw PNG, so promos stay light."""
    try:
        name = os.path.basename(str(ref))
        p = os.path.join(CONFIG["studio_generated"], name)
        if os.path.isfile(p):
            os.remove(p)
    except Exception as e:
        print(f"[jambo] could not remove the studio copy: {e}", flush=True)


def _shrink_still(png_bytes, max_side=1280, quality=4):
    """Photos are data-expensive to send and view. A 1280px JPEG is ~10x
    smaller than the raw PNG and still looks right on a phone. Returns None
    on failure so the caller keeps the PNG."""
    tmpdir = os.path.join(CONFIG["vpipe_sandbox"], "uploads")
    os.makedirs(tmpdir, exist_ok=True)
    stem = os.path.join(tmpdir, f"still_{os.urandom(5).hex()}")
    inp, outp = stem + ".png", stem + ".jpg"
    try:
        with open(inp, "wb") as f:
            f.write(png_bytes)
        expr = f"scale={max_side}:{max_side}:force_original_aspect_ratio=decrease"
        r = subprocess.run([_ffmpeg_bin(), "-y", "-loglevel", "error", "-i", inp,
                            "-vf", expr, "-q:v", str(quality), outp],
                           capture_output=True, timeout=120)
        if r.returncode != 0 or not os.path.exists(outp) or os.path.getsize(outp) < 1000:
            return None
        return open(outp, "rb").read()
    except Exception:
        return None
    finally:
        for p in (inp, outp):
            try: os.remove(p)
            except Exception: pass


# ── still lane: SnailStudio (Qwen-Image) on this box ─────────────────
# One GPU, one picture: the studio's Qwen worker is serial, so two "give me 2
# variants" requests must not race each other into an OOM.
_still_lock = threading.Lock()


def _studio_generate(payload):
    """POST a Qwen still job to SnailStudio. Blocking — the worker holds the call
    for the whole render, so callers must be ready to wait."""
    req = urllib.request.Request(
        CONFIG["studio_url"].rstrip("/") + "/api/editor/ai/generate-qwen",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as resp:
        return json.loads(resp.read().decode())


def _studio_fetch(ref):
    req = urllib.request.Request(
        CONFIG["studio_url"].rstrip("/") + ref,
        headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def _builder_still_plan(message):
    """Turn a plain-language idea into ONE Qwen image prompt (Jambo lane)."""
    base = CONFIG.get("snail_api_base")
    key = CONFIG.get("snail_api_key")
    if not base or not key:
        raise RuntimeError("snail-api key not configured")
    msgs = [{"role": "system", "content": _jambo_still_system() + FIRE_RULE},
            {"role": "user", "content": str(message)[:2000]}]
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps({"model": CONFIG.get("snail_model", "snail-top"),
                         "messages": msgs, "max_tokens": 700, "temperature": 0.6}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        d = json.loads(resp.read().decode())
    content = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
    m = re.search(r"\{.*\}", content, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {"reply": content[:300], "prompt": str(message)[:600], "aspect": "portrait"}


def _still_record(sid, prompt, aspect, quality, transparent, source, ip, status="rendering"):
    return {"id": sid, "prompt": prompt, "aspect": aspect, "quality": quality,
            "transparent": bool(transparent), "source": source, "r2": False,
            "ext": None, "created": time.time(), "ip": ip, "status": status,
            "seed": None, "width": None, "height": None, "mode": None, "reply": ""}


def _still_render(sid, prompt, message, aspect, quality, transparent, images, source, ip):
    """One Qwen still, in a background thread — the web request returns at once.
    Stills take ~2 minutes, far longer than a Cloudflare worker proxy will wait,
    so the page polls /stills and the cooking card shows it working."""
    reply = ""
    rec = {}
    try:
        if not prompt and message:
            plan = _builder_still_plan(message)
            prompt = (plan.get("prompt") or message).strip()
            reply = plan.get("reply") or ""
            if plan.get("aspect") in ("portrait", "square", "wide"):
                aspect = plan["aspect"]
            with _lock:
                st = _load_state()
                s = st.setdefault("stills", {}).setdefault(sid, {})
                s.update({"prompt": prompt, "aspect": aspect, "reply": reply})
                _save_state(st)
        payload = {"prompt": prompt, "quality": quality,
                   "steps": {"fast": 24, "standard": 40, "best": 60}[quality],
                   "seed": -1, "use_kv_cache": True, "transparent": transparent,
                   "aspect": aspect}
        if images:
            payload["images"] = images[:3]
        # The video lane cannot do this (H3 is CFG-distilled); Qwen-Image can, but
        # only with true_cfg_scale > 1. Off by default — see h3_prompt.negative_for.
        neg, cfg = negative_for(message or prompt,
                                enabled=bool(CONFIG.get("fire_negative_stills")),
                                negative=CONFIG.get("fire_negative_prompt"),
                                cfg=CONFIG.get("fire_negative_cfg") or 3.0)
        if neg:
            payload["negative_prompt"] = neg
            payload["true_cfg_scale"] = cfg
            print(f"[sora] still {sid}: CFG {cfg} with a fireless negative prompt", flush=True)
        with _still_lock:
            res = _studio_generate(payload)
        ref = res.get("image") or ""
        if not ref:
            raise RuntimeError(res.get("error") or "the still engine returned nothing")
        png = _studio_fetch(ref)
        ext, blob, ctype = "png", png, "image/png"
        if not transparent:
            small = _shrink_still(png)
            if small:
                ext, blob, ctype = "jpg", small, "image/jpeg"
        r2ok = False
        try:
            r2ok = _upload_still_to_worker(sid, blob, ctype)
        except Exception as e:
            print(f"[jambo] still upload failed: {e}", flush=True)
        if r2ok and not transparent:
            _forget_studio_still(ref)   # promos live in their own bucket
        rec = {"ext": ext, "r2": r2ok, "seed": res.get("seed"),
               "width": res.get("width"), "height": res.get("height"),
               "mode": res.get("mode"), "status": "done",
               "transparent": bool(transparent), "reply": reply,
               "studio_name": os.path.basename(str(ref))}
    except Exception as e:
        print(f"[jambo] still {sid} failed: {e}", flush=True)
        rec = {"status": "failed", "error": str(e)[:200]}
    with _lock:
        st = _load_state()
        s = st.setdefault("stills", {}).setdefault(sid, {"id": sid, "prompt": prompt,
                                                         "aspect": aspect,
                                                         "quality": quality,
                                                         "source": source,
                                                         "created": time.time(),
                                                         "transparent": bool(transparent)})
        s.update(rec)
        _save_state(st)


def _still_create(data, ip):
    """Queue 1-4 promotional photos. Each renders in the background and lands in R2,
    so the HTTP call returns in milliseconds (a Cloudflare worker proxy cannot wait
    two minutes for a render)."""
    site = str(data.get("site") or "").strip().lower()
    prompt = (data.get("prompt") or "").strip()
    message = (data.get("message") or "").strip()
    if not prompt and not message:
        return {"error": "prompt is required"}, 400
    if max(len(prompt), len(message)) > 2000:
        return {"error": "keep it under 2000 characters"}, 400
    aspect = data.get("aspect") if data.get("aspect") in ("portrait", "square", "wide") else "portrait"
    quality = data.get("quality") if data.get("quality") in ("fast", "standard", "best") else "standard"
    try:
        n = int(data.get("n") or 1)
    except Exception:
        n = 1
    n = min(4, max(1, n))
    transparent = bool(data.get("transparent"))
    images = []
    for d in (data.get("images") or [])[:3]:
        if isinstance(d, str) and d:
            images.append(d)
    if isinstance(data.get("image"), str) and data["image"]:
        images.insert(0, data["image"])
    source = "jambo" if site == "jambo" else None

    made = []
    for i in range(n):
        sid = time.strftime("%Y%m%d") + "_st" + hashlib.sha1(
            f"{prompt}{message}{time.time()}{i}".encode()).hexdigest()[:8]
        with _lock:
            st = _load_state()
            st.setdefault("stills", {})[sid] = _still_record(
                sid, prompt or message, aspect, quality, transparent, source, ip)
            _save_state(st)
        threading.Thread(target=_still_render,
                         args=(sid, prompt, message, aspect, quality, transparent,
                               images, source, ip), daemon=True).start()
        made.append({"id": sid, "url": f"/api/sora/still/{sid}",
                     "aspect": aspect, "status": "rendering"})
    return {"ok": True, "stills": made, "queued": n}, 201


def _save_dataurl(dataurl, prefix):
    """Decode a data: URL (image/voice from the web form) into the sandbox.
    Returns abs path. Raises ValueError on bad input."""
    m = re.match(r"data:(image|audio)/(png|jpeg|jpg|webp|mpeg|mp3|wav|m4a|x-m4a|mp4);base64,(.+)", dataurl)
    if not m:
        raise ValueError("image/voice must be a base64 data URL (png/jpg/webp or mp3/wav/m4a)")
    ext = {"mpeg": "mp3", "x-m4a": "m4a"}.get(m.group(2), m.group(2))
    updir = os.path.join(CONFIG["vpipe_sandbox"], "uploads")
    os.makedirs(updir, exist_ok=True)
    # ms + random suffix: two uploads in the same second must not collide
    path = os.path.join(updir, f"{prefix}_{int(time.time()*1000)}_{os.urandom(3).hex()}.{ext}")
    with open(path, "wb") as f:
        f.write(base64.b64decode(m.group(3)))
    if os.path.getsize(path) < 500:
        raise ValueError("uploaded file looks empty")
    return path


def _media_ratio(path):
    """Exact display ratio (Fraction) of an image/video via ffprobe, or None."""
    from fractions import Fraction
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", path],
            capture_output=True, text=True, timeout=30)
        w, h = (int(x) for x in r.stdout.strip().split("x")[:2])
        if w < 2 or h < 2:
            return None
        return Fraction(w, h).limit_denominator(1000)
    except Exception:
        return None


def _auto_aspect(ratio):
    """The render bucket whose ratio is closest to the input's (log distance)."""
    import math
    return min(CONFIG["aspects"].items(),
               key=lambda kv: abs(math.log(float(ratio)) - math.log(
                   kv[1]["width"] / kv[1]["height"])))[0]


def _crop_to_ratio(job, clip_path):
    """Center-crop the finished clip so its ratio EXACTLY matches the input
    picture the job was anchored to (only jobs submitted with aspect 'auto').
    Runs before the upload, so Discord and the app both get the exact clip."""
    target = job.get("target_ratio")
    if not target or ":" not in str(target):
        return clip_path
    from fractions import Fraction
    try:
        tw, th = (int(x) for x in str(target).split(":"))
        want = Fraction(tw, th)
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
             clip_path], capture_output=True, text=True, timeout=30)
        W, H = (int(x) for x in r.stdout.strip().split("x")[:2])
        if abs(float(Fraction(W, H)) - float(want)) < 0.005:
            return clip_path  # already there
        if float(Fraction(W, H)) > float(want):
            newW, newH = int(H * float(want)) // 2 * 2, H       # too wide
        else:
            newW, newH = W, int(W / float(want)) // 2 * 2       # too tall
        if newW < 16 or newH < 16:
            return clip_path
        out = clip_path[:-4] + "_exact.mp4"
        rr = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", clip_path, "-vf",
             f"crop={newW}:{newH}:{(W - newW) // 2}:{(H - newH) // 2}",
             "-c:v", "libx264", "-crf", "17", "-preset", "fast",
             "-pix_fmt", "yuv420p", "-c:a", "copy", out], timeout=600)
        if rr.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 5000:
            os.replace(out, clip_path)
            print(f"[sora] {job.get('id')}: cropped to exact input ratio "
                  f"{newW}x{newH}", flush=True)
    except Exception as e:
        print(f"[sora] {job.get('id')}: ratio crop skipped: {e}", flush=True)
    return clip_path


def _to_wav(path):
    """H3 voice refs want wav and choke past ~15s — convert/trim via ffmpeg."""
    if path.endswith(".wav"):
        return path
    out = path.rsplit(".", 1)[0] + "_c.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-t", "15",
             "-ar", "32000", "-ac", "1", out], check=True)
        return out
    except Exception as e:
        print(f"[sora] voice convert failed, using raw: {e}", flush=True)
        return path


def _silent_wav(job):
    """Silent 32kHz mono room tone — voiceless ref2va (identity mode) renders
    nothing without an audio ref; see the note in _render's ref assembly."""
    out = os.path.join(CONFIG["vpipe_sandbox"], "uploads", f"silence_{job['id']}.wav")
    if not os.path.exists(out):
        secs = min(15, int(job.get("duration") or 5))
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "anullsrc=r=32000:cl=mono", "-t", str(secs), out], check=True)
    return out

def _scaffold_fl2va(prompt, dur, soundscape=None, music=None):
    """FL2VA with first+last = same image: declare both alignment slots."""
    end = f"{max(0.1, dur - 0.17):.2f}"
    head = (f"How the reference pictures align with the target video: "
            f"Picture 1 (from Shot 1) aligns with the 0.00-second mark; "
            f"Picture 2 (from Shot 1) aligns with the {end}-second mark.\n")
    if _has_dialogue(prompt):
        body = f"{prompt} {SPOKEN_CLAUSE}"
        sound = SOUND_SPOKEN_VOICE + soundscape_for(soundscape)
    else:
        body = f"{prompt} Nobody speaks — the characters make no words, only natural reactions."
        sound = soundscape_for(soundscape)
    return (head +
            f"integrated_multimodal_description: [Shot 1] {body}\n"
            f"overall_soundscape: {sound}\n"
            f"non_diegetic_music: {music_for(music)}")
def _scaffold_ref2va(prompt, voiceless=False, soundscape=None, music=None):
    """Ref2VA: the references carry the subject/voice, prompt carries the action.
    voiceless (identity mode) renders against a SILENT wav — never claim that
    wav is the voice, the model garbles trying to clone nothing."""
    if voiceless:
        # say nothing about voices when the prompt has no line — claiming a
        # voice there is what made the model babble gibberish to fill it
        if _has_dialogue(prompt):
            speech = ("The character delivers the quoted line clearly in natural English, "
                      "one clean breath, lips moving in sync with every word.\n")
            sound = "overall_soundscape: " + SOUND_SPOKEN_VOICE + soundscape_for(soundscape)
        else:
            speech = ("Nobody speaks — the characters make no words, only natural reactions. "
                      "No voice of any kind, no background chatter, no babble.\n")
            sound = "overall_soundscape: " + soundscape_for(soundscape)
        return (f"integrated_multimodal_description: [Shot 1] "
                f"The subject shown in <Picture 1>, {prompt}\n"
                f"{speech}{sound}\n"
                f"non_diegetic_music: {music_for(music)}")
    amb = " ".join(str(soundscape or "").split()) or (
        "natural diegetic sound matching the scene and its action, clean and unprocessed")
    return (f"integrated_multimodal_description: [Shot 1] "
            f"The subject shown in <Picture 1>, speaking with the voice of <Audio 1>, {prompt}\n"
            f"Any spoken lines are in clear English.\n"
            f"overall_soundscape: {amb}\n"
            f"non_diegetic_music: {music_for(music)}")
def _vpipe_ready():
    return os.path.isdir(
        os.path.join(CONFIG["vpipe_cwd"], "models", *CONFIG["vpipe_model"].split("/")))


def _scaffold_i2v(prompt, soundscape=None, music=None):
    """i2v: one first-frame anchor (the chain/seed image), motion continues from it."""
    if _has_dialogue(prompt):
        body = f"{prompt} {SPOKEN_CLAUSE}"
        sound = SOUND_SPOKEN_VOICE + soundscape_for(soundscape)
    else:
        body = (f"{prompt} Nobody speaks — the characters make no words, only natural "
                f"reactions. No voice of any kind, no background chatter, no babble.")
        sound = soundscape_for(soundscape)
    return (f"For the target video, at 0.00 seconds into the target video, <Picture 1> "
            f"(from [Shot 1]) is fully referenced.\n"
            f"integrated_multimodal_description: [Shot 1] {body}\n"
            f"overall_soundscape: {sound}\n"
            f"non_diegetic_music: {music_for(music)}")
def _extract_last_frame(clip_path, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    r = subprocess.run(["ffmpeg", "-y", "-sseof", "-0.1", "-i", clip_path,
                        "-vframes", "1", out_path], capture_output=True, timeout=120)
    if r.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
        raise RuntimeError(f"last-frame extraction failed: {r.stderr.decode()[-200:]}")
    return out_path


def _render_vpipe(job):
    """Run one render through the vpipe CLI. Returns the clip path."""
    q = CONFIG["qualities"].get(job.get("quality") or "standard",
                                CONFIG["qualities"]["standard"])
    a = CONFIG["aspects"].get(job.get("aspect") or "square",
                              CONFIG["aspects"]["square"])
    frames = CONFIG["durations"].get(int(job.get("duration") or 5), 124)
    img = job.get("image_path")
    vox = job.get("voice_path")
    # prep the reference image: exact canvas AR crop + optional zoom/focus (never stretch)
    if img:
        try:
            img = _prep_image(img, 1024, 1024, 1.0, "center")  # normalize
            img = _prep_image(img, a["width"], a["height"],
                              float(job.get("zoom") or 1.0), job.get("focus") or "center")
        except Exception as e:
            print(f"[sora] {job['id']}: image prep failed, using raw: {e}", flush=True)
    out_rel = f"sora/{job['id']}.mp4"
    out_abs = os.path.join(CONFIG["vpipe_sandbox"], out_rel)
    os.makedirs(os.path.dirname(out_abs), exist_ok=True)

    if vox and not img:
        raise ValueError("voice needs an image reference (audio can't be the only reference)")
    seq = job.get("sequence")
    chain_from = None
    tr = (seq or {}).get("transition", "chain") if seq else "chain"
    if job.get("ref_mode") == "identity":
        tr = "cut"  # identity shots rebuild Eric in a new scene — never chain-anchor
    if vox:
        tr = "cut"  # ref2va VO shots are standalone — no chain anchoring across partitions
    if seq and int(seq.get("index", 0)) > 0 and tr == "chain":
        prev_id = seq.get("prev_id")
        prev_clip = os.path.join(CONFIG["vpipe_sandbox"], f"sora/{prev_id}.mp4") if prev_id else None
        if not prev_clip or not os.path.exists(prev_clip):
            raise RuntimeError(f"chain frame missing: previous clip {prev_id}.mp4 not found")
        chain_from = _extract_last_frame(
            prev_clip, os.path.join(CONFIG["vpipe_sandbox"], "seq", f"{seq['id']}_first_{seq['index']}.png"))
        with _lock:
            _st = _load_state()
        director_note = (_st.get("sequences", {}).get(seq["id"], {}).get("notes", {}) or {}).get(str(seq["index"]))

    identity = job.get("ref_mode") == "identity" and img is not None
    multi = (job.get("image_paths") or [])[:9]
    end_img = job.get("end_image_path")
    if end_img and not os.path.isfile(end_img):
        end_img = None
    if img and end_img:
        # explicit begin+end: the FL2VA model morphs FIRST picture -> LAST
        # picture; the scaffold already declares Picture 1 at 0.00s and
        # Picture 2 at the final mark, so the prompt needs no new format.
        try:
            end_img = _prep_image(end_img, 1024, 1024, 1.0, "center")
            end_img = _prep_image(end_img, a["width"], a["height"], 1.0,
                                  job.get("focus") or "center")
        except Exception as e:
            print(f"[sora] {job['id']}: end-image prep failed, using raw: {e}", flush=True)
        pipeline = CONFIG["vpipe_pipeline_be2v"]
        prompt = _scaffold_fl2va(job["prompt"], int(job.get("duration") or 5),
                                 job.get("soundscape"), job.get("music"))
        extra = [
            "--stage-cfg", f"load-image::url=[\"{img}\"]",
            "--stage-cfg", f"load-image-end::url=[\"{end_img}\"]",
            "--stage-cfg", f"first-frame::width={a['width']}",
            "--stage-cfg", f"first-frame::height={a['height']}",
            "--stage-cfg", f"last-frame::width={a['width']}",
            "--stage-cfg", f"last-frame::height={a['height']}",
        ]
        mode = "be2v(begin+end)"
    elif img and (vox or identity or multi):
        pipeline = CONFIG["vpipe_pipeline_ref2va"]
        prompt = _scaffold_ref2va(job["prompt"], voiceless=not vox,
                                  soundscape=job.get("soundscape"), music=job.get("music"))
        # The ref encoder only builds valid rows [n,96]/[n,32] when an audio ref is
        # present — EVERY voiceless ref2va (identity mode) has failed with
        # "reference rows are not [n, 96] / [n, 32]" since 2026-09-06 (11s, exit 0,
        # no clip). Feed it silent room tone so the shape matches the working
        # image+voice path; photo-only clips are near-silent anyway.
        ref_list = ([img] if img else []) + multi
        if vox:
            ref_list.append(vox)
        else:
            ref_list.append(_silent_wav(job))
        refs = json.dumps(ref_list)
        extra = [
            "--stage-cfg", f"video-ref-encoder::references={refs}",
        ]
        mode = "ref2va(image" + ("+voice)" if vox else " identity-only)")
    elif chain_from is not None:
        pipeline = CONFIG["vpipe_pipeline_i2v"]
        prompt = _scaffold_i2v(job["prompt"], job.get("soundscape"), job.get("music"))
        extra = [
            "--stage-cfg", f"load-image::url=[\"{chain_from}\"]",
            "--stage-cfg", f"first-frame::width={a['width']}",
            "--stage-cfg", f"first-frame::height={a['height']}",
        ]
        mode = "i2v(chain)"
    elif seq is not None and img:
        pipeline = CONFIG["vpipe_pipeline_i2v"]
        prompt = _scaffold_i2v(job["prompt"], job.get("soundscape"), job.get("music"))
        extra = [
            "--stage-cfg", f"load-image::url=[\"{img}\"]",
            "--stage-cfg", f"first-frame::width={a['width']}",
            "--stage-cfg", f"first-frame::height={a['height']}",
        ]
        mode = "i2v(seed)"
    elif img:
        pipeline = CONFIG["vpipe_pipeline_fl2va"]
        prompt = _scaffold_fl2va(job["prompt"], int(job.get("duration") or 5),
                                 job.get("soundscape"), job.get("music"))
        extra = [
            "--stage-cfg", f"load-image::url=[\"{img}\"]",
            "--stage-cfg", f"first-frame::width={a['width']}",
            "--stage-cfg", f"first-frame::height={a['height']}",
            "--stage-cfg", "last-frame::fit=crop",
            "--stage-cfg", f"last-frame::width={a['width']}",
            "--stage-cfg", f"last-frame::height={a['height']}",
        ]
        mode = "fl2va(image)"
    else:
        pipeline = CONFIG["vpipe_pipeline"]
        prompt = _scaffold(job["prompt"], job.get("soundscape"), job.get("music"))
        extra = [
            "--stage-cfg", f"model-select::hf_dir={CONFIG['vpipe_model']}",
        ]
        mode = "t2v"

    # Jambo lane: Swahili is spoken natively from the <d>[Swahili] tag, but every
    # scaffold says "natural English" by default — redirect the language clause here.
    if str(job.get("lang") or "").lower().startswith("sw"):
        prompt = prompt.replace("natural English", "natural Swahili")
    # Chron singing lane: every spoken scaffold says "natural English ... lips
    # moving in sync" and would fight a dog singing a foreign anthem, so the
    # scaffold's spoken clauses are rewritten in place (the same idiom the
    # Swahili redirect above uses).
    if job.get("sing"):
        prompt = prompt.replace("delivers the quoted line", "sings the quoted line")
        prompt = prompt.replace(
            "natural English", str(job.get("sing_lang") or "the language in the tag"))
        prompt = prompt.replace("lips moving in sync with every word",
                                "mouth and throat open and working on every phrase")
        prompt = prompt.replace("one clean breath", "one continuous take")
        prompt = prompt.replace(
            SOUND_SPOKEN_VOICE,
            "one clear non-human voice singing the quoted line, plus ")

    cmd = [
        CONFIG["vpipe_bin"],
        "--launch", pipeline,
        "--stage-cfg", f"text-prompt::text={prompt}",
        "--stage-cfg", f"generate-video::width={a['width']}",
        "--stage-cfg", f"generate-video::height={a['height']}",
        "--stage-cfg", f"generate-video::steps={q['steps']}",
        "--stage-cfg", f"generate-video::frames={frames}",
        "--stage-cfg", f"generate-video::seed={job['seed']}",
        "--stage-cfg", "generate-video::i8_gemm=true",
        "--stage-cfg", f"save-video::output_url={out_abs}",
    ] + extra
    print(f"[sora] {job['id']}: vpipe render {job.get('quality')}/{job.get('aspect')} "
          f"{mode} {a['width']}x{a['height']} {frames}f {q['steps']} steps seed={job['seed']}", flush=True)
    proc = subprocess.Popen(
        cmd, cwd=CONFIG["vpipe_cwd"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    lines = []

    def _drain():
        for ln in proc.stdout:
            ln = ln.rstrip("\n")
            lines.append(ln)
            m = _PROGRESS_RE.match(ln)
            if m:
                _progress_update(job["id"], int(m.group(1)), m.group(2))

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()
    try:
        proc.wait(timeout=CONFIG["stale_secs"])
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("vpipe timed out")
    drainer.join(timeout=5)
    tail = "\n".join(lines[-12:])
    print(f"[sora] {job['id']}: vpipe exit={proc.returncode}\n{tail}", flush=True)
    if proc.returncode != 0 or not os.path.exists(out_abs) \
            or os.path.getsize(out_abs) < 5000:
        # surface the lines that actually say WHY (warnings/errors/crashes),
        # not the memory-plan spam that ends every run
        bad = [l for l in lines if any(
            k in l.lower() for k in ("error", "failed", "warn", "assert", "abort",
                                     "out of memory", "exception", "traceback",
                                     "not [n,", "cannot", "invalid"))]
        reason = " | ".join(bad[-4:])[-400:] if bad else tail[-400:]
        raise RuntimeError(f"vpipe render failed (exit {proc.returncode}): {reason}")
    return _crop_to_ratio(job, out_abs)


# ── live render progress: vpipe streams "[PROGRESS] 52% of 'denoise' completed"
# lines on stdout; the render drainer parses them here so /jobs can expose them
# and the Discord bot can draw a bar. In-memory only — nobody needs it persisted.
_PROGRESS = {}
_PROGRESS_RE = re.compile(r"^\[PROGRESS\] (\d+)% of '([^']+)' completed")


def _progress_update(job_id, pct, stage):
    """pct is PER-STAGE (engine restarts it each phase), so the ETA we derive
    is 'seconds left in this stage' — honest, and all the UI needs. Two samples
    of the same stage give the rate; a stage change resets the clock."""
    now = time.time()
    prev = _PROGRESS.get(job_id) or {}
    eta = None
    if (prev.get("stage") == stage and pct > prev.get("pct", -1)
            and now > prev.get("ts", 0)):
        rate = (pct - prev["pct"]) / (now - prev["ts"])   # % per second
        if rate > 0:
            eta = round((100 - pct) / rate)
    elif prev.get("stage") == stage and prev.get("eta"):
        eta = prev["eta"]          # keep last estimate until a better one lands
    _PROGRESS[job_id] = {"pct": pct, "stage": stage, "ts": now, "eta": eta}

POST_QUEUE = []
_post_wake = threading.Event()


def _post_loop():
    """Background lane: uploads, chain-frame extraction, director notes,
    sequence stitching — everything that is NOT the GPU."""
    while True:
        _post_wake.wait(timeout=5)
        _post_wake.clear()
        while True:
            with _lock:
                item = POST_QUEUE.pop(0) if POST_QUEUE else None
            if not item:
                break
            job, clip = item["job"], item["clip"]
            jid = job["id"]
            status, err = "failed", None
            try:
                done = False
                for _ in range(3):
                    try:
                        done = _upload_to_worker(jid, clip)
                        break
                    except Exception as e:
                        print(f"[sora] {jid}: upload retry: {e}", flush=True)
                        time.sleep(10)
                seq = job.get("sequence") or {}
                idx, count = int(seq.get("index", 0)), int(seq.get("count", 1))
                keep = bool(seq) and idx < count - 1
                if done:
                    status = "done"
                else:
                    status = "failed"  # clip kept in sandbox for manual retry
                    err = "cloud upload failed — the clip is still on the farm"
                # ── chain prep + director note (overlapped with the next render) ──
                if done and keep:
                    try:
                        with _lock:
                            state = _load_state()
                            nxt = next((j for j in state["jobs"].values()
                                        if j.get("sequence", {}).get("id") == seq.get("id")
                                        and int(j.get("sequence", {}).get("index", 0)) == idx + 1), None)
                            goal = state.get("sequences", {}).get(seq.get("id"), {}).get("goal", "")
                        if nxt and job.get("sequence", {}).get("transition", "chain") == "chain":
                            frame = os.path.join(CONFIG["vpipe_sandbox"], "seq",
                                                 f"{seq['id']}_first_{idx+1}.png")
                            _extract_last_frame(clip, frame)
                            note = _director_note(goal, job, nxt, clip)
                        else:
                            note = None
                        with _lock:
                            state = _load_state()
                            sm = state.setdefault("sequences", {}).setdefault(seq.get("id"), {})
                            if nxt:
                                sm.setdefault("frames", {})[str(idx + 1)] = frame
                            if note:
                                sm.setdefault("notes", {})[str(idx + 1)] = note
                            sm.setdefault("post_done", {})[str(idx)] = True
                            _save_state(state)
                    except Exception as e:
                        # chain continuity degraded but the sequence continues
                        print(f"[sora] {jid}: post-chain error (continuing): {e}", flush=True)
                        with _lock:
                            state = _load_state()
                            state.setdefault("sequences", {}).setdefault(seq.get("id"), {}).setdefault(
                                "post_done", {})[str(idx)] = True
                            _save_state(state)
                # ── sequence film stitch (last member) ──
                if done and seq and idx == count - 1:
                    try:
                        _stitch_sequence(seq.get("id"))
                    except Exception as e:
                        print(f"[sora] {jid}: stitch error: {e}", flush=True)
                if done and not keep:
                    try:
                        os.remove(clip)
                    except OSError:
                        pass
            except Exception as e:
                print(f"[sora] {jid}: post error: {e}", flush=True)
                status = "failed"
                err = str(e)[:500]
            with _lock:
                state = _load_state()
                if jid in state["jobs"] and state["jobs"][jid]["status"] == "rendering":
                    state["jobs"][jid]["status"] = status
                    state["jobs"][jid]["finished"] = time.time()
                    if status == "failed" and err and not state["jobs"][jid].get("error"):
                        state["jobs"][jid]["error"] = err
                    _save_state(state)
            # a scheduled clip goes out to its channels as soon as it is on R2 —
            # after the status flip, so a delivery crash can never re-run a render
            if status == "done":
                try:
                    _deliver_clip({"id": jid, "clip_path": clip})
                except Exception as e:
                    print(f"[sched] {jid}: delivery error: {e}", flush=True)
            _post_wake.set()


def _chain_ready(state, job):
    """A chained job may claim the GPU only once the previous shot's post
    work (chain frame + director note) has landed."""
    seq = job.get("sequence") or {}
    if not seq or int(seq.get("index", 0)) == 0:
        return True
    pd = (state.get("sequences", {}).get(seq.get("id"), {}) or {}).get("post_done", {})
    return bool(pd.get(str(int(seq.get("index", 0)) - 1)))


def _prev_failed(state, job):
    seq = job.get("sequence") or {}
    if not seq or int(seq.get("index", 0)) == 0:
        return False
    prev_id = seq.get("prev_id")
    prev = state["jobs"].get(prev_id) if prev_id else None
    return bool(prev) and prev.get("status") == "failed"


def _stitch_sequence(seq_id):
    """Assemble the sequence film with per-transition effects, upload it as a
    pseudo-job, then clean up member clips."""
    with _lock:
        state = _load_state()
    seqmeta = (state.get("sequences", {}) or {}).get(seq_id) or {}
    if seqmeta.get("stitched"):
        return
    members = sorted((j for j in state["jobs"].values()
                      if (j.get("sequence") or {}).get("id") == seq_id),
                     key=lambda j: int((j.get("sequence") or {}).get("index", 0)))
    if not members or any(j.get("status") != "done" for j in members):
        return
    clips = [os.path.join(CONFIG["vpipe_sandbox"], "sora", f"{j['id']}.mp4") for j in members]
    if any(not os.path.exists(c) for c in clips):
        print(f"[sora] {seq_id}: stitch skipped — member clips missing", flush=True)
        return
    durs = []
    for c in clips:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                            "-of", "csv=p=0", c], capture_output=True, text=True)
        try:
            durs.append(float(r.stdout.strip()))
        except ValueError:
            return
    XF = {"dissolve": "dissolve", "fade": "fade", "dip": "fadeblack", "flash": "fadewhite",
          "slide": "slideleft", "wipe": "wipeleft", "whip": "hblur", "glitch": "pixelize",
          "iris": "circleopen"}
    lines = []
    offset = 0.0
    prev_v, prev_a = "[0:v]", "[0:a]"
    n = len(clips)
    for i in range(1, n):
        tr = (members[i].get("sequence") or {}).get("transition", "chain")
        eff = XF.get(tr)
        d = 0.04 if not eff else min(0.5, durs[i - 1] * 0.4, durs[i] * 0.4)
        offset += durs[i - 1] - (d if eff else 0)
        outv, outa = f"[v{i}]", f"[a{i}]"
        lines.append(f"{prev_v}[{i}:v]xfade=transition={eff or 'fade'}:duration={d:.3f}:offset={offset:.3f}{outv}")
        lines.append(f"{prev_a}[{i}:a]acrossfade=d={d:.3f}{outa}")
        prev_v, prev_a = outv, outa
    film = os.path.join(CONFIG["vpipe_sandbox"], "sora", f"{seq_id}_film.mp4")
    cmd = ["ffmpeg", "-y"]
    for c in clips:
        cmd += ["-i", c]
    cmd += ["-filter_complex", ";".join(lines), "-map", prev_v, "-map", prev_a,
            "-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", film]
    print(f"[sora] stitching {seq_id} film ({n} clips)...", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not os.path.exists(film) or os.path.getsize(film) < 10000:
        print(f"[sora] {seq_id}: stitch failed: {r.stderr[-300:]}", flush=True)
        return
    film_id = seq_id.replace("_seq_", "_film_")
    ok = False
    for _ in range(3):
        try:
            ok = _upload_to_worker(film_id, film)
            break
        except Exception as e:
            print(f"[sora] film upload retry: {e}", flush=True)
            time.sleep(10)
    with _lock:
        state = _load_state()
        state.setdefault("sequences", {}).setdefault(seq_id, {})["stitched"] = True
        if ok:
            state["jobs"][film_id] = {
                "id": film_id, "prompt": f"🎬 sequence film — {seqmeta.get('goal', '')[:300]}",
                "status": "done", "backend": "vpipe", "quality": "film",
                "aspect": members[0].get("aspect", "square"),
                "duration": round(sum(durs)),
                "created": time.time(), "ip": members[0].get("ip", ""),
                "finished": time.time()}
            for c in clips:
                try:
                    os.remove(c)
                except OSError:
                    pass
        _save_state(state)
    try:
        os.remove(film)
    except OSError:
        pass
    print(f"[sora] {seq_id}: film uploaded ({film_id}, ok={ok})", flush=True)


def _renderer_loop():
    """GPU lane: up to render_slots concurrent renders (2 by default — the box has
    128GB and one H3 render peaks ~6GB streaming / 33GB preloaded). Chained shots
    stay strictly ordered via _chain_ready. Post work (uploads, chain prep,
    director notes, stitching) runs in the parallel _post_loop lane."""
    while True:
        time.sleep(2)
        try:
            with _lock:
                state = _load_state()
                slots = int(CONFIG.get("render_slots", 2))
                busy = sum(1 for j in state["jobs"].values()
                           if j["status"] == "rendering" and not j.get("posting"))
                if busy >= slots:
                    continue
                # awaiting_approval is deliberately NOT in this bucket: it is a
                # not-runnable status (same bucket as done/failed), so the GPU
                # lane can never claim it. A gated job must not render until
                # POST /jobs/<id>/approve flips it to "queued" — until then it
                # is invisible to this picker, exactly like a finished job.
                queued = [j for j in state["jobs"].values() if j["status"] == "queued"]
                # chained jobs wait for the previous shot's post lane (chain frame + note)
                ready = [j for j in queued if _chain_ready(state, j)]
                if not ready:
                    continue
                broken = [j for j in ready if _prev_failed(state, j)]
                for j in broken:
                    j["status"] = "failed"
                    j["error"] = "the previous shot in this sequence failed, so this one was never rendered"
                    print(f"[sora] {j['id']}: chain broken (previous shot failed)", flush=True)
                if not [j for j in ready if j not in broken]:
                    if broken:
                        _save_state(state)
                    continue
                if broken:
                    _save_state(state)
                job = min([j for j in ready if j not in broken], key=lambda j: j["created"])
                job["status"] = "rendering"
                job["started"] = time.time()
                _save_state(state)
        except Exception as e:
            print(f"[sora] renderer claim error: {e}", flush=True)
            continue
        # render outside the lock
        jid = job["id"]
        try:
            clip = _render_vpipe(job)
            print(f"[sora] {jid}: GPU done, handing to post lane", flush=True)
            with _lock:
                POST_QUEUE.append({"job": job, "clip": clip})
            _post_wake.set()
        except subprocess.TimeoutExpired:
            print(f"[sora] {jid}: vpipe timed out", flush=True)
            with _lock:
                state = _load_state()
                if jid in state["jobs"]:
                    state["jobs"][jid]["status"] = "failed"
                    state["jobs"][jid]["finished"] = time.time()
                    state["jobs"][jid]["error"] = f"render timed out (limit {CONFIG['stale_secs']}s)"
                    _save_state(state)
        except Exception as e:
            print(f"[sora] {jid}: {e}", flush=True)
            with _lock:
                state = _load_state()
                if jid in state["jobs"]:
                    state["jobs"][jid]["status"] = "failed"
                    state["jobs"][jid]["finished"] = time.time()
                    state["jobs"][jid]["error"] = str(e)[:500]
                    _save_state(state)


# ── comfy fallback backend ────────────────────────────────────────────

def _comfy_request(path, payload=None, timeout=60):
    url = CONFIG["comfy_url"] + path
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _submit_comfy(prompt_text, job_id, seed):
    """POST the t2va workflow to ComfyUI. Returns the prompt_id."""
    with open(CONFIG["workflow_template"]) as f:
        wf = json.load(f)
    wf["6"]["inputs"]["prompt"] = _scaffold(prompt_text)
    wf["6"]["inputs"]["width"] = 704
    wf["6"]["inputs"]["height"] = 704
    wf["6"]["inputs"]["length"] = CONFIG["frames"]
    wf["7"]["inputs"]["noise_seed"] = seed
    wf["8"]["inputs"]["steps"] = 8
    wf["15"]["inputs"]["filename_prefix"] = "sora/" + job_id
    r = _comfy_request("/prompt", {"prompt": wf, "client_id": "sora-farm"})
    if "prompt_id" not in r:
        raise RuntimeError(f"comfy /prompt rejected: {json.dumps(r)[:300]}")
    return r["prompt_id"]


def _find_output_video(history):
    """Locate the saved clip in a completed history entry. Returns abs path."""
    for node_out in (history.get("outputs") or {}).values():
        for key in ("videos", "gifs", "images"):
            for item in node_out.get(key, []) if isinstance(node_out, dict) else []:
                sub = item.get("subfolder", "")
                name = item.get("filename", "")
                if name:
                    p = os.path.join(CONFIG["comfy_output"], sub, name)
                    if os.path.exists(p) and os.path.getsize(p) > 5000:
                        return p
    return None


def _comfy_poll_loop():
    while True:
        time.sleep(20)
        try:
            with _lock:
                state = _load_state()
                changed = False
                for jid, job in state["jobs"].items():
                    if job["status"] != "rendering" or job.get("backend") != "comfy":
                        continue
                    pid = job.get("prompt_id")
                    if not pid:
                        job["status"] = "failed"
                        changed = True
                        continue
                    if time.time() - job["created"] > CONFIG["stale_secs"]:
                        job["status"] = "failed"
                        changed = True
                        print(f"[sora] {jid}: stale render, marking failed", flush=True)
                        continue
                    try:
                        hist = _comfy_request(f"/history/{pid}", timeout=30)
                    except Exception:
                        continue  # comfy busy/restarting — retry next tick
                    entry = hist.get(pid)
                    if not entry:
                        continue
                    status = (entry.get("status") or {})
                    if status.get("status_str") == "error" or status.get("completed"):
                        out = _find_output_video(entry)
                        if out:
                            try:
                                if _upload_to_worker(jid, out):
                                    job["status"] = "done"
                                # upload failure: retry next tick
                            except Exception as e:
                                print(f"[sora] {jid}: upload error: {e}", flush=True)
                        else:
                            job["status"] = "failed"
                        changed = True
                if changed:
                    _save_state(state)
        except Exception as e:
            print(f"[sora] poller error: {e}", flush=True)


# ── job refs + guarded delete (HARD RULES: specs/job-delete-clearqueue-detail.md) ──

_CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                   ".webp": "image/webp", ".mp4": "video/mp4", ".wav": "audio/wav",
                   ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}


def _ref_paths(job):
    """A job's reference pictures: its own stored anchor (image_path) plus
    extras (image_paths). This IS the traversal defence — refs are served only
    from these stored paths; nothing the client sends is ever a path."""
    paths = []
    if job.get("image_path"):
        paths.append(job["image_path"])
    paths.extend(p for p in (job.get("image_paths") or []) if p)
    return paths


def _delete_guarded(path, removed, kept):
    """Unlink `path` only if the HARD RULES allow it:
    1. its realpath must be inside CONFIG["vpipe_sandbox"] — nothing outside
       (library/shack assets, anything else) may ever be deleted;
    2. it must not be an uploaded original — uploads/img_* and uploads/vox_*
       stay on disk; the only upload allowed is the derived vox_<t>_c.wav.
    Existing-but-refused files are reported in `kept` so the caller can see
    what was spared. Returns True iff the file was removed."""
    p = os.path.realpath(path)
    sb = os.path.realpath(CONFIG["vpipe_sandbox"])
    name = os.path.basename(p)
    inside = p == sb or p.startswith(sb + os.sep)
    is_original = name.startswith("img_") or (
        name.startswith("vox_") and not name.endswith("_c.wav"))
    if not os.path.isfile(p):
        return False
    if not inside or is_original:
        kept.append(p)
        return False
    try:
        os.remove(p)
        removed.append(p)
        return True
    except OSError:
        kept.append(p)
        return False


def _approve_into_queue(job):
    """Cross the approval gate: a gated (awaiting_approval) job becomes
    runnable. vpipe backend -> "queued" and the renderer lane takes it from
    there, exactly like a manual submit. The comfy backend has no queue lane
    (its submit IS the GPU start), so that submit happens here, at approval
    time — never while the job is still gated. Returns an error string on
    comfy submit failure (the job stays gated), else None."""
    if job.get("backend") == "comfy" and CONFIG["backend"] == "comfy":
        try:
            pid = _submit_comfy(job["prompt"], job["id"], job.get("seed") or 0)
        except Exception as e:
            return f"render submit failed: {e}"
        job["prompt_id"] = pid
        job["status"] = "rendering"
        return None
    job["status"] = "queued"
    return None

# ── HTTP ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "sNora/3.0"

    def log_message(self, fmt, *args):
        print(f"[sora] {self.address_string()} {fmt % args}", flush=True)

    def _authed(self):
        return hmac.compare_digest(
            self.headers.get("X-Sora-Secret", ""), CONFIG["secret"])

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        return (xff.split(",")[0].strip() if xff else self.client_address[0])

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/library" and self._authed():
            mf = _mf("jambo" if "site=jambo" in self.path else "snora")
            is_jambo = mf is JAMBO
            items = []
            for a in mf.get("assets", []):
                ext = a.get("ext") or ("jpg" if is_jambo else "png")
                items.append({"key": a["key"], "type": "image", "set": a.get("set", ""),
                              "name": a.get("key", "").replace("_", " "), "desc": a.get("desc", ""),
                              "r2": f"library/{a['key']}.{ext}"})
            v = _mf_voice(mf)
            if v.get("key"):
                items.append({"key": v["key"], "type": "voice", "set": "voices",
                              "name": f"{v.get('key','')} voice (reference)", "desc": v.get("desc", ""),
                              "r2": f"library/voice_{v['key']}.wav"})
            return self._send({"items": items})
        if path == "/stills" and self._authed():
            with _lock:
                st = _load_state()
            items = []
            for s in (st.get("stills") or {}).values():
                items.append({"id": s["id"], "prompt": s.get("prompt", ""),
                              "aspect": s.get("aspect"), "quality": s.get("quality"),
                              "created": s.get("created"), "source": s.get("source"),
                              "status": s.get("status", "done"),
                              "transparent": bool(s.get("transparent")),
                              "ext": s.get("ext"), "error": s.get("error"),
                              "reply": s.get("reply", ""),
                              "r2": bool(s.get("r2")),
                              "url": f"/api/sora/still/{s['id']}"})
            items.sort(key=lambda x: x.get("created") or 0, reverse=True)
            return self._send({"stills": items[:80]})
        if path == "/health":
            info = {"ok": True, "backend": CONFIG["backend"]}
            if CONFIG["backend"] == "vpipe":
                info["model"] = "ready" if _vpipe_ready() else "preparing"
            else:
                try:
                    _comfy_request("/system_stats", timeout=10)
                    info["comfy"] = "up"
                except Exception:
                    info["comfy"] = "down"
            return self._send(info)
        if path == "/view":
            # phone viewer (Android/any-browser client). The page itself is
            # inert; every API call it makes still needs the secret header.
            try:
                html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "snora_viewer.html"), "rb").read()
            except OSError:
                return self._send({"error": "viewer not installed"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
            return
        if not self._authed():
            return self._send({"error": "unauthorized"}, 401)
        with _lock:
            state = _load_state()
        if path == "/jobs":
            jobs = sorted(state["jobs"].values(), key=lambda j: -j["created"])[:100]
            return self._send({"jobs": [self._public(j) for j in jobs]})
        # ── the chron lane: schedules + the Discord picker ──────────────
        if path == "/schedule":
            scheds = state.get("schedules", [])
            return self._send({
                "schedules": scheds,
                "now": time.time(),
                "tz": SCHED.DEFAULT_TZ,
                "describe": {s["id"]: SCHED.describe(s) for s in scheds},
            })
        if path == "/schedule/guilds":
            try:
                return self._send({"guilds": SCHED.list_guilds(CONFIG)})
            except Exception as e:
                return self._send({"error": f"discord lookup failed: {e}"}, 502)
        m = re.fullmatch(r"/schedule/guilds/(\d+)/channels", path)
        if m:
            try:
                return self._send({"channels": SCHED.list_text_channels(m.group(1), CONFIG)})
            except Exception as e:
                return self._send({"error": f"discord lookup failed: {e}"}, 502)
        m = re.fullmatch(r"/schedule/([a-z0-9_]+)", path)
        if m:
            for s in state.get("schedules", []):
                if s["id"] == m.group(1):
                    return self._send(s)
            return self._send({"error": "not found"}, 404)
        if path == "/schedule/songs":
            return self._send({"songs": [{k: v for k, v in s.items() if k != "lyric"}
                                         for s in SCHED.DOG_SONGS]})
        m = re.fullmatch(r"/jobs/([a-z0-9_]+)/refs", path)
        if m:
            job = state["jobs"].get(m.group(1))
            if not job:
                return self._send({"error": "not found"}, 404)
            refs = []
            for i, p in enumerate(_ref_paths(job)):
                if os.path.isfile(p):
                    refs.append({"i": i, "name": f"photo_{i + 1}",
                                 "bytes": os.path.getsize(p)})
            return self._send({"refs": refs})
        m = re.fullmatch(r"/jobs/([a-z0-9_]+)/ref/([0-9]+)", path)
        if m:
            job = state["jobs"].get(m.group(1))
            if not job:
                return self._send({"error": "not found"}, 404)
            paths = _ref_paths(job)
            i = int(m.group(2))
            if i >= len(paths):
                return self._send({"error": "ref not found"}, 404)
            p = paths[i]
            if not p or not os.path.isfile(p):
                return self._send({"error": "ref not found"}, 404)
            try:
                data = open(p, "rb").read()
            except OSError:
                return self._send({"error": "ref not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", _CONTENT_TYPES.get(
                os.path.splitext(p)[1].lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        m = re.fullmatch(r"/jobs/([a-z0-9_]+)", path)
        if m:
            job = state["jobs"].get(m.group(1))
            if not job:
                return self._send({"error": "not found"}, 404)
            return self._send(self._public(job))
        m = re.fullmatch(r"/video/([a-z0-9_]+)(?:\.mp4)?", path)
        if m:
            # farm-direct clip playback (buyers without a Cloudflare worker,
            # and the /view phone client). Auth required, id-locked, jailed
            # to the sandbox dir.
            base = os.path.realpath(CONFIG["vpipe_sandbox"])
            stem = m.group(1)
            p = None
            for name in (stem + ".mp4", stem + "_exact.mp4", "dc_" + stem + ".mp4"):
                cand = os.path.realpath(os.path.join(base, "sora", name))
                if cand.startswith(base + os.sep) and os.path.isfile(cand):
                    p = cand
                    break
            if not p:
                return self._send({"error": "not found"}, 404)
            size = os.path.getsize(p)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            with open(p, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authed():
            return self._send({"error": "unauthorized"}, 401)
        path = self.path.split("?")[0].rstrip("/")
        # ── the chron lane ──────────────────────────────────────────────
        if path == "/schedule":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send({"error": "bad json"}, 400)
            name = str(data.get("name") or "").strip()[:80]
            if not name:
                return self._send({"error": "a schedule needs a name"}, 400)
            targets = data.get("targets")
            if not isinstance(targets, list) or not targets:
                return self._send({"error": "pick at least one delivery target"}, 400)
            rec = SCHED.new_schedule(
                name,
                kind=data.get("kind") or "singing_dog",
                interval_hours=data.get("interval_hours") or 2,
                quiet_start=data.get("quiet_start", 0),
                quiet_end=data.get("quiet_end", 8),
                tz=data.get("tz") or SCHED.DEFAULT_TZ,
                targets=targets,
                enabled=bool(data.get("enabled", True)),
                duration=data.get("duration") or 8,
                quality=data.get("quality") or "standard",
                aspect=data.get("aspect") or "vertical",
                image_key=data.get("image_key"),
                payload=data.get("payload"),
            )
            store = _load_schedules()
            store.append(rec)
            _save_schedules(store)
            print(f"[sched] created {rec['id']} {name} every {rec['interval_hours']}h "
                  f"(quiet {rec['quiet_start']:02d}-{rec['quiet_end']:02d} {rec['tz']})", flush=True)
            return self._send({"schedule": rec, "describe": SCHED.describe(rec)}, 201)
        m = re.fullmatch(r"/schedule/([a-z0-9_]+)/(toggle|run|delete)", path)
        if m:
            sid, action = m.group(1), m.group(2)
            store = _load_schedules()
            hit = next((s for s in store if s["id"] == sid), None)
            if not hit:
                return self._send({"error": "not found"}, 404)
            if action == "toggle":
                hit["enabled"] = not hit.get("enabled")
                if hit["enabled"]:
                    SCHED.advance(hit)          # never fire the moment it is switched on
                _save_schedules(store)
                return self._send({"schedule": hit, "describe": SCHED.describe(hit)})
            if action == "delete":
                store.remove(hit)
                _save_schedules(store)
                return self._send({"deleted": sid})
            # run now — QUIET HOURS STILL APPLY (Eric's rule), so this either
            # enqueues immediately or tells you why it refuses.
            if not SCHED.in_active_window(hit):
                z = SCHED.zone_of(hit)
                return self._send(
                    {"error": f"{hit['name']} is in quiet hours "
                              f"(quiet {hit['quiet_start']:02d}:00–{hit['quiet_end']:02d}:00) — "
                              f"nothing queues until the window opens."}, 409)
            try:
                built = SCHED.build_job(hit)
            except Exception as e:
                return self._send({"error": f"could not build the job: {e}"}, 400)
            payload = built["payload"]
            meta = built.get("meta", {})
            try:
                jid = _enqueue_payload(payload, f"sched:{hit['id']}")
            except Exception as e:
                return self._send({"error": f"enqueue failed: {e}"}, 502)
            hit["last_run"] = time.time()
            hit["last_job_id"] = jid
            hit["last_song_id"] = meta.get("song_id")
            hit["last_song_title"] = meta.get("song_title")
            hit["last_song_lang"] = meta.get("song_lang")
            hit["pending_job"] = jid
            hit["delivered"] = False
            _save_schedules(store)
            print(f"[sched] {sid}: run-now queued {jid}", flush=True)
            return self._send({"job_id": jid, "schedule": hit}, 201)
        m = re.fullmatch(r"/jobs/([a-z0-9_]+)/approve", self.path.split("?")[0].rstrip("/"))
        if m:  # the approval gate — Eric's tap is the only thing that opens it
            jid = m.group(1)
            with _lock:
                state = _load_state()
                job = state["jobs"].get(jid)
                if not job:
                    return self._send({"error": "not found"}, 404)
                st = job.get("status")
                if st != "awaiting_approval":
                    # 409 whose body names the status it actually found, so the
                    # app can say what happened instead of guessing
                    return self._send(
                        {"error": f"job is {st}, not awaiting_approval",
                         "status": st}, 409)
                err = _approve_into_queue(job)
                if err:
                    # gate stays closed; nothing was queued or rendered
                    return self._send({"error": err}, 502)
                _save_state(state)
            self.log_message("approved job %s -> %s", jid, job["status"])
            return self._send(self._public(job))
        if self.path.split("?")[0].rstrip("/") == "/approve_all":
            # "PRODUCE ALL" — flip every gated job in one tap
            with _lock:
                state = _load_state()
                gated = sorted(jid for jid, j in state["jobs"].items()
                               if j.get("status") == "awaiting_approval")
                approved = 0
                for jid in gated:
                    err = _approve_into_queue(state["jobs"][jid])
                    if err:
                        # leave this one gated; the rest still go through
                        print(f"[sora] approve_all: {jid}: {err}", flush=True)
                        continue
                    approved += 1
                if gated:
                    _save_state(state)
            if approved:
                self.log_message("approve_all: approved %d jobs", approved)
            return self._send({"ok": True, "approved": approved})
        m = re.fullmatch(r"/jobs/([a-z0-9_]+)/delete", self.path.split("?")[0].rstrip("/"))
        if m:  # permanent delete — dismissed jobs must never come back
            jid = m.group(1)
            with _lock:
                state = _load_state()
                job = state["jobs"].pop(jid, None)
                if job:
                    _save_state(state)
            removed, kept = [], []
            # Sweep this job's own derived artifacts. Uploaded photos and the
            # uploaded voice stay (Eric keeps his originals); _delete_guarded
            # enforces the sandbox realpath + never-an-original rules in code.
            _delete_guarded(os.path.join(CONFIG["vpipe_sandbox"], "sora", f"{jid}.mp4"),
                            removed, kept)
            _delete_guarded(os.path.join(CONFIG["vpipe_sandbox"], "uploads",
                                         f"silence_{jid}.wav"), removed, kept)
            vp = job.get("voice_path") if job else None
            if vp and str(vp).endswith("_c.wav"):  # the derived conversion only
                _delete_guarded(vp, removed, kept)
            seq = ((job or {}).get("sequence") or {}).get("id")
            if seq:
                with _lock:
                    state = _load_state()
                    still_needed = any((j.get("sequence") or {}).get("id") == seq
                                       for j in state["jobs"].values())
                if not still_needed:
                    seqdir = os.path.join(CONFIG["vpipe_sandbox"], "seq")
                    try:
                        names = sorted(os.listdir(seqdir))
                    except OSError:
                        names = []
                    for n in names:
                        if n.startswith(f"{seq}_") and n.endswith(".png"):
                            _delete_guarded(os.path.join(seqdir, n), removed, kept)
            if job:
                self.log_message("deleted job %s (removed %d, kept %d)",
                                 jid, len(removed), len(kept))
            return self._send({"ok": True, "removed": removed, "kept": kept})
        if self.path.split("?")[0].rstrip("/") == "/clear_queue":
            # Cancel everything still waiting — queued AND gated (a gated job
            # is waiting for Eric, it is not rendering). The job currently
            # rendering keeps going; uploads stay on disk (Eric keeps photos).
            with _lock:
                state = _load_state()
                waiting = sorted((jid for jid, j in state["jobs"].items()
                                  if j.get("status") in ("queued", "awaiting_approval")),
                                 key=lambda jid: state["jobs"][jid].get("created", 0))
                for jid in waiting:
                    del state["jobs"][jid]
                if waiting:
                    _save_state(state)
                still_rendering = sorted(
                    jid for jid, j in state["jobs"].items()
                    if j.get("status") == "rendering")
            if waiting:
                self.log_message("clear_queue: removed %d waiting jobs", len(waiting))
            return self._send({"ok": True, "removed": waiting,
                               "still_rendering": still_rendering})
        if self.path.split("?")[0].rstrip("/") == "/cancel":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send({"error": "bad json"}, 400)
            jid = str(data.get("id") or "")
            if not re.fullmatch(r"[a-z0-9_]+", jid):
                return self._send({"error": "bad id"}, 400)
            with _lock:
                state = _load_state()
                job = state["jobs"].get(jid)
                if not job:
                    return self._send({"error": "not found"}, 404)
                st = job.get("status")
                if st in ("queued", "awaiting_approval"):
                    # gated is waiting, not rendering — cancel-on-gated is allowed
                    del state["jobs"][jid]
                    _save_state(state)
                    self.log_message("cancelled %s job %s", st, jid)
                    return self._send({"ok": True, "removed": jid})
                if st == "rendering":
                    # kill the GPU lane's current render; the renderer will mark it failed
                    subprocess.Popen(["pkill", "-f", f"Helpers/vpipe --launch"])
                    self.log_message("cancel: killing render %s", jid)
                    return self._send({"ok": True, "killing": jid})
                return self._send({"error": f"cannot cancel a {st} job"}, 400)
        if self.path.split("?")[0].rstrip("/") == "/stills":
            if not self._authed():
                return self._send({"error": "unauthorized"}, 401)
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send({"error": "bad json"}, 400)
            res, code = _still_create(data, self._client_ip())
            return self._send(res, code)
        if self.path.split("?")[0].rstrip("/") == "/sequence":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send({"error": "bad json"}, 400)
            shots = [s for s in (data.get("shots") or []) if isinstance(s, dict) and s.get("prompt")][:6]
            if not shots:
                return self._send({"error": "shots required"}, 400)
            # The approval gate is farm law, not app manners. /sequence is the lane
            # the cockpit routes agent-planned films onto, so it gates BY DEFAULT:
            # nothing an agent planned can reach the GPU until Eric taps approve.
            # (Manual single-shot RENDER IT is unaffected — /jobs still defaults to
            # await_approval=false, per the spec.)
            await_approval = bool(data.get("await_approval", True))
            ip = self._client_ip()
            with _lock:
                state = _load_state()
                total_pending = sum(1 for j in state["jobs"].values()
                                    if j["status"] in ("queued", "rendering"))
                if total_pending + len(shots) > CONFIG["max_total_pending"] + 6:
                    return self._send({"error": "render farm is busy — try again in a bit"}, 503)
                seq_id = time.strftime("%Y%m%d") + "_seq_" + hashlib.sha1(
                    f"{shots[0]['prompt']}{time.time()}".encode()).hexdigest()[:6]
                state.setdefault("sequences", {})[seq_id] = {
                    "goal": str(data.get("goal", ""))[:1000], "notes": {}}
                created_jobs = []
                base_time = time.time()
                prev_id = None
                chain_aspect = shots[0].get("aspect") if shots[0].get("aspect") in CONFIG["aspects"] else "square"
                for i, s in enumerate(shots):
                    job_id = time.strftime("%Y%m%d") + "_" + hashlib.sha1(
                        f"{s['prompt']}{time.time()}{i}".encode()).hexdigest()[:8]
                    seed = random.getrandbits(63)
                    image_path = None
                    mf = _mf(data.get("site"))
                    if i == 0 and s.get("image_key"):
                        for a in mf.get("assets", []):
                            if a["key"] == s["image_key"]:
                                image_path = a["file"]
                                break
                    # chains share ONE canvas — mixed orientations are what makes a sequence cursed
                    aspect = chain_aspect
                    if s.get("voice") and i > 0 and s.get("voice_key") == _mf_voice(mf).get("key"):
                        # VO shot: ref2va standalone with this lane's face + voice
                        for a in mf.get("assets", []):
                            if a["key"] == s.get("image_key"):
                                break
                    tr = s.get("transition") if s.get("transition") in (
                        "chain", "cut", "dissolve", "fade", "dip", "flash", "slide", "wipe", "whip", "glitch", "iris") else "chain"
                    job = {
                        "id": job_id, "prompt": str(s.get("prompt", ""))[:CONFIG["prompt_max_chars"]],
                        "quality": s.get("quality") if s.get("quality") in CONFIG["qualities"] else "standard",
                        "aspect": aspect, "duration": int(s["duration"]) if int(s.get("duration", 5) or 5) in CONFIG["durations"] else 5,
                        "zoom": min(2.5, max(1.0, float(s.get("zoom") or 1.0))),
                        "focus": s.get("focus") if s.get("focus") in ("center", "face", "top") else "center",
                        "image_path": image_path,
                        "ref_mode": s.get("ref_mode") if s.get("ref_mode") in ("scene", "identity") else "scene",
                        "sequence": {"id": seq_id, "index": i, "count": len(shots), "prev_id": prev_id,
                                     "transition": tr if i > 0 else "chain"},
                        "title": str(s.get("title", f"shot {i+1}"))[:60],
                        "soundscape": str(s.get("soundscape") or "")[:600],
                        "music": str(s.get("music") or "")[:300],
                        "backend": CONFIG["backend"], "status": "awaiting_approval" if await_approval else "queued", "ip": ip,
                        "source": (str(data.get("source"))[:60] if data.get("source") else None),
                        "lang": "sw" if str(data.get("site") or "").strip().lower() == "jambo" else None,
                        "created": base_time + i * 0.001, "seed": seed,
                    }
                    # VO shots: standalone ref2va with this lane's face + voice
                    if s.get("voice") and s.get("image_key"):
                        for a in mf.get("assets", []):
                            if a["key"] == s["image_key"]:
                                job["image_path"] = a["file"]
                                job["voice_path"] = _mf_voice(mf).get("file")
                                job["sequence"]["transition"] = "cut"
                                break
                    state["jobs"][job_id] = job
                    # report the status the job ACTUALLY has — a gated sequence has to
                    # come back looking gated, or the phone shows "queued" for a shot
                    # that is waiting on his tap and the card never appears
                    created_jobs.append({"id": job_id, "title": job["title"], "status": job["status"]})
                    prev_id = job_id
                _save_state(state)
            self.log_message("new sequence %s (%d shots)", seq_id, len(shots))
            return self._send({"ok": True, "sequence": seq_id, "jobs": created_jobs}, 201)
        if self.path.split("?")[0].rstrip("/") == "/plan":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send({"error": "bad json"}, 400)
            try:
                return self._send(_builder_plan_handler(data))
            except Exception as e:
                return self._send({"error": f"planner failed: {e}"}, 502)
        if self.path.split("?")[0].rstrip("/") != "/jobs":
            return self._send({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send({"error": "bad json"}, 400)

        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return self._send({"error": "prompt is required"}, 400)
        if len(prompt) > CONFIG["prompt_max_chars"]:
            return self._send(
                {"error": f"prompt too long (max {CONFIG['prompt_max_chars']})"}, 400)
        quality = (data.get("quality") or "standard").lower()
        if quality not in CONFIG["qualities"]:
            quality = "standard"
        aspect = (data.get("aspect") or "square").lower()
        auto_aspect = aspect == "auto"  # match the clip to the input picture
        if auto_aspect:
            aspect = "wide"  # provisional; refined below once the image lands
        elif aspect not in CONFIG["aspects"]:
            aspect = "square"
        duration = int(data.get("duration") or 5)
        if duration not in CONFIG["durations"]:
            duration = 5
        # where a job came from — "discord:<name>" from the Discord bot,
        # absent from the app. carried through to the gallery for visibility.
        source = data.get("source")
        source = (re.sub(r"[^A-Za-z0-9_#. @:-]", "", str(source))[:60] or None
                  if isinstance(source, str) and source.strip() else None)
        # A line the clip cannot speak comes out garbled — bounce it at the
        # door with the math; the app shows this error verbatim.
        spoken = spoken_words(prompt)
        budget = word_budget(duration)
        if spoken > budget:
            return self._send(
                {"error": f"that line is {spoken} words — {duration}s fits about "
                          f"{budget} spoken words (≈2 per second). "
                          "Shorten the line or pick a longer duration."}, 400)
        ref_mode = data.get("ref_mode") if data.get("ref_mode") in ("scene", "identity") else "scene"
        zoom = min(2.5, max(1.0, float(data.get("zoom") or 1.0)))
        focus = data.get("focus") if data.get("focus") in ("center", "face", "top") else "center"
        # the approval gate: agent-planned jobs arrive gated; manual submits
        # (await_approval false/absent) queue immediately, exactly as before
        await_approval = bool(data.get("await_approval", False))
        image_path = voice_path = None
        end_image_path = None
        key_paths = []
        mf = _mf(data.get("site"))
        for k in (data.get("image_keys") or [])[:9]:
            if isinstance(k, str) and re.fullmatch(r"[a-z0-9_]+", k):
                for a in mf.get("assets", []):
                    if a["key"] == k:
                        key_paths.append(a["file"])
                        break
        try:
            # uploaded photos, plural: first one is the anchor, rest ride along as extra refs
            uploads = []
            for d in (data.get("images") or [])[:9]:
                if isinstance(d, str) and d:
                    uploads.append(_save_dataurl(d, "img"))
            if data.get("image"):
                uploads.insert(0, _save_dataurl(data["image"], "img"))
            if uploads:
                image_path = uploads[0]
                key_paths = uploads[1:] + key_paths
            if data.get("end_image"):
                end_image_path = _save_dataurl(data["end_image"], "end")
            if data.get("voice"):
                voice_path = _save_dataurl(data["voice"], "vox")
                if not image_path:
                    return self._send(
                        {"error": "custom voice needs an input image too (the voice rides on a character)"}, 400)
        except ValueError as e:
            return self._send({"error": str(e)}, 400)
        if voice_path:
            voice_path = _to_wav(voice_path)
        # builder library references (validated against this lane's manifest)
        if not image_path and data.get("image_key"):
            for a in mf.get("assets", []):
                if a["key"] == data["image_key"]:
                    image_path = a["file"]
                    break
        if not voice_path and data.get("voice_key"):
            v = _mf_voice(mf)
            if data["voice_key"] == v.get("key"):
                voice_path = v.get("file")
                if voice_path and not os.path.exists(voice_path):
                    voice_path = None
        if voice_path and not image_path:
            return self._send({"error": "voice needs an image"}, 400)
        if not end_image_path and data.get("end_image_key"):
            for aa in mf.get("assets", []):
                if aa["key"] == data["end_image_key"]:
                    end_image_path = aa["file"]
                    break
        if end_image_path and not (image_path or key_paths):
            return self._send({"error": "an ending frame needs a beginning frame too"}, 400)

        target_ratio = None
        if auto_aspect:
            anchor = image_path or (key_paths[0] if key_paths else None)
            ratio = _media_ratio(anchor) if anchor else None
            if ratio:
                aspect = _auto_aspect(ratio)
                target_ratio = f"{ratio.numerator}:{ratio.denominator}"
            else:
                aspect = "wide"
        ip = self._client_ip()
        with _lock:
            state = _load_state()
            mine = [j for j in state["jobs"].values() if j.get("ip") == ip]
            active = sum(1 for j in mine if j["status"] == "rendering")
            pending = sum(1 for j in mine if j["status"] in ("queued", "rendering"))
            total_pending = sum(1 for j in state["jobs"].values()
                                if j["status"] in ("queued", "rendering"))
            if active >= CONFIG["max_active_per_ip"]:
                return self._send(
                    {"error": "you already have renders in progress — wait for one to finish"}, 429)
            if pending >= CONFIG["max_pending_per_ip"]:
                return self._send(
                    {"error": "queue full for your session — try again later"}, 429)
            if total_pending >= CONFIG["max_total_pending"]:
                return self._send(
                    {"error": "render farm is busy — try again in a bit"}, 503)

            job_id = time.strftime("%Y%m%d") + "_" + hashlib.sha1(
                f"{prompt}{time.time()}".encode()).hexdigest()[:8]
            seed = random.getrandbits(63)
            job = {
                "id": job_id, "prompt": prompt[:CONFIG["prompt_max_chars"]],
                "quality": quality, "aspect": aspect, "duration": duration,
                "target_ratio": target_ratio,
                "source": source,
                "lang": "sw" if str(data.get("site") or "").strip().lower() == "jambo" else None,
                "has_image": bool(image_path), "has_voice": bool(voice_path),
                # the official overall_soundscape / non_diegetic_music fields: the
                # planner's own words when it wrote them, else the scaffold default
                "soundscape": str(data.get("soundscape") or "")[:600],
                "music": str(data.get("music") or "")[:300],
                "ref_mode": ref_mode, "zoom": zoom, "focus": focus,
                "image_path": image_path or (key_paths[0] if key_paths else None),
                "image_paths": key_paths,
                "end_image_path": end_image_path,
                "voice_path": voice_path,
                "backend": CONFIG["backend"],
                # gated jobs wait for Eric's tap; everything else queues
                # exactly as today (manual RENDER IT is already an approval)
                "status": "awaiting_approval" if await_approval else "queued",
                "ip": ip,
                "created": time.time(), "seed": seed,
            }
            if CONFIG["backend"] == "comfy" and not await_approval:
                # comfy has no queue lane — this submit IS the GPU start, so a
                # gated job must never go through here (HARD RULE: a gated job
                # never renders). _approve_into_queue submits at approval time.
                try:
                    pid = _submit_comfy(prompt, job_id, seed)
                except Exception as e:
                    return self._send({"error": f"render submit failed: {e}"}, 502)
                job["prompt_id"] = pid
                job["status"] = "rendering"
            state["jobs"][job_id] = job
            _save_state(state)

        self.log_message("new job %s backend=%s quality=%s ip=%s",
                         job_id, job["backend"], quality, ip)
        return self._send(self._public(job), 201)

    @staticmethod
    def _public(j):
        pub = {k: j.get(k) for k in ("id", "prompt", "status", "backend",
                                     "quality", "aspect", "duration", "ref_mode",
                                     "source",
                                     "has_image", "has_voice", "created",
                                     "started", "finished")}
        pub["ref_count"] = len(_ref_paths(j))
        pub["has_end"] = bool(j.get("end_image_path"))
        pr = _PROGRESS.get(j.get("id"))
        if pr and j.get("status") == "rendering" and time.time() - pr["ts"] < 300:
            pub["progress"] = pr
        elif j.get("status") != "rendering" and pr:
            _PROGRESS.pop(j.get("id"), None)
        if (j.get("sequence") or {}).get("id"):
            pub["seq"] = j["sequence"]["id"]
        if j.get("error"):
            pub["error"] = j["error"]
        return pub


def _sync_cuts():
    """Mirror any workshop cuts (eli-tv uploads on R2) into ~/johnnyb_h3/inputs/cuts/
    so Shot Planner commands reference real local files."""
    import shutil
    cuts_dir = f"{HOME}/johnnyb_h3/inputs/cuts"
    os.makedirs(cuts_dir, exist_ok=True)
    url = "https://snail3d.com/api/elitv/manifest"
    # token comes from the elitv worker (same password hash scheme) — stored beside config
    tok_file = os.path.join(HERE, "elitv_token.txt")
    if not os.path.exists(tok_file):
        return
    token = open(tok_file).read().strip()
    req = urllib.request.Request(url, headers={"X-EliTV-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            manifest = json.loads(resp.read().decode())
    except Exception:
        return
    for item in manifest.get("uploads", []):
        name = item["name"]
        dest = os.path.join(cuts_dir, name)
        if os.path.exists(dest):
            continue
        try:
            areq = urllib.request.Request(
                f"https://snail3d.com/api/elitv/asset/{item['key']}",
                headers={"X-EliTV-Token": token})
            with urllib.request.urlopen(areq, timeout=60) as r:
                data = r.read()
            with open(dest + ".tmp", "wb") as f:
                f.write(data)
            os.replace(dest + ".tmp", dest)
            print(f"[sora] synced cut {name}", flush=True)
        except Exception as e:
            print(f"[sora] cut sync {name}: {e}", flush=True)


def _cuts_sync_loop():
    while True:
        time.sleep(300)
        try:
            _sync_cuts()
        except Exception:
            pass


SHACK_MANIFEST_FILE = os.path.join(HERE, "shack_manifest.json")


def _load_shack():
    try:
        return json.load(open(SHACK_MANIFEST_FILE))
    except Exception:
        return {"character": {}, "assets": []}


SHACK = _load_shack()

# ── the Jambo manifest: a second, Kenyan reference library for the promo lane ──
JAMBO_MANIFEST_FILE = os.path.join(HERE, "jambo_manifest.json")


def _load_jambo():
    try:
        return json.load(open(JAMBO_MANIFEST_FILE))
    except Exception:
        return {"character": {}, "assets": []}


JAMBO = _load_jambo()


def _mf(site):
    """Which reference library this request belongs to. One service, two lanes."""
    return JAMBO if str(site or "").strip().lower() == "jambo" else SHACK


def _mf_voice(mf):
    return (mf.get("character") or {}).get("voice") or {}


def _builder_system():
    lib = [{"key": a["key"], "set": a["set"], "desc": a["desc"]} for a in SHACK.get("assets", [])]
    return (
        "You are the sNora Scene Builder — a master director/showrunner who chats with Eric, plans MiniMax H3 video shots for the render farm, and directs their continuity.\n\n"
        "WORLD:\n"
        f"- The star is Eric: {SHACK.get('character', {}).get('look', 'man in cap and robe')}. Any mention of 'me', 'my', 'I' = Eric.\n"
        "- Set SNAIL SHACK ('the shack', 'the office', 'my workshop'): backyard shed workshop, usually NIGHT — warm LED bar light, dark sloped ceiling, pegboard tool wall, parts drawers, 'The Office' sign, worn brown leather chair, white marble-topped desks, Persian rug, Bambu P2S 3D printer corner.\n"
        "- Set BACKYARD PATIO ('outside', 'backyard', 'the hammock'): night — warm string lights in a tree with a white hanging hammock chair, green turf, lit brown privacy fence, covered patio with green cushions, shed in the background.\n"
        "- Eric's own voice reference is available (voice key 'eric'): casual American male.\n\n"
        "NAMING — SAY THE REAL NAMES (casting and direction notes for Eric's own render farm, not public claims):\n"
        "- When Eric's words point at a real actor, character, film, show, brand, landmark or studio, the job prompt carries THAT NAME — plus the specific film and scene when it helps. Hedging is a defect: a vague descriptor renders as generic mush; the name is what steers the shot.\n"
        "- Never 'a rugged action star' — write 'Arnold Schwarzenegger, bison-ranch hand'. Never 'reminiscent of a 90s cop movie' — write 'the Kindergarten Cop classroom scene, same energy'. Never 'a familiar blue hedgehog' — write 'Sonic the Hedgehog, doing the farm chores'. Never 'in the style of Pixar' — write 'Pixar short-film look: big eyes, warm rim light'.\n"
        "- Banned hedges, never in a prompt/goal/reply: 'inspired by', 'in the vein of', 'a certain', 'evoking', 'somewhat like'.\n"
        "- A shoutout line from Eric — his brands (Snail3D, sNora) or any name he lifts — is dialogue content carried verbatim, not advertising to soften.\n"
        "- Casting language, not false claims: 'Arnold Schwarzenegger as the ranch hand' (a direction to the renderer) — but never state as fact that someone starred in, endorsed, or appears in Eric's clip.\n"
        "- The carve-out — Eric's own family and their photos: keep them relational ('the kid', 'his wife') with his reference photos; never guess at names for people he knows.\n"
        "- No apologies, no disclaimers, no safety preambles — not in a job prompt, not anywhere. Whether to publish a clip is Eric's call with the finished clip in front of him; the prompt's job is to name the shot he asked for, not to hide the choice from him.\n\n"
        "IMAGE LIBRARY (use these exact keys in shots.image):\n"
        f"{json.dumps(lib, indent=1)}\n\n"
        "== THE DIRECTOR'S HANDBOOK ==\n"
        "SHOT BUDGETS (validated on this model — never cram):\n"
        "- Clips up to 5s: ONE shot only. Stage depth (foreground/midground/background) instead of cutting.\n"
        "- 8s clips: 1-2 internal beats, action-reaction momentum.\n"
        "- 10-15s clips: 2-4 internal shots with an emotional punchline per beat — H3 natively models multi-shot; write them as [Shot 1] ... [Shot 2 At 00:05.000] ... paragraphs with strictly increasing timestamps covering the whole duration.\n"
        "PACING — pick duration by content, not habit:\n"
        "- 3-4s: establishing plates, quick reactions, cutaway inserts, pure atmosphere.\n"
        "- 5s: one dialogue beat (≤10 words) OR one clean action. The default unit.\n"
        "- 8s: action + reaction, or a two-beat gag with pause before the punchline.\n"
        "- 10s: two dialogue beats, or a slow reveal.\n"
        "- 15s: a full mini-scene with a turn. Only when the story truly needs it.\n"
        "CAMERA VOCABULARY (write as prose, ONE move per shot — never keyword lists): 'the camera holds steady', 'slow push in', 'slow pull back to reveal', 'pans right/left with his gaze', 'drifts closer past his shoulder', 'handheld, with a slight documentary shake', 'low angle looking up'. Always say what the camera does or the model invents cuts.\n"
        "GENRE TUNING:\n"
        "- Vlog/casual: handheld feel, direct address to camera, warm light, small gestures.\n"
        "- Cinematic/drama: slow deliberate moves, deep shadow, held looks, silence before speech.\n"
        "- Comedy: static camera for reactions, a beat of stillness before the punchline, exaggerated expressions.\n"
        "- Documentary: steady tripod energy, natural motion, narration in off-screen voiceover ('says in an off-screen voiceover ... while his lips remain completely closed').\n"
        "- Cozy/asmr: soft light, slow drift, gentle ambient sound foregrounded.\n"
        "ZOOM & CROP TOOL (how you get different angles from one photo):\n"
        "- The farm crops/zooms reference photos to the canvas — it NEVER stretches them. So different angles come from choosing zoom + focus on the SAME master photo.\n"
        "- shots.zoom: 1.0 = full frame, 1.5 = medium close, 2.0-2.5 = close-up. shots.focus: 'face' (bias to the face), 'top', 'center'.\n"
        "- Example: one wide shack master at zoom 1.0 is the establishing shot; the same photo at zoom 2.0/focus 'face' is the close-up. That's how you build coverage — derive, don't invent.\n"
        "- In a chain, zooming IN across shots (1.0 → 1.4 → 2.0) creates a natural push-in feel.\n"
        "- All shots in one chain share ONE aspect ratio (the first shot's) — mixed orientations in a sequence are forbidden.\n\n"
        "REFERENCE INTENT — THE MOST IMPORTANT ROUTING DECISION:\n"
        "Every shot that uses an image must set ref_mode:\n"
        "- ref_mode='scene' — the photo's LOCATION AND TIME OF DAY ARE the scene (first-frame anchor). Use ONLY when Eric wants to continue in the photo's actual setting (the shack at night, the backyard at night).\n"
        "- ref_mode='identity' — the photo defines WHO Eric is, NOT where he is. Use whenever Eric describes a NEW OR DIFFERENT setting from the photo: different location, daytime when the photo is night, a whole new world (Andy's room, the beach, space). Ref2VA carries Eric into the freshly-prompted scene.\n"
        "- If Eric's described scene matches no library photo's setting: either (a) ref_mode='identity' with the closest Eric image — the prompt's scene wins, or (b) image=null with a rich character description in the prompt (pure text-to-video).\n"
        "- NEVER use ref_mode='scene' just because a nice photo exists — that pins the photo's background onto the clip. This is the #1 failure mode.\n"
        "- Library images carry scene tags in their descriptions ('night'/'day'/'golden hour' + location) — match scene AND time when anchoring.\n\n"
        "CONTINUITY & TRANSITIONS — two tools plus a effects rack, choose deliberately:\n"
        "- CHAIN (transition='chain'): next shot literally continues from the last frame of the previous clip — same pose, camera state, energy. For flowing single-take feel, real-time action, conversations.\n"
        "- HARD CUT (transition='cut'): next shot is independent — new angle, time skip, comedic jump. Classic: hard cut from WIDE to CLOSE-UP.\n"
        "- EFFECT TRANSITIONS (applied when the sequence film is stitched): 'dissolve' (time passing, dreamy), 'fade' (gentle crossfade), 'dip' (dip-to-black — chapter/scene change), 'flash' (dip-to-white — impact/memory), 'slide' (playful, social-media energy), 'wipe' (retro/energetic), 'whip' (whip-pan — comedy, high energy), 'glitch' (tech/horror sting), 'iris' (playful close). Use sparingly — 1-2 per film max; cuts and chains carry most films.\n"
        "- Every shot after the first MUST declare its transition. Default to chain only for one continuous moment.\n"
        "- Chained shots: write openings that MATCH the previous ending (pose, camera framing, energy). Cut/effect shots: write as fresh starts — reuse the set's base image key for the new angle, or image=null for time skips.\n"
        "- Shots in the same location (non-chained) use the SAME base image key. Establishing/empty-set shots use the *_wide / corner keys.\n"
        "VOICEOVER — a first-class tool:\n"
        "- On-camera dialogue: <d>[English] the words</d> + voice=true (Eric's reference voice, Ref2VA pipeline).\n"
        "- OFF-SCREEN VOICEOVER ( narration over any scene ): voice=true AND prompt phrased exactly like: 'Eric narrates in an off-screen voiceover — <the narration>, while his lips remain completely closed' or 'says in an off-screen voiceover ... while his lips remain completely closed'. For pure b-roll narration use image=null in chained plans, or the subject shot with image=<Eric pose key>. VO shots work best as their own shot (transition='cut' into or out of them).\n"
        "- Max ~2 short VO beats (≤10 words each) per 5s clip; insert silent visual beats between dense narration.\n"
        "- No dialogue = ambient sound only. Music only via non_diegetic_music when Eric asks.\n"
        "- Single (non-chained) image shots: the image becomes BOTH first and last frame — write motion that starts and settles near that pose.\n\n"
        "DIALOGUE DISCIPLINE — every spoken line, VO or on-camera:\n"
        "- Word budget ≈ 2.2 spoken words per second of clip: 3s≤7, 5s≤11, 8s≤18, 10s≤22, 15s≤33. The farm 400s an over-budget line.\n"
        "- Banned filler in any spoken line: hey, hey y'all, y'all, just, literally, like, you know, I mean, uh, um, basically, actually, guys, okay so, 'so' as an opener, 'well' as an opener.\n"
        "- A spoken line must NEVER narrate what the shot already shows — it adds a want, a joke, a name, a memory.\n"
        "- One intent per line; one speaker per shot. Write it as: He says, <emotion>: \"<line>\" — placed at the END of the scene description, before the Soundscape line.\n"
        "- When the scene invites words and Eric gave none, WRITE one line that fits the budget (proactive fill-in); a line over budget gets rewritten shorter, meaning and voice intact.\n"
        "- Leave at least 30% of the clip wordless so the delivery can breathe.\n\n"
        "IMAGE LIBRARY pose rules: match the shot's action to the pose in the key (typing keys for typing shots, chair keys for talking-to-camera, backyard keys for outdoor).\n\n"
        "OUTPUT — strict JSON only, no markdown fences:\n"
        '{"reply": "<1-3 friendly sentences to Eric>", "goal": "<one-sentence synopsis of the whole scene>", '
        '"shots": [{"title": "short name", "prompt": "<complete scene: action + camera + any dialogue>", "aspect": "square", "duration": 5, "quality": "standard", "image": "<library key or null>", "voice": false, "transition": "chain", "ref_mode": "scene"}]}\n'
        "(shots.transition ∈ chain|cut|dissolve|fade|dip|flash|slide|wipe|whip|glitch|iris; shots.ref_mode ∈ scene|identity; the FIRST shot's transition is ignored.)"
    )


def _jambo_system():
    lib = [{"key": a["key"], "set": a.get("set", ""), "desc": a["desc"]}
           for a in JAMBO.get("assets", [])]
    return (
        "Wewe ni JAMBO — mkurugenzi wa matangazo ya video na picha kwa biashara ndogo katika "
        "Kenya na Afrika Mashariki. You are JAMBO, a promo director for small East African "
        "businesses: dukas, salons, phone shops, tailors, produce stalls, boda boda riders, "
        "restaurants, matatus and market traders.\n\n"
        "LUGHA — THE FIRST RULE:\n"
        "- Reply to the user in simple, warm KISWAHILI. Keep it to 1-3 sentences.\n"
        "- Every spoken line is SWAHILI and written exactly as: <d>[Swahili] the words</d>.\n"
        "- Never write English dialogue. Swahili words are never 'English'.\n"
        "- Write the Swahili a market trader actually speaks, not textbook Swahili.\n"
        "  Good: 'Karibu! Bei nafuu leo.' · 'Bidhaa zetu ni za ubora.' · 'Tunafanya kazi kwa haraka.'\n"
        "  Good CTA: 'Njoo leo!' · 'Tupigie simu.' · 'Karibu dukani.'\n\n"
        "PHONE-FIRST (9:16):\n"
        "- The audience watches on a phone. Unless told otherwise EVERY shot uses "
        "aspect \"vertical\" (9:16). Never mix orientations in one film.\n"
        "- Compose for the phone: subject centred, faces in the upper third, clear space "
        "at the bottom for shop name and phone number.\n\n"
        "BUSINESS PROMO CRAFT:\n"
        "- A promo is short: hook in the first second, the product/service clearly shown, one "
        "benefit, one call to action, the shop name and phone number at the end.\n"
        "- Show real work: hands, tools, product close-ups, the owner smiling at the camera, "
        "the shopfront, the queue, the delivery.\n"
        "- On-screen text goes in DOUBLE QUOTES exactly as asked, e.g. \"JAMBO SALON - KARIBU SANA\".\n"
        "- Sound: diegetic shop ambience by default; music only when asked (non_diegetic_music).\n\n"
        "NAMING — SAY THE REAL NAMES:\n"
        "- When the user names a shop, brand, product or place, carry THAT NAME verbatim in the "
        "prompt. Hedging ('a certain shop', 'some salon') is a defect — the name is what steers "
        "the shot.\n"
        "- A shop's own name and phone number are content, not advertising to soften — keep them exact.\n\n"
        "IMAGE LIBRARY — KENYAN LOCATIONS (use these exact keys in shots.image):\n"
        f"{json.dumps(lib, indent=1)}\n\n"
        "REFERENCE INTENT — every shot that uses an image must set ref_mode:\n"
        "- ref_mode='scene' — the library photo's LOCATION IS the scene. Use the Kenyan market, "
        "street, shop and savanna shots as the setting.\n"
        "- ref_mode='identity' — the photo defines WHO the subject is; the scene comes from the idea. "
        "Use for a user-uploaded photo of the owner or their product.\n"
        "- image=null gives a pure text-to-video scene — best when no library photo fits.\n\n"
        "DIALOGUE DISCIPLINE:\n"
        "- Word budget ≈ 2.2 spoken words per second: 3s≤7, 5s≤11, 8s≤18, 10s≤22, 15s≤33. "
        "An over-budget line is rejected by the farm.\n"
        "- Leave at least a third of the clip wordless so the delivery can breathe.\n"
        "- One speaker per shot, one intent per line.\n\n"
        "SHOT BUDGETS: 5s = one beat. 8s = two beats. 10-15s = 2-4 shots written as "
        "[Shot 1] ... [Shot 2 At 00:05.000] ... with strictly increasing timestamps.\n"
        "CAMERA: write ONE move per shot as prose — 'the camera holds steady', 'slow push in', "
        "'pans right across the stall'. Always say what the camera does.\n\n"
        "OUTPUT — strict JSON only, no markdown fences:\n"
        '{"reply": "<1-3 sentences in Kiswahili>", "goal": "<one-sentence synopsis>", '
        '"shots": [{"title": "short name", "prompt": "<complete scene: action + camera + Swahili dialogue>", '
        '"aspect": "vertical", "duration": 5, "quality": "standard", "image": "<library key or null>", '
        '"voice": false, "transition": "chain", "ref_mode": "scene"}]}\n'
        "(shots.aspect is 'vertical' for phone promos. shots.voice is false — Swahili is spoken "
        "natively from the <d> tag, no voice reference needed. shots.transition ∈ "
        "chain|cut|dissolve|fade|dip|flash|slide|wipe|whip|glitch|iris; shots.ref_mode ∈ scene|identity. "
        "The FIRST shot's transition is ignored.)"
    )


def _jambo_still_system():
    return (
        "You write ONE prompt for a text-to-image model (Qwen-Image) that paints a promotional "
        "photo for a small business in Kenya.\n"
        "Write the image prompt in ENGLISH — the image model reads English — describing the "
        "FINISHED FRAME only. No commands, no 'generate', no camera instructions.\n"
        "Include subject, setting, light, mood and composition; use 8-14 concrete phrases.\n"
        "If the user wants words in the picture, put them in DOUBLE QUOTES exactly as asked: "
        "\"JAMBO SALON\".\n"
        "Kenyan context that helps: matatus, dukas, kitenge fabrics, produce markets, acacia, "
        "baobab, warm golden light, red earth.\n"
        "Phone-first: default aspect \"portrait\" (9:16). Never mention resolution or aspect "
        "inside the prompt.\n"
        "Reply in strict JSON only: "
        '{"reply": "<1-2 sentences in Kiswahili>", "prompt": "<the image prompt>", '
        '"aspect": "portrait|square|wide", "mood": "<one word>"}'
    )


def _builder_plan(message, history, site=None):
    base = CONFIG.get("snail_api_base")
    key = CONFIG.get("snail_api_key")
    if not base or not key:
        raise RuntimeError("snail-api key not configured")
    # FIRE_RULE + CONTRACT: fire is opt-in, and the official H3 writing rules.
    # H3 is CFG-distilled, so there is no negative prompt to fall back on.
    system_prompt = ((_jambo_system() if str(site or "").strip().lower() == "jambo"
                      else _builder_system()) + FIRE_RULE + CONTRACT)
    msgs = [{"role": "system", "content": system_prompt}]
    for h in (history or [])[-6:]:
        role = h.get("role", "user")
        if role in ("user", "assistant"):
            msgs.append({"role": role, "content": str(h.get("content", ""))[:2000]})
    msgs.append({"role": "user", "content": message[:2000]})
    fire_ok = wants_fire(message)
    plan = {}
    for fire_try in (1, 2):
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps({"model": CONFIG.get("snail_model", "snail-top"),
                             "messages": msgs, "max_tokens": 2500, "temperature": 0.7}).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=180) as resp:
            d = json.loads(resp.read().decode())
        content = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return {"reply": content[:500] or "(no plan returned)", "shots": []}
        plan = None
        for attempt in (m.group(0), re.sub(r",\s*([}\]])", r"\1", m.group(0))):
            try:
                plan = json.loads(attempt)
                break
            except json.JSONDecodeError:
                continue
        if not isinstance(plan, dict):
            return {"reply": content[:500], "shots": []}
        # some models string-encode the plan inside "reply" — unwrap if so
        if (not plan.get("shots") and isinstance(plan.get("reply"), str)
                and plan["reply"].strip().startswith("{")):
            try:
                plan = json.loads(plan["reply"])
            except json.JSONDecodeError:
                pass
        # Fire is opt-in and H3 has no negative prompt to lean on, so a planner
        # that invents fire gets exactly ONE re-ask, with its own draft quoted
        # back at it. See h3_prompt.py.
        doors = []
        for _s in (plan.get("shots") or []):
            if isinstance(_s, dict):
                _b, _f = clean_body(_s.get("prompt") or "")
                doors.append(" ".join([_b, _f.get("overall_soundscape", ""),
                                       _f.get("non_diegetic_music", "")]))
        slip = [] if fire_ok else fire_slip_in(" ".join(doors), str(plan.get("goal") or ""))
        if not slip:
            break
        if fire_try == 1:
            print(f"[sora] plan invented fire ({', '.join(slip)}) for a no-fire ask "
                  f"— re-asking: {message[:70]}", flush=True)
            msgs.append({"role": "user",
                         "content": fire_corrective(slip, "\n".join(doors)[:1500])})
            continue
        print(f"[sora] fire survived the re-ask ({', '.join(slip)}) — shipping the plan "
              f"anyway", flush=True)
    valid_keys = {a["key"] for a in SHACK.get("assets", [])}
    shots = []
    for s in plan.get("shots", [])[:6]:
        if not isinstance(s, dict) or not s.get("prompt"):
            continue
        # the scaffold owns the field labels: lift any the planner inlined
        pbody, pfields = clean_body(s["prompt"])
        shots.append({
            "title": str(s.get("title", "shot"))[:60],
            "prompt": pbody[:2000],
            "aspect": s.get("aspect") if s.get("aspect") in CONFIG["aspects"] else "square",
            "duration": int(s["duration"]) if int(s.get("duration", 5) or 5) in CONFIG["durations"] else 5,
            "quality": s.get("quality") if s.get("quality") in CONFIG["qualities"] else "standard",
            "image": s.get("image") if s.get("image") in valid_keys else None,
            "voice": bool(s.get("voice")),
            "soundscape": str(s.get("soundscape") or pfields.get("overall_soundscape")
                              or "")[:600],
            "music": str(s.get("music") or pfields.get("non_diegetic_music") or "")[:300],
            "transition": s.get("transition") if s.get("transition") in (
                "chain", "cut", "dissolve", "fade", "dip", "flash", "slide", "wipe", "whip", "glitch", "iris") else "chain",
            "zoom": min(2.5, max(1.0, float(s.get("zoom") or 1.0))),
            "focus": s.get("focus") if s.get("focus") in ("center", "face", "top") else "center",
            "ref_mode": s.get("ref_mode") if s.get("ref_mode") in ("scene", "identity") else "scene",
        })
    return {"reply": str(plan.get("reply", ""))[:600], "shots": shots}


# ── conversation memory ────────────────────────────────────────────────────
# Eric talks to the farm and expects it to remember. The phone used to be the only
# place history lived (it sent `history` back on every turn), so a redo from the
# Reel tab, a different device, or a fresh launch forgot everything he'd already
# said. Turns live here now, keyed by a thread id the caller sticks to.
_TURNS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sora_turns.json")
_TURNS_KEEP = 24  # per thread: 12 exchanges is plenty for "no, not like that" x12


def _load_turns():
    try:
        with open(_TURNS_PATH, encoding="utf8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_turns(turns):
    tmp = _TURNS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf8") as fh:
        json.dump(turns, fh, ensure_ascii=False)
    os.replace(tmp, _TURNS_PATH)


def _thread_id(data):
    t = str(data.get("thread") or "").strip()
    return re.sub(r"[^A-Za-z0-9_.-]", "_", t)[:64]


def _thread_history(tid):
    """The thread's turns in chat form, oldest first, bounded."""
    out = []
    for t in _load_turns().get(tid, [])[-_TURNS_KEEP:]:
        role = t.get("role")
        body = str(t.get("content", ""))[:2000]
        if role in ("user", "assistant") and body:
            out.append({"role": role, "content": body})
    return out


def _append_turn(tid, role, content, jobs=None):
    content = str(content or "").strip()
    if not content:
        return
    with _lock:
        turns = _load_turns()
        thread = turns.setdefault(tid, [])
        entry = {"role": role, "content": content[:2000], "at": time.time()}
        if jobs:
            entry["jobs"] = [str(j)[:40] for j in jobs][:8]
        thread.append(entry)
        if len(thread) > _TURNS_KEEP:
            del thread[:len(thread) - _TURNS_KEEP]
        _save_turns(turns)


def _plan_digest(plan):
    """What the farm says it is about to make — compact enough to remember."""
    bits = [str(plan.get("reply") or "").strip()[:600]]
    for s in (plan.get("shots") or [])[:6]:
        if isinstance(s, dict):
            bits.append(f"- {str(s.get('title') or 'shot')[:40]}: "
                        f"{str(s.get('prompt') or '')[:220]}")
    return "\n".join(b for b in bits if b)


def _redo_context(job_id, note):
    """Rebuild 'what I made, and what Eric says is wrong' into one instruction.

    A redo is not a fresh prompt: the brain has to see the take it already made
    (its actual prompt and settings) plus Eric's complaint, or it cheerfully
    re-renders the thing he just rejected.
    """
    with _lock:
        job = _load_state()["jobs"].get(str(job_id))
    if not job:
        return f"REDO request. Eric is unhappy with a shot the farm made, but that " \
               f"job id is gone ({job_id}). Ask him what it was.\n" \
               f"HIS NOTE: {str(note)[:1200]}"
    made = (f"job {job.get('id')} | status={job.get('status')} | "
            f"{job.get('aspect')}/{job.get('quality')}/{job.get('duration')}s"
            + (f" | title={job.get('title')}" if job.get("title") else ""))
    seq = job.get("sequence") or {}
    if seq:
        made += f" | shot {int(seq.get('index', 0)) + 1} of {seq.get('count')}"
    return (
        "REDO request — the farm already made this and Eric is not happy with it.\n"
        f"WHAT WAS MADE ({made}):\n"
        f"PROMPT: {str(job.get('prompt', ''))[:900]}\n"
        + (f"REFERENCE IMAGE: {os.path.basename(str(job.get('image_path')))}\n"
           if job.get("image_path") else "")
        + f"\nWHAT ERIC WANTS DIFFERENT: {str(note)[:1200]}\n"
        "\nChange only what he complained about, keep everything he didn't, and do not "
        "repeat the take described above. Reply to him in one or two sentences saying "
        "what you understood, then give the corrected shot(s)."
    )


def _builder_plan_handler(data):
    tid = _thread_id(data)
    message = str(data.get("message", ""))[:2000]
    # Server-side memory wins over whatever the caller carries: two devices on one
    # thread must not get two different memories.
    history = _thread_history(tid) if tid else (data.get("history") or [])
    if data.get("redo_of"):
        message = _redo_context(data.get("redo_of"), message)
    plan = _builder_plan(message, history, data.get("site"))
    if tid:
        _append_turn(tid, "user", message)
        _append_turn(tid, "assistant", _plan_digest(plan),
                     jobs=[s.get("job_id") for s in (plan.get("shots") or [])
                           if isinstance(s, dict) and s.get("job_id")])
    return plan


def _director_note(goal, job, next_job, clip_path):
    """Watch the tail of the clip we just made (3 frames — fast + reliable vs
    video_url which times out) and produce continuity direction for the next."""
    try:
        tmpdir = os.path.join(CONFIG["vpipe_sandbox"], "seq")
        os.makedirs(tmpdir, exist_ok=True)
        frames = []
        for i, ss in enumerate(("-4", "-2", "-0.2")):
            fp = os.path.join(tmpdir, f"qa_{job['id']}_{i}.jpg")
            r = subprocess.run(["ffmpeg", "-y", "-ss", ss, "-i", clip_path,
                                "-vframes", "1", "-vf", "scale=512:-2", "-q:v", "5", fp],
                               capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.exists(fp) and os.path.getsize(fp) > 2000:
                frames.append(base64.b64encode(open(fp, "rb").read()).decode())
        if not frames:
            return None
        nxt = (next_job or {}).get("prompt", "(the next shot)")
        content = [{"type": "text", "text":
            "You are the continuity director for a multi-shot AI film. These 3 frames are the TAIL "
            f"(last ~4 seconds) of the clip that was just generated. The overall scene goal: {goal}\n"
            f"This clip's brief was: {job.get('prompt','')}\n"
            f"The NEXT shot brief is: {nxt}\n\n"
            "Reply in strict JSON only: "
            '{"end_state": "subject position/pose/action in the final frame", '
            '"camera_end": "where the camera ended (framing + movement state)", '
            '"tempo": "pacing and energy", '
            '"note_to_next_shot": "1-2 sentences of direction so the next shot matches continuously (camera entry, subject pose, light, energy)"}'}]
        for i, b64 in enumerate(frames):
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        body = {"model": CONFIG.get("snail_model", "snail-top"), "max_tokens": 500,
                "messages": [{"role": "user", "content": content}]}
        req = urllib.request.Request(
            CONFIG.get("snail_api_base") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {CONFIG.get('snail_api_key')}",
                     "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            d = json.loads(resp.read().decode())
        c = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        m = re.search(r"\{.*\}", c, re.S)
        if not m:
            return None
        j = json.loads(m.group(0))
        note = (f"Director continuity note (match the previous clip, which ended with: "
                f"{j.get('end_state','?')}; camera ended: {j.get('camera_end','?')}; tempo: {j.get('tempo','?')}). "
                f"{j.get('note_to_next_shot','')}")
        print(f"[sora] director note for {next_job and next_job.get('id')}: {note[:160]}", flush=True)
        return note
    except Exception as e:
        print(f"[sora] director note failed (continuing without): {e}", flush=True)
        return None


PREP_PY = "/Users/snailmac/mlx-env/bin/python3"
PREP_SCRIPT = '''
import sys
from PIL import Image, ImageOps
src, dst, w, h = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
zoom = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
focus = sys.argv[6] if len(sys.argv) > 6 else "center"
im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
target = w / h
iw, ih = im.size
cw, ch = iw, int(iw / target)
if ch > ih:
    ch, cw = ih, int(ih * target)
cw = max(32, int(cw / zoom)); ch = max(32, int(ch / zoom))
x = (iw - cw) // 2
if focus == "face":
    x = (iw - cw) // 2; y = max(0, int((ih - ch) * 0.15))
elif focus == "top":
    x = (iw - cw) // 2; y = 0
else:
    y = (ih - ch) // 2
    # HEAD SAFETY (measured 2026-10-02): a pure centre crop beheads a portrait
    # whose subject sits low in the frame - the tall 9:16 and 4:5 buckets throw
    # away the top and bottom of a landscape photo, so a head above the crop line
    # is cut off. If a face detector finds one, shift the window just enough to
    # keep it inside (with a small margin). No detector / no face -> unchanged.
    try:
        import numpy as np, cv2
        g = np.asarray(im.convert("L"))
        cc = cv2.CascadeClassifier(cv2.data.haarcascades +
                                   "haarcascade_frontalface_default.xml")
        f = cc.detectMultiScale(g, 1.1, 5, minSize=(max(24, iw // 25),) * 2)
        if len(f):
            fx, fy, fw, fh = max(f, key=lambda r: r[2] * r[3])
            m = int(fh * 0.35)
            top, bot = fy - m, fy + fh + m
            if bot - top <= ch:
                # the face fits: move the window the least it takes to hold it
                if top < y:
                    y = top
                if bot > y + ch:
                    y = bot - ch
            else:
                # taller than the canvas: centre it. Pinning the top would push
                # the chin out; centring keeps eyes and mouth inside, which is
                # what reads as "the whole head".
                y = int(fy + fh / 2 - ch / 2)
            y = max(0, min(y, ih - ch))
    except Exception:
        pass
im = im.crop((x, y, x + cw, y + ch))
im = im.resize((w, h), Image.LANCZOS)
im.save(dst, "PNG")
print("ok")
'''


def _prep_image(src, w, h, zoom=1.0, focus="center"):
    """Crop/zoom a reference image to the exact canvas AR (never stretch)."""
    out = os.path.join(CONFIG["vpipe_sandbox"], "uploads", f"prep_{int(time.time()*1000)}.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    r = subprocess.run([PREP_PY, "-c", PREP_SCRIPT, src, out, str(w), str(h),
                        str(zoom), focus], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"image prep failed: {r.stderr[-200:]}")
    return out


def _recover_orphans():
    """On startup: any job stuck in 'rendering' belongs to a service that died
    mid-render. Kill orphaned vpipe render subprocesses and requeue the jobs."""
    import signal
    # MEASURED 2026-10-02: killing the orphan and starting the renderer in the
    # SAME instant deadlocks the new process — it loads the whole model, then
    # sits at 0.0% CPU forever (the Metal device is still held by the dying one).
    # So SIGTERM, then WAIT for the process to actually go, escalating to SIGKILL
    # only if it will not. Nothing may render until the GPU is genuinely free.
    def _alive():
        pids = []
        try:
            out = subprocess.run(["pgrep", "-f",
                                  "Helpers/vpipe --launch pipelines/minimax-h3-"],
                                 capture_output=True, text=True, timeout=10)
            pids = [int(p) for p in out.stdout.split() if p.strip()]
        except Exception:
            pass
        return pids
    try:
        for pid in _alive():
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"[sora] recovered: SIGTERM orphaned vpipe pid {pid}", flush=True)
            except (ProcessLookupError, ValueError):
                pass
        deadline = time.time() + 30
        while time.time() < deadline and _alive():
            time.sleep(0.5)
        stuck = _alive()
        for pid in stuck:
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"[sora] recovered: SIGKILL orphaned vpipe pid {pid}", flush=True)
            except (ProcessLookupError, ValueError):
                pass
        if stuck:
            deadline = time.time() + 15
            while time.time() < deadline and _alive():
                time.sleep(0.5)
        left = _alive()
        print(f"[sora] recovered: GPU clear, no orphaned vpipe"
              if not left else
              f"[sora] recovered: {len(left)} vpipe process(es) still alive — "
              f"renders may hang", flush=True)
    except Exception as e:
        print(f"[sora] orphan scan: {e}", flush=True)
    with _lock:
        state = _load_state()
        changed = False
        for jid, job in state["jobs"].items():
            if job["status"] == "rendering":
                job["status"] = "queued"
                changed = True
                print(f"[sora] recovered: requeued orphaned render {jid}", flush=True)
        if changed:
            _save_state(state)


def main():
    if not CONFIG["secret"]:
        raise SystemExit(
            "Sora secret not set — create sora_config.json or export SORA_SECRET")
    # post lane must start first thing after renderer — it drains finished clips
    # to R2 and flips jobs to done; missing it leaves every render stuck in "rendering"
    threading.Thread(target=_post_loop, daemon=True).start()
    # the chron lane: enqueues scheduled jobs (no priority) and delivers finished
    # ones to Discord. Safe to start before the renderer — it only writes queue rows.
    threading.Thread(target=_sched_loop, daemon=True).start()
    if CONFIG["backend"] == "vpipe":
        # SYNCHRONOUS on purpose: this waits for any orphaned vpipe to actually
        # exit. Starting the renderer in the same instant races it into a Metal
        # deadlock that looks like a permanently stuck render (0.0% CPU).
        _recover_orphans()
        slots = max(1, int(CONFIG.get("render_slots", 2)))
        for _ in range(slots):
            threading.Thread(target=_renderer_loop, daemon=True).start()
        print(f"[sora] {slots} render slots", flush=True)
        threading.Thread(target=_cuts_sync_loop, daemon=True).start()
    else:
        threading.Thread(target=_comfy_poll_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", CONFIG["port"]), Handler)
    print(f"[sora] listening on 127.0.0.1:{CONFIG['port']} "
          f"(backend={CONFIG['backend']})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
