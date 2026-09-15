#!/usr/bin/env python3
"""sNora render service — public video generation backend.

Bridges a public Cloudflare worker (or any front-end) to the local render
farm. Pure stdlib; runs under a service manager on :7789, exposed publicly
via your tunnel of choice.

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
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")

CONFIG = {
    "port": 7789,
    "secret": os.environ.get("SORA_SECRET", ""),
    "worker_base": "https://your-frontend.example.com",
    "backend": "vpipe",
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
        "wide":    {"width": 832, "height": 480},
    },
    "durations": {3: 73, 4: 90, 5: 124, 8: 192, 10: 243, 15: 362},   # seconds -> frames (17n+5)
    "vpipe_pipeline_fl2va": "pipelines/minimax-h3-first-last-to-video.vpipeline",
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
SOUND_SPOKEN = ("overall_soundscape: one clear human voice speaking the quoted line with quiet "
                "room tone between words, plus the scene's own natural ambient sound "
                "(wind, fire, footsteps, machines)")

def _scaffold(prompt):
    music = ("subtle background music matching the mood of the scene"
             if MUSIC_WORDS.search(prompt) else "none")
    if _has_dialogue(prompt):
        return (f"integrated_multimodal_description: [Shot 1] {prompt} "
                f"{SPOKEN_CLAUSE}\n"
                f"{SOUND_SPOKEN}\n"
                f"non_diegetic_music: {music}")
    return (f"integrated_multimodal_description: [Shot 1] {prompt} "
            f"Nobody speaks — the characters make no words, only natural reactions.\n"
            f"overall_soundscape: quiet room tone with soft natural ambient sound only — no voices of any kind; "
            f"the only audio is the scene itself (wind, fire, footsteps, machines)\n"
            f"non_diegetic_music: {music}")


# ── upload to worker (worker writes R2) ───────────────────────────────

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

def _scaffold_fl2va(prompt, dur):
    """FL2VA with first+last = same image: declare both alignment slots."""
    music = ("subtle background music matching the mood of the scene"
             if MUSIC_WORDS.search(prompt) else "none")
    end = f"{max(0.1, dur - 0.17):.2f}"
    if _has_dialogue(prompt):
        return (f"How the reference pictures align with the target video: "
                f"Picture 1 (from Shot 1) aligns with the 0.00-second mark; "
                f"Picture 2 (from Shot 1) aligns with the {end}-second mark.\n"
                f"integrated_multimodal_description: [Shot 1] {prompt} "
                f"{SPOKEN_CLAUSE}\n"
                f"{SOUND_SPOKEN}\n"
                f"non_diegetic_music: {music}")
    return (f"How the reference pictures align with the target video: "
            f"Picture 1 (from Shot 1) aligns with the 0.00-second mark; "
            f"Picture 2 (from Shot 1) aligns with the {end}-second mark.\n"
            f"integrated_multimodal_description: [Shot 1] {prompt} "
            f"Nobody speaks — the characters make no words, only natural reactions.\n"
            f"overall_soundscape: quiet room tone with soft natural ambient sound only — no voices of any kind; "
            f"the only audio is the scene itself (wind, fire, footsteps, machines)\n"
            f"non_diegetic_music: {music}")


def _scaffold_ref2va(prompt, voiceless=False):
    """Ref2VA: the references carry the subject/voice, prompt carries the action.
    voiceless (identity mode) renders against a SILENT wav — never claim that
    wav is the voice, the model garbles trying to clone nothing."""
    music = ("subtle background music matching the mood of the scene"
             if MUSIC_WORDS.search(prompt) else "none")
    if voiceless:
        # say nothing about voices when the prompt has no line — claiming a
        # voice there is what made the model babble gibberish to fill it
        if _has_dialogue(prompt):
            speech = ("The character delivers the quoted line clearly in natural English, "
                      "one clean breath, lips moving in sync with every word.\n")
            sound = ("overall_soundscape: one clear human voice speaking the quoted line with quiet "
                     "room tone between words, plus the scene's own natural ambient sound\n")
        else:
            speech = ("Nobody speaks — the characters make no words, only natural reactions. "
                      "No voice of any kind, no background chatter, no babble.\n")
            sound = ("overall_soundscape: quiet room tone with soft natural ambient sound only — "
                     "no voices of any kind; the only audio is the scene itself "
                     "(wind, machines, footsteps)\n")
        return (f"integrated_multimodal_description: [Shot 1] "
                f"The subject shown in <Picture 1>, {prompt}\n"
                f"{speech}{sound}"
                f"non_diegetic_music: {music}")
    return (f"integrated_multimodal_description: [Shot 1] "
            f"The subject shown in <Picture 1>, speaking with the voice of <Audio 1>, {prompt}\n"
            f"Any spoken lines are in clear English.\n"
            f"overall_soundscape: natural diegetic sound matching the scene and its action, clean and unprocessed\n"
            f"non_diegetic_music: {music}")


# ── vpipe backend ─────────────────────────────────────────────────────

def _vpipe_ready():
    return os.path.isdir(
        os.path.join(CONFIG["vpipe_cwd"], "models", *CONFIG["vpipe_model"].split("/")))


def _scaffold_i2v(prompt):
    """i2v: one first-frame anchor (the chain/seed image), motion continues from it."""
    music = ("subtle background music matching the mood of the scene"
             if MUSIC_WORDS.search(prompt) else "none")
    spoken = _has_dialogue(prompt)
    clause = f" {SPOKEN_CLAUSE}" if spoken else (
        " Nobody speaks — the characters make no words, only natural reactions."
        " No voice of any kind, no background chatter, no babble.")
    sound = ("overall_soundscape: natural diegetic sound matching the scene and its action, "
             "clean and unprocessed" if spoken else
             "overall_soundscape: quiet room tone with soft natural ambient sound only — "
             "no voices of any kind; the only audio is the scene itself "
             "(wind, fire, footsteps, machines)")
    return (f"For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n"
            f"integrated_multimodal_description: [Shot 1] {prompt}{clause}\n"
            f"{sound}\n"
            f"non_diegetic_music: {music}")


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
        tr = "cut"  # identity shots rebuild the user in a new scene — never chain-anchor
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
    if img and (vox or identity or multi):
        pipeline = CONFIG["vpipe_pipeline_ref2va"]
        prompt = _scaffold_ref2va(job["prompt"], voiceless=not vox)
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
        prompt = _scaffold_i2v(job["prompt"])
        extra = [
            "--stage-cfg", f"load-image::url=[\"{chain_from}\"]",
            "--stage-cfg", f"first-frame::width={a['width']}",
            "--stage-cfg", f"first-frame::height={a['height']}",
        ]
        mode = "i2v(chain)"
    elif seq is not None and img:
        pipeline = CONFIG["vpipe_pipeline_i2v"]
        prompt = _scaffold_i2v(job["prompt"])
        extra = [
            "--stage-cfg", f"load-image::url=[\"{img}\"]",
            "--stage-cfg", f"first-frame::width={a['width']}",
            "--stage-cfg", f"first-frame::height={a['height']}",
        ]
        mode = "i2v(seed)"
    elif img:
        pipeline = CONFIG["vpipe_pipeline_fl2va"]
        prompt = _scaffold_fl2va(job["prompt"], int(job.get("duration") or 5))
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
        prompt = _scaffold(job["prompt"])
        extra = [
            "--stage-cfg", f"model-select::hf_dir={CONFIG['vpipe_model']}",
        ]
        mode = "t2v"

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
                _PROGRESS[job["id"]] = {"pct": int(m.group(1)),
                                        "stage": m.group(2), "ts": time.time()}

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
            items = []
            for a in SHACK.get("assets", []):
                items.append({"key": a["key"], "type": "image", "set": a.get("set", ""),
                              "name": a.get("key", "").replace("_", " "), "desc": a.get("desc", ""),
                              "r2": f"library/{a['key']}.png"})
            v = SHACK.get("character", {}).get("voice", {})
            if v.get("key"):
                items.append({"key": v["key"], "type": "voice", "set": "voices",
                              "name": f"{v.get('key','')} voice (reference)", "desc": v.get("desc", ""),
                              "r2": f"library/voice_{v['key']}.wav"})
            return self._send({"items": items})
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
        if not self._authed():
            return self._send({"error": "unauthorized"}, 401)
        with _lock:
            state = _load_state()
        if path == "/jobs":
            jobs = sorted(state["jobs"].values(), key=lambda j: -j["created"])[:100]
            return self._send({"jobs": [self._public(j) for j in jobs]})
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
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authed():
            return self._send({"error": "unauthorized"}, 401)
        m = re.fullmatch(r"/jobs/([a-z0-9_]+)/approve", self.path.split("?")[0].rstrip("/"))
        if m:  # the approval gate — the user's tap is the only thing that opens it
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
            # uploaded voice stay (the user keeps his originals); _delete_guarded
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
            # is waiting for the user, it is not rendering). The job currently
            # rendering keeps going; uploads stay on disk (the user keeps photos).
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
            # nothing an agent planned can reach the GPU until the user taps approve.
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
                    if i == 0 and s.get("image_key"):
                        for a in SHACK.get("assets", []):
                            if a["key"] == s["image_key"]:
                                image_path = a["file"]
                                break
                    # chains share ONE canvas — mixed orientations are what makes a sequence cursed
                    aspect = chain_aspect
                    if s.get("voice") and i > 0 and s.get("voice_key", DEFAULT_VOICE_KEY) == DEFAULT_VOICE_KEY:
                        # VO shot: ref2va standalone with the user's face + voice
                        for a in SHACK.get("assets", []):
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
                        "backend": CONFIG["backend"], "status": "awaiting_approval" if await_approval else "queued", "ip": ip,
                        "created": base_time + i * 0.001, "seed": seed,
                    }
                    # VO shots: standalone ref2va with the user's face + voice
                    if s.get("voice") and s.get("image_key"):
                        for a in SHACK.get("assets", []):
                            if a["key"] == s["image_key"]:
                                job["image_path"] = a["file"]
                                job["voice_path"] = SHACK.get("character", {}).get("voice", {}).get("file")
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
        key_paths = []
        for k in (data.get("image_keys") or [])[:9]:
            if isinstance(k, str) and re.fullmatch(r"[a-z0-9_]+", k):
                for a in SHACK.get("assets", []):
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
            if data.get("voice"):
                voice_path = _save_dataurl(data["voice"], "vox")
                if not image_path:
                    return self._send(
                        {"error": "custom voice needs an input image too (the voice rides on a character)"}, 400)
        except ValueError as e:
            return self._send({"error": str(e)}, 400)
        if voice_path:
            voice_path = _to_wav(voice_path)
        # builder library references (validated against the manifest)
        if not image_path and data.get("image_key"):
            for a in SHACK.get("assets", []):
                if a["key"] == data["image_key"]:
                    image_path = a["file"]
                    break
        if not voice_path and data.get("voice_key"):
            if data["voice_key"] == DEFAULT_VOICE_KEY:
                voice_path = SHACK.get("character", {}).get("voice", {}).get("file")
                if voice_path and not os.path.exists(voice_path):
                    voice_path = None
        if voice_path and not image_path:
            return self._send({"error": "voice needs an image"}, 400)

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
                "has_image": bool(image_path), "has_voice": bool(voice_path),
                "ref_mode": ref_mode, "zoom": zoom, "focus": focus,
                "image_path": image_path or (key_paths[0] if key_paths else None),
                "image_paths": key_paths,
                "voice_path": voice_path,
                "backend": CONFIG["backend"],
                # gated jobs wait for the user's tap; everything else queues
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
    """OPTIONAL hook: pull reference clips into the render box so the Shot
    Planner can point commands at real local files. Left inert for a fresh
    install — wire it to your own asset source if you want live uploads."""
    return


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


def _sets_brief():
    """One WORLD line per set defined in shack_manifest.json (the buyer's own)."""
    lines = []
    for st in SHACK.get("sets", []):
        lines.append(f"- Set {st.get('name', 'SET')} ({st.get('aliases', '')}): "
                     f"{st.get('desc', 'describe your set in shack_manifest.json')}.\n")
    return "".join(lines) or "- (no sets yet — add them under \"sets\" in shack_manifest.json)\n"


def _voice_brief():
    v = SHACK.get("character", {}).get("voice", {})
    if v.get("key"):
        return (f"- The subject's voice reference is available (voice key "
                f"'{v['key']}'): {v.get('desc', 'your voice')}.\n")
    return ""


DEFAULT_VOICE_KEY = SHACK.get("character", {}).get("voice", {}).get("key", "myvoice")


def _builder_system():
    lib = [{"key": a["key"], "set": a["set"], "desc": a["desc"]} for a in SHACK.get("assets", [])]
    return (
        "You are the sNora Scene Builder — a master director/showrunner who chats with the user, plans MiniMax H3 video shots for the render farm, and directs their continuity.\n\n"
        "WORLD — the user's standing character and sets (edit shack_manifest.json to describe YOURS):\n"
        f"- The star is {SHACK.get('character', {}).get('name', 'the user')}: {SHACK.get('character', {}).get('look', 'describe your recurring subject in shack_manifest.json')}. Any mention of 'me', 'my', 'I' = this subject.\n"
        f"{_sets_brief()}"
        f"{_voice_brief()}\n\n"
        "NAMING — SAY THE REAL NAMES (casting/direction notes for the user's own render farm, not public claims):\n"
        "- When the user's words point at a real actor, character, film, show, brand, landmark or studio, the job prompt carries THAT NAME — plus the specific film and scene when it helps. Hedging is a defect: a vague descriptor renders as generic mush; the name is what steers the shot.\n"
        "- Never 'a rugged action star' — name the actor. Never 'a familiar cartoon creature' — name it. Never 'in the style of a big animation studio' — name the studio and describe its look. Casting specificity is what steers the shot.\n"
        "- Banned hedges, never in a prompt/goal/reply: 'inspired by', 'in the vein of', 'a certain', 'evoking', 'somewhat like'.\n"
        "- A shoutout line from the user — their own brands or any name they lift — is dialogue content carried verbatim, not advertising to soften.\n"
        "- Casting language, not false claims: 'a named star as the ranch hand' (a direction to the renderer) — but never state as fact that someone starred in, endorsed, or appears in the user's clip.\n"
        "- The carve-out — the user's own family and their photos: keep them relational ('the kid', 'his wife') with his reference photos; never guess at names for people he knows.\n"
        "- No apologies, no disclaimers, no safety preambles — not in a job prompt, not anywhere. Whether to publish a clip is the user's call with the finished clip in front of them; the prompt's job is to name the shot they asked for, not to hide the choice from him.\n\n"
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
        "- Example: one wide set master at zoom 1.0 is the establishing shot; the same photo at zoom 2.0/focus 'face' is the close-up. That's how you build coverage — derive, don't invent.\n"
        "- In a chain, zooming IN across shots (1.0 → 1.4 → 2.0) creates a natural push-in feel.\n"
        "- All shots in one chain share ONE aspect ratio (the first shot's) — mixed orientations in a sequence are forbidden.\n\n"
        "REFERENCE INTENT — THE MOST IMPORTANT ROUTING DECISION:\n"
        "Every shot that uses an image must set ref_mode:\n"
        "- ref_mode='scene' — the photo's LOCATION AND TIME OF DAY ARE the scene (first-frame anchor). Use ONLY when the user wants to continue in the photo's actual setting (same place, same time of day).\n"
        "- ref_mode='identity' — the photo defines WHO the subject is, NOT where they are. Use whenever the user describes a NEW OR DIFFERENT setting from the photo: different location, daytime when the photo is night, a whole new world (the beach, space, a fantasy set). Ref2VA carries the subject into the freshly-prompted scene.\n"
        "- If the user's described scene matches no library photo's setting: either (a) ref_mode='identity' with the closest subject image — the prompt's scene wins, or (b) image=null with a rich character description in the prompt (pure text-to-video).\n"
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
        "- On-camera dialogue: <d>[English] the words</d> + voice=true (the subject's reference voice, Ref2VA pipeline).\n"
        "- OFF-SCREEN VOICEOVER ( narration over any scene ): voice=true AND prompt phrased exactly like: 'the subject narrates in an off-screen voiceover — <the narration>, while their lips remain completely closed' or 'says in an off-screen voiceover ... while his lips remain completely closed'. For pure b-roll narration use image=null in chained plans, or the subject shot with image=<a pose key>. VO shots work best as their own shot (transition='cut' into or out of them).\n"
        "- Max ~2 short VO beats (≤10 words each) per 5s clip; insert silent visual beats between dense narration.\n"
        "- No dialogue = ambient sound only. Music only via non_diegetic_music when the user asks.\n"
        "- Single (non-chained) image shots: the image becomes BOTH first and last frame — write motion that starts and settles near that pose.\n\n"
        "DIALOGUE DISCIPLINE — every spoken line, VO or on-camera:\n"
        "- Word budget ≈ 2.2 spoken words per second of clip: 3s≤7, 5s≤11, 8s≤18, 10s≤22, 15s≤33. The farm 400s an over-budget line.\n"
        "- Banned filler in any spoken line: hey, hey y'all, y'all, just, literally, like, you know, I mean, uh, um, basically, actually, guys, okay so, 'so' as an opener, 'well' as an opener.\n"
        "- A spoken line must NEVER narrate what the shot already shows — it adds a want, a joke, a name, a memory.\n"
        "- One intent per line; one speaker per shot. Write it as: He says, <emotion>: \"<line>\" — placed at the END of the scene description, before the Soundscape line.\n"
        "- When the scene invites words and the user gave none, WRITE one line that fits the budget (proactive fill-in); a line over budget gets rewritten shorter, meaning and voice intact.\n"
        "- Leave at least 30% of the clip wordless so the delivery can breathe.\n\n"
        "IMAGE LIBRARY pose rules: match the shot's action to the pose in the key (match action to pose — desk keys for desk shots, chair keys for talking-to-camera, outdoor keys for outside).\n\n"
        "OUTPUT — strict JSON only, no markdown fences:\n"
        '{"reply": "<1-3 friendly sentences to the user>", "goal": "<one-sentence synopsis of the whole scene>", '
        '"shots": [{"title": "short name", "prompt": "<complete scene: action + camera + any dialogue>", "aspect": "square", "duration": 5, "quality": "standard", "image": "<library key or null>", "voice": false, "transition": "chain", "ref_mode": "scene"}]}\n'
        "(shots.transition ∈ chain|cut|dissolve|fade|dip|flash|slide|wipe|whip|glitch|iris; shots.ref_mode ∈ scene|identity; the FIRST shot's transition is ignored.)"
    )


def _builder_plan(message, history):
    base = CONFIG.get("snail_api_base")
    key = CONFIG.get("snail_api_key")
    if not base or not key:
        raise RuntimeError("snail-api key not configured")
    msgs = [{"role": "system", "content": _builder_system()}]
    for h in (history or [])[-6:]:
        role = h.get("role", "user")
        if role in ("user", "assistant"):
            msgs.append({"role": role, "content": str(h.get("content", ""))[:2000]})
    msgs.append({"role": "user", "content": message[:2000]})
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
    if not plan.get("shots") and isinstance(plan.get("reply"), str) and plan["reply"].strip().startswith("{"):
        try:
            plan = json.loads(plan["reply"])
        except json.JSONDecodeError:
            pass
    valid_keys = {a["key"] for a in SHACK.get("assets", [])}
    shots = []
    for s in plan.get("shots", [])[:6]:
        if not isinstance(s, dict) or not s.get("prompt"):
            continue
        shots.append({
            "title": str(s.get("title", "shot"))[:60],
            "prompt": str(s["prompt"])[:2000],
            "aspect": s.get("aspect") if s.get("aspect") in CONFIG["aspects"] else "square",
            "duration": int(s["duration"]) if int(s.get("duration", 5) or 5) in CONFIG["durations"] else 5,
            "quality": s.get("quality") if s.get("quality") in CONFIG["qualities"] else "standard",
            "image": s.get("image") if s.get("image") in valid_keys else None,
            "voice": bool(s.get("voice")),
            "transition": s.get("transition") if s.get("transition") in (
                "chain", "cut", "dissolve", "fade", "dip", "flash", "slide", "wipe", "whip", "glitch", "iris") else "chain",
            "zoom": min(2.5, max(1.0, float(s.get("zoom") or 1.0))),
            "focus": s.get("focus") if s.get("focus") in ("center", "face", "top") else "center",
            "ref_mode": s.get("ref_mode") if s.get("ref_mode") in ("scene", "identity") else "scene",
        })
    return {"reply": str(plan.get("reply", ""))[:600], "shots": shots}


# ── conversation memory ────────────────────────────────────────────────────
# the user talks to the farm and expects it to remember. The phone used to be the only
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
    """Rebuild 'what I made, and what the user says is wrong' into one instruction.

    A redo is not a fresh prompt: the brain has to see the take it already made
    (its actual prompt and settings) plus the user's complaint, or it cheerfully
    re-renders the thing he just rejected.
    """
    with _lock:
        job = _load_state()["jobs"].get(str(job_id))
    if not job:
        return f"REDO request. the user is unhappy with a shot the farm made, but that " \
               f"job id is gone ({job_id}). Ask him what it was.\n" \
               f"HIS NOTE: {str(note)[:1200]}"
    made = (f"job {job.get('id')} | status={job.get('status')} | "
            f"{job.get('aspect')}/{job.get('quality')}/{job.get('duration')}s"
            + (f" | title={job.get('title')}" if job.get("title") else ""))
    seq = job.get("sequence") or {}
    if seq:
        made += f" | shot {int(seq.get('index', 0)) + 1} of {seq.get('count')}"
    return (
        "REDO request — the farm already made this and the user is not happy with it.\n"
        f"WHAT WAS MADE ({made}):\n"
        f"PROMPT: {str(job.get('prompt', ''))[:900]}\n"
        + (f"REFERENCE IMAGE: {os.path.basename(str(job.get('image_path')))}\n"
           if job.get("image_path") else "")
        + f"\nWHAT THE USER WANTS DIFFERENT: {str(note)[:1200]}\n"
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
    plan = _builder_plan(message, history)
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


PREP_PY = os.environ.get("SNORA_PREP_PY", "python3")  # needs Pillow installed
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
    try:
        out = subprocess.run(["pgrep", "-f",
            "Helpers/vpipe --launch pipelines/minimax-h3-"],
            capture_output=True, text=True, timeout=10)
        pids = [p for p in out.stdout.split() if p.strip()]
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
                print(f"[sora] recovered: killed orphaned vpipe pid {pid}", flush=True)
            except (ProcessLookupError, ValueError):
                pass
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
    if CONFIG["backend"] == "vpipe":
        threading.Thread(target=_recover_orphans, daemon=True).start()
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
