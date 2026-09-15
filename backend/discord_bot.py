#!/usr/bin/env python3
"""sNora Discord bot — @sNora <idea> in your Discord server → render farm → clip back in chat.

Runs beside the render service
(sora_service.py on :7789, launched by your service manager).

Flow for a visitor's "@sNora make a clip of ..." message:
  1. bot acks with "🎬 On it…"
  2. snail-api (the app's own LLM lane, snail-top) turns the ask into a proper
     one-shot scene prompt — defaults: standard quality, wide (16:9), 5s
  3. job is POSTed to the normal sNora queue (same farm, same rules)
  4. the ack message is EDITED with live queue position / rendering state
  5. when the job is done the mp4 is posted back in the channel as an
     attachment (grabbed from the local sandbox, or from R2 via the worker)

Config lives in sora_config.json under "discord" (see DEFAULTS below).
The bot token is never stored in the repo — it is read from token_file.
"""
import asyncio
import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

import discord

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
WATCH_FILE = os.path.join(HERE, "discord_watch.json")

DEFAULTS = {
    "service_url": "http://127.0.0.1:7789",
    "token_file": os.path.join(HOME, ".sora_discord_token"),
    "allowed_guild_ids": [],      # empty = every server the bot is in
    "allowed_channel_ids": [],    # empty = every channel in an allowed server
    "max_active_per_user": 1,     # bot-side guard (farm has its own too)
    "min_gap_secs": 15,           # one ask per user per this window
    "poll_secs": 20,              # queue poll cadence
    "max_wait_secs": 3 * 3600,    # give up watching after 3h
    "await_approval": False,      # True = hold Discord jobs at the owner's gate
    "nanny": True,                # hard PG gate on asks before anything runs
    "nanny_off_guilds": [],       # server IDs with the PG gate OFF (private adult channels)
    "owner_ids": [],              # user IDs (the operator + helpers): no nanny, no spam guards
    "snora_token": "",            # R2 fallback fetch (X-Sora-Token)
}

DURATIONS = {3, 4, 5, 8, 10, 15}
QUALITIES = {"fast", "standard", "best"}
ASPECTS = {"square", "portrait", "portrait-hd", "wide"}
DEFAULT_ASPECT = "wide"        # Discord default: 16:9
DEFAULT_QUALITY = "standard"   # "normal"

SPOKEN_RE = re.compile(r'["“”]([^"“”]{3,})["“”]')
D_TAG_RE = re.compile(r"<d>(.*?)</d>", re.S)
SHAPE_WORDS = re.compile(r"\b(wide|16:9|landscape|square|1:1|portrait|vertical|tall|"
                         r"9:16|4:3|3:4|horizontal)\b", re.I)

# ── nanny layer: cheap deterministic gate, runs before the LLM or the GPU ──

NANNY_RE = re.compile(
    r"\b(porn\w*|erotica|erotic|nude|nudes|nudity|naked|blow ?job|hand ?job|cum|cumshot|orgy|"
    r"milf|incest|bestiality|boob\w*|penis|vagina|genital\w*|masturbat\w*|topless|sex tape|"
    r"horny|sexual|sex)\b", re.I)
MINOR_RE = re.compile(r"\b(kids?|child(ren)?|minors?|teens?|loli\w*)\b", re.I)
MINOR_HOT_RE = re.compile(r"(child(ren)?'s? (porn|sex|abuse)|schoolgirls?|schoolboys?|loli\w*|cp\b)", re.I)
# PG-13, not G: mild (damn/hell/crap/ass/bullshit) passes; these do not.
PROFANITY_RE = re.compile(
    r"\b(fuck\w*|shitt?ing?|cunt\w*|bitch\w*|ass ?hole|dick ?head|piss\w*|whore|slut\w*|"
    r"fag(got)?|nigg\w+|retard\w*|tranny)\b", re.I)


def _is_owner(user_id):
    """Operator allowlist: runs the open lane everywhere and skips bot-side guards."""
    return str(user_id) in [str(u) for u in CFG.get("owner_ids", [])]


def _nanny_on(guild_id):
    """PG lane applies unless globally off or this server is on the open list."""
    if not CFG.get("nanny", True):
        return False
    return str(guild_id) not in [str(g) for g in CFG.get("nanny_off_guilds", [])]


def nanny_check(text, guild_id=None, user_id=None):
    """Return (short refusal reason, hard) for clearly disallowed asks, else None."""
    # hard floor under EVERY lane — illegal content has no override, no guild exemption
    if MINOR_HOT_RE.search(text):
        return ("that one is a hard no, full stop", True)
    if _is_owner(user_id) or not _nanny_on(guild_id):
        return None
    if NANNY_RE.search(text):
        if MINOR_RE.search(text):
            return ("I don't mix kids and adult content", True)
        return ("PG-13 house rules — nothing sexual gets made here", False)
    if PROFANITY_RE.search(text):
        return ("PG-13 here — mild swearing survives, that doesn't", False)
    return None


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(HERE, "sora_config.json")) as f:
            sc = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        sc = {}
    cfg["secret"] = sc.get("secret", "")
    cfg["worker_base"] = sc.get("worker_base", "https://your-frontend.example.com")
    cfg["vpipe_sandbox"] = sc.get("vpipe_sandbox", f"{HOME}/vpipe/sandbox")
    cfg["llm_base"] = sc.get("snail_api_base", "")
    cfg["llm_key"] = sc.get("snail_api_key", "")
    cfg["llm_model"] = sc.get("snail_model", "snail-top")
    cfg.update(sc.get("discord") or {})
    return cfg


CFG = load_cfg()


def _load_library():
    """The Discord-facing slice of the owner's reference manifest. Ship your OWN
    keys in shack_manifest.json and mark the ones safe for public use with
    "discord_ok": true; only those are offered to Discord members, so personal
    or private references stay out of the community's reach."""
    try:
        with open(os.path.join(HERE, "shack_manifest.json")) as f:
            sh = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return [{"key": a["key"], "desc": a.get("desc", a["key"])}
            for a in sh.get("assets", []) if a.get("discord_ok")]


LIBRARY = _load_library()
LIB_KEYS = {a["key"] for a in LIBRARY}

log = logging.getLogger("snora-discord")


# ── farm HTTP (stdlib, sync — always called via asyncio.to_thread) ──────

def _req(url, payload=None, headers=None, method=None, timeout=20):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def farm_headers(user_id=None):
    h = {"X-Sora-Secret": CFG["secret"]}
    if user_id:
        # make the farm's per-IP limits mean per-Discord-user limits
        h["X-Forwarded-For"] = f"discord-{user_id}"
    return h


def farm_get(path):
    return _req(CFG["service_url"] + path, headers=farm_headers(), timeout=25)[1]


def farm_submit(payload, user_id):
    req = urllib.request.Request(
        CFG["service_url"] + "/jobs", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **farm_headers(user_id)})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}


# ── LLM prompt formatter (the app's own snail-api lane) ──────────────────

SYSTEM = (
    "You are sNora, the video-generation bot in this Discord community. A member gives you a "
    "video idea; you turn it into ONE complete scene prompt for the owner's MiniMax H3 render farm "
    "and a short friendly reply. You cannot render anything you haven't planned.\n\n"
    "THE HANDBOOK (condensed):\n"
    "- ONE shot per clip. Stage depth (foreground/midground/background) instead of cutting.\n"
    "- Write the prompt as a complete scene: subject + action + setting + lighting + exactly "
    "ONE camera move in prose ('the camera holds steady', 'slow push in', 'pans left with her "
    "gaze'). Never keyword lists; always say what the camera does or the model invents cuts.\n"
    "- DURATION by content: 3s is the DEFAULT — one punchy beat, quick reaction, pure "
    "atmosphere (≤7 spoken words). Go longer only when the content truly needs it: 5s one "
    "dialogue beat or a meaty action, 8s action+reaction or a two-beat gag, 10s two dialogue "
    "beats or a slow reveal, 15s a full mini-scene with a turn. Allowed: 3, 4, 5, 8, 10, 15.\n"
    "- DIALOGUE is optional and scarce. If the scene invites words, write ONE short line the "
    "member didn't give you, as: He/She says, <emotion>: \"<line>\" at the END of the scene "
    "text. Budget ≈ 2.2 spoken words per second (3s → ≤7 words, 5s → ≤11 words). The farm REJECTS over-budget "
    "lines — shorten them. Banned fillers in speech: hey, y'all, just, literally, like, you "
    "know, I mean, uh, um, basically, actually, guys, so/well openers. A line must never "
    "narrate what the shot already shows. No dialogue = ambient sound only.\n"
    "- NAMING: when the idea points at a real character, film, show, brand or landmark, use "
    "THE REAL NAME as a casting direction ('Arnold Schwarzenegger as the ranch hand'). Vague "
    "hedges ('inspired by', 'a certain') are defects. Never claim anyone actually appears in "
    "the clip; never name real private individuals.\n"
    "- ASPECT: if the member works from a picture (attached or a library key) and did NOT "
    "name a shape, set \"auto\" — the farm sizes the clip to that picture's own ratio. "
    "Otherwise default \"wide\" (16:9); pick \"square\", \"portrait\" or \"portrait-hd\" only "
    "when the member clearly asks for that shape.\n"
    "- QUALITY defaults to \"standard\". Use \"fast\" or \"best\" only if the member clearly "
    "asks for a draft or top quality.\n"
    "- If the member attached a photo, set ref_mode: \"scene\" when the photo's location/time "
    "IS the scene, \"identity\" when the photo shows WHO but the prompt is a new place.\n"
    "- VOICE: an attached audio clip is a VOICE REFERENCE — the character on screen speaks "
    "with that voice. Whenever voice is involved, the prompt must carry ONE short spoken "
    "line (within budget) and the shot MUST have a picture to ride the voice on (attached "
    "photo, else a library key).\n"
    "- IMAGE LIBRARY — the server's standing reference shots (snail characters and bare "
    "sets ONLY). When a member names or points at one of them, set image to that "
    "library KEY and choose ref_mode "
    "(\"scene\" = the photo's place/time IS the scene; \"identity\" = the photo is WHO, the "
    "prompt is a new place). Character/model-sheet keys (HERO SHOT, MODEL SHEET, IDENTITY in "
    "their description) are WHO-references — use \"identity\" with them unless the member "
    "literally wants that photo's background as the scene. A library call must ride on the "
    "actual reference — never re-describe it in text. CHARACTER FIRST: if a named "
    "CHARACTER is IN the scene, the image is the CHARACTER key — bare set/prop keys "
    "only carry establishing shots with no character. Use the library description's own "
    "wording for the character's look; the reference picture carries it, the prompt just "
    "names them and stages the action. No library subject fits: image null "
    "(pure text-to-video). Members may NOT request likenesses of real people from the library "
    "— it does not contain any, and do not offer any.\n"
    f"LIBRARY (key — description):\n{json.dumps(LIBRARY, indent=1)}\n"
    "- SAFETY (hard rules of this server):\n"
    "  * Nothing sexual, ever — nudity, erotica, 'steamy', fetish, suggestive posing of any "
    "kind. Refuse regardless of phrasing or 'art' framing.\n"
    "  * Real named public figures MAY be satirised: an absurd fictional vignette is fair fun "
    "('the mayor loses a wrestling match to the server mascot'). But NEVER stage anything that "
    "could read as a true claim about them — confessing crimes, taking bribes, being arrested, "
    "degrading acts, mock 'documentary' footage presented as real, or harm coming to them. If "
    "the bit only works by implying it's real, refuse it; if it's obviously cartoon nonsense, "
    "let it through.\n"
    "  * No slurs, no cruelty aimed at private individuals, no content sexualising or abusing "
    "anyone depicted as a minor (instant, absolute).\n"
    "  * Language is PG-13: 'damn', 'hell', 'crap', 'ass', 'bullshit' are fine in dialogue; "
    "f-words, c-words and every slur are not. The farm does not make the mouth move, but the "
    "line is still heard — keep it clean.\n"
    "  * No blasphemy: never mock, demean, or 'roast' a religion, deity, prophet, scripture, "
    "or act of worship, and never stage a religious figure in a comedic or degrading bit. "
    "Religious settings, holidays, architecture, music and culture as neutral or warm "
    "backdrop are perfectly fine ('a neon-lit church marquee at Easter'). If the joke lands "
    "ON the faith itself, refuse it.\n"
    "  * Refusing is one calm line in reject_reason — friendly, no lecture, no moralising. "
    "Accept everything else with zero disclaimers.\n\n"
    "OUTPUT — strict JSON only, no markdown fences:\n"
    '{"reply": "<1-2 friendly sentences to the member>", "prompt": "<the complete scene>", '
    '"duration": 3, "aspect": "<auto if a picture is in play and no shape was named, else wide>", '
    '"quality": "standard", "ref_mode": "scene", '
    '"image": "<library key or null>", "reject_reason": null}'
)


def spoken_words(prompt):
    total = 0
    for ln in SPOKEN_RE.findall(prompt) + D_TAG_RE.findall(prompt):
        total += sum(1 for w in ln.split() if len(re.sub(r"[^\w']", "", w)) > 1)
    return total


SAFETY_OPEN = (
    "- SAFETY (private channel — most limits are off):\n"
    "  * Anything involving minors stays absolutely off: never write sexual content for or "
    "about anyone depicted as a minor; that request rejects with a hard reject_reason.\n"
    "  * Satire of real people is allowed, but never phrase it as a fact about them — no "
    "staged confessions, crimes, arrests, or mock-documentary presented as real. Obviously "
    "cartoonish = fine.\n"
    "  * Everything else is on the table: strong language, sexual and erotic themes, "
    "violence and gore, religious or blasphemous material, dark comedy, and any fictional "
    "character, franchise, or brand by its real name (Sonic, Mario, Godzilla, whoever). "
    "Write what is asked, with no disclaimers and no hedging.\n"
    "  * Refusing is one calm line in reject_reason — friendly, no lecture.\n\n"
)


def system_for(open_lane):
    """PG prompt by default; open-lane prompt swaps the SAFETY block wholesale."""
    if not open_lane:
        return SYSTEM
    a = SYSTEM.index("- SAFETY (hard rules of this server):")
    b = SYSTEM.index("OUTPUT — strict JSON only")
    return SYSTEM[:a] + SAFETY_OPEN + SYSTEM[b:]


def craft(ask, has_photo, has_voice=False, guild_id=None, user_id=None):
    """LLM turn: raw member ask -> validated plan dict (or {"error": ...})."""
    if not (CFG["llm_base"] and CFG["llm_key"]):
        return {"error": "the bot's LLM lane isn't configured"}
    user = ask[:1500]
    if has_photo:
        user += "\n[The member attached a photo — see ref_mode.]"
    if has_voice:
        user += ("\n[The member attached an audio clip — it is the character's VOICE "
                 "REFERENCE. Write one budget-friendly spoken line for the shot; the "
                 "rendered character speaks it with that voice.]")
    _, d = _req(CFG["llm_base"] + "/chat/completions",
                payload={"model": CFG["llm_model"], "temperature": 0.7, "max_tokens": 900,
                         "messages": [{"role": "system", "content": system_for(
                             not _nanny_on(guild_id) or _is_owner(user_id))},
                                      {"role": "user", "content": user}]},
                headers={"Authorization": f"Bearer {CFG['llm_key']}"}, timeout=120)
    content = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
    m = re.search(r"\{.*\}", content, re.S)
    plan = {}
    if m:
        for attempt in (m.group(0), re.sub(r",\s*([}\]])", r"\1", m.group(0))):
            try:
                plan = json.loads(attempt)
                break
            except json.JSONDecodeError:
                continue
    if not isinstance(plan, dict):
        plan = {}
    if plan.get("reject_reason"):
        return {"reject": str(plan["reject_reason"])[:300], "reply": str(plan.get("reply", ""))[:300]}
    prompt = str(plan.get("prompt") or "").strip()
    if not prompt:
        # formatter hiccup — fall back to the member's own words
        prompt = ask[:1500]
        plan.setdefault("reply", "I used your idea straight — here goes!")
    duration = plan.get("duration")
    duration = int(duration) if str(duration).isdigit() and int(duration) in DURATIONS else 3
    if spoken_words(prompt) > max(4, round(duration * 2.2)) and duration < 15:
        duration = 15 if duration > 8 else 10 if duration > 5 else 8  # make room for the line
    img_key = plan.get("image") if plan.get("image") in LIB_KEYS else None
    aspect = (plan.get("aspect") if plan.get("aspect") in ASPECTS or
              plan.get("aspect") == "auto" else DEFAULT_ASPECT)
    # deterministic: any picture in play + no shape named by the member = match the picture
    if (img_key or has_photo) and aspect != "auto" and not SHAPE_WORDS.search(ask):
        aspect = "auto"
    return {
        "reply": str(plan.get("reply") or "On it!")[:300],
        "prompt": prompt[:2000],
        "duration": duration,
        "aspect": aspect,
        "quality": plan.get("quality") if plan.get("quality") in QUALITIES else DEFAULT_QUALITY,
        "ref_mode": plan.get("ref_mode") if plan.get("ref_mode") in ("scene", "identity") else "scene",
        "image": img_key,
    }


# ── clip delivery ────────────────────────────────────────────────────────

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126 Safari/537.36")


def get_clip(jid):
    """Return a local path to the finished mp4 — sandbox first, R2 via worker as fallback."""
    path = os.path.join(CFG["vpipe_sandbox"], "sora", f"{jid}.mp4")
    if os.path.isfile(path):
        return path
    if not CFG["snora_token"]:
        return None
    tmp = os.path.join(CFG["vpipe_sandbox"], "sora", f"dc_{jid}.mp4")
    req = urllib.request.Request(f"{CFG['worker_base']}/api/sora/video/{jid}",
                                 headers={"X-Sora-Token": CFG["snora_token"], "User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        f.write(resp.read())
    return tmp if os.path.getsize(tmp) > 1000 else None


# ── watch store: survives bot restarts so in-flight jobs still get delivered ──

def _load_watch():
    try:
        return json.load(open(WATCH_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_watch(w):
    tmp = WATCH_FILE + ".tmp"
    json.dump(w, open(tmp, "w"), indent=1)
    os.replace(tmp, WATCH_FILE)


# ── the bot ──────────────────────────────────────────────────────────────

class SnoraClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True   # needs the Message Content intent in the dev portal
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
        self.active = {}     # user_id -> int (bot-side concurrency guard)
        self.last_ask = {}   # user_id -> ts (spam guard)

    # ---- helpers ----

    def allowed(self, ch):
        if isinstance(ch, discord.TextChannel):
            if CFG["allowed_guild_ids"] and int(ch.guild.id) not in [int(g) for g in CFG["allowed_guild_ids"]]:
                return False
            if CFG["allowed_channel_ids"] and int(ch.id) not in [int(c) for c in CFG["allowed_channel_ids"]]:
                return False
        return isinstance(ch, (discord.TextChannel, discord.Thread))

    @staticmethod
    def _queue_line(job, snapshot):
        """Human-readable queue state for a queued job, with a heads-up on
        what's ahead — one render at a time, ~5 min a clip is the honest guess."""
        queued = sorted((j for j in snapshot if j.get("status") == "queued"),
                        key=lambda j: j.get("created", 0))
        rendering = [j for j in snapshot if j.get("status") == "rendering"]
        pos = next((i + 1 for i, j in enumerate(queued) if j.get("id") == job["id"]), len(queued))
        ahead = pos - 1
        est = (ahead + len(rendering)) * 5
        if ahead == 0 and rendering:
            return (f"⏳ You're NEXT — {len(rendering)} clip" +
                    ("s" if len(rendering) != 1 else "") + f" still rendering (~{est} min).")
        line = f"⏳ Queued — position {pos} of {len(queued)}"
        if ahead:
            line += (f" — **{ahead} clip{'s' if ahead != 1 else ''} ahead of you**, "
                     f"est. ~{est} min before yours starts")
        else:
            line += " — renderer free, starting soon"
        if rendering:
            line += f" · {len(rendering)} rendering now"
        return line

    async def safe_edit(self, msg, text):
        try:
            if msg.content != text:
                await msg.edit(content=text[:1990])
        except discord.DiscordException:
            pass  # a lost edit must never kill the watch task

    # ---- the pipeline ----

    async def handle_ask(self, ch, user, text, ack, attachments=()):
        # nanny layer first — some asks should die before costing a token
        blocked = nanny_check(text, getattr(getattr(ch, "guild", None), "id", None),
                              getattr(user, "id", None))
        if blocked:
            reason, hard = blocked
            log.warning("NANNY blocked ask from %s in #%s: %s",
                        user, getattr(ch, "name", "?"), text[:300])
            try:
                with open(os.path.join(HERE, "discord_rejects.log"), "a") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M')} user={user} "
                            f"ch={getattr(ch, 'name', '?')} hard={hard} :: {text[:300]}\n")
            except OSError:
                pass
            return await ack.edit(
                content=f"🚫 {reason}." if hard else
                        f"🚫 {reason}. Give me a clean idea and I'll blow you away with it.")
        imgs = []
        skipped = []
        for a in attachments:
            ct, fn = a.content_type or "", (a.filename or "photo")
            if ct not in ("image/png", "image/jpeg", "image/webp", "image/heic") \
                    or a.size > 10_000_000:
                if ct.startswith("image/"):
                    skipped.append(fn)
                continue
            raw = await a.read()
            if ct == "image/heic":
                # iPhone photos — the farm only eats png/jpg/webp, convert here
                try:
                    import io

                    from PIL import Image
                    import pillow_heif
                    pillow_heif.register_heif_opener()
                    im = Image.open(io.BytesIO(raw)).convert("RGB")
                    im.thumbnail((1536, 1536))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=88)
                    raw = buf.getvalue()
                except Exception:
                    skipped.append(fn)
                    continue
                ext = "jpg"
            else:
                ext = ct.split("/")[-1].replace("jpeg", "jpg")
            imgs.append(f"data:image/{ext};base64," + base64.b64encode(raw).decode())
        # audio attachments: the character's voice reference (first one wins)
        voice = None
        for a in attachments:
            act = (a.content_type or "").split(";")[0]
            if not act.startswith("audio/") or a.size > 10_000_000:
                continue
            raw = await a.read()
            aext = act.split("/")[-1]
            if aext not in ("mpeg", "mp3", "wav", "m4a", "x-m4a", "mp4"):
                # Discord voice messages are ogg/opus — the farm wants mp3/wav/m4a
                try:
                    import subprocess, tempfile
                    with tempfile.NamedTemporaryFile(suffix=".in", delete=False) as tf:
                        tf.write(raw)
                        src = tf.name
                    dst = src + ".mp3"
                    rr = subprocess.run(
                        ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                         "-t", "15", "-b:a", "96k", dst],
                        capture_output=True, timeout=60)
                    if rr.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 1000:
                        raise RuntimeError("convert failed")
                    raw = open(dst, "rb").read()
                except Exception:
                    continue
                finally:
                    for p in (src, dst):
                        try:
                            os.unlink(p)
                        except OSError:
                            pass
                aext = "mpeg"
            voice = f"data:audio/{aext};base64," + base64.b64encode(raw).decode()
            break
        # 1. LLM formats the ask into a real scene prompt
        try:
            plan = await asyncio.to_thread(craft, text, bool(imgs), bool(voice),
                                            getattr(getattr(ch, "guild", None), "id", None),
                                            getattr(user, "id", None))
        except Exception as e:
            log.exception("llm failed")
            return await ack.edit(content=f"⚠️ Couldn't reach the scene writer ({e.__class__.__name__}). "
                                          "Try again in a sec.")
        if plan.get("reject"):
            return await ack.edit(content=f"🚫 {plan['reject']}")
        if voice and not imgs and not plan.get("image"):
            return await ack.edit(
                content="🎙️ A voice needs a face to ride on — send a photo with it, or name "
                        "a library reference by name so I know who's talking.")
        # 2. queue it on the farm
        payload = {k: plan[k] for k in ("prompt", "quality", "aspect", "duration", "ref_mode")}
        payload["await_approval"] = bool(CFG["await_approval"])
        payload["source"] = f"discord:{user.name}"[:60]
        if plan.get("image") and not imgs:
            payload["image_key"] = plan["image"]  # farm re-validates against the manifest
        if imgs:
            payload["images"] = imgs
        if voice:
            payload["voice"] = voice
        try:
            code, res = await asyncio.to_thread(farm_submit, payload, user.id)
        except Exception:
            return await ack.edit(content="🖥️ The render farm is unreachable right now — try again later.")
        if code != 201:
            return await ack.edit(content=f"🚫 {res.get('error', 'the farm said no')}")
        job = res
        self.active[user.id] = self.active.get(user.id, 0) + 1
        w = _load_watch()
        w[job["id"]] = {"channel": int(ch.id), "message": int(ack.id), "user": int(user.id),
                        "ask": text[:200], "started": time.time(), "done_notified": False}
        _save_watch(w)
        try:
            snapshot = (await asyncio.to_thread(farm_get, "/jobs")).get("jobs", [])
            qline = self._queue_line(job, snapshot)
        except Exception:
            qline = "⏳ Queued."
        note = ("\n⚠️ skipped unsupported picture(s): " + ", ".join(skipped[:3])) if skipped else ""
        await self.safe_edit(ack, f"🎬 **Your shot:** {plan['prompt'][:300]}\n{plan['reply']}\n{qline}{note}")
        # 3. watch it, then deliver
        asyncio.create_task(self.watch_job(job["id"], plan, ch, ack, user, w))

    @staticmethod
    def _render_line(job):
        """Queue message while rendering: live bar once the engine reports
        percentages (each pipeline stage counts its own 0-100)."""
        p = job.get("progress") or {}
        if not p:
            return (f"🎬 **Rendering now** — {job.get('duration')}s · {job.get('aspect')} · "
                    f"{job.get('quality')} — usually a few minutes.")
        pct = max(0, min(100, int(p.get("pct", 0))))
        bar = "▰" * (pct // 10) + "▱" * (10 - pct // 10)
        return (f"🎬 **Rendering** {bar} **{pct}%** — {str(p.get('stage', ''))[:40]}"
                f"\n_({job.get('duration')}s · {job.get('aspect')} · {job.get('quality')})_")

    async def watch_job(self, jid, plan, ch, ack, user, w):
        started = w[jid]["started"]
        try:
            while time.time() - started < CFG["max_wait_secs"]:
                await asyncio.sleep(CFG["poll_secs"])
                try:
                    snap = await asyncio.to_thread(farm_get, "/jobs")
                    job = next((j for j in snap.get("jobs", []) if j.get("id") == jid), None)
                except Exception:
                    continue  # farm briefly unreachable — keep waiting
                if job is None:
                    await self.safe_edit(ack, "🗑️ Your job was removed from the queue.")
                    break
                st = job.get("status")
                if st == "queued":
                    await self.safe_edit(ack, await asyncio.to_thread(
                        lambda s=snap: self._queue_line(job, s)))
                elif st == "rendering":
                    await self.safe_edit(ack, self._render_line(job))
                elif st == "done":
                    await self.deliver(jid, plan, ch, ack, user, started)
                    break
                elif st == "failed":
                    await self.safe_edit(ack, f"💥 The render failed: {str(job.get('error', 'unknown'))[:200]}")
                    break
            else:
                await self.safe_edit(ack, "⌛ This one's been parked longer than expected — "
                                          "ping the owner if it doesn't land.")
        finally:
            # record completion FIRST — a crash after delivery must never make
            # a restart redeliver the same clip (this actually happened)
            w = _load_watch()
            if jid in w:
                w[jid]["done_notified"] = True
                # keep completed entries a day for forensics, then drop them
                if time.time() - started > 86400:
                    del w[jid]
                _save_watch(w)
            if user is not None:
                self.active[user.id] = max(0, self.active.get(user.id, 1) - 1)

    async def deliver(self, jid, plan, ch, ack, user, started):
        mins = max(1, round((time.time() - started) / 60))
        try:
            path = await asyncio.to_thread(get_clip, jid)
        except Exception:
            path = None
        if not path:
            await self.safe_edit(ack, f"✅ Your clip rendered ({mins} min) but the bot couldn't "
                                      "fetch the file — check the sNora gallery.")
            return
        limit = getattr(getattr(ch, "guild", None), "filesize_limit", 10 * 1024 * 1024)
        if os.path.getsize(path) > limit - 50_000:
            await self.safe_edit(ack, f"✅ Your clip rendered ({mins} min) but is bigger than this "
                                      "server's upload cap — check the sNora gallery.")
            return
        await self.safe_edit(ack, f"✅ **Done in {mins} min** — {plan['duration']}s · "
                                  f"{plan['aspect']} · {plan['quality']}")
        who = f"<@{user.id}> your " if user else "Your "
        try:
            async with ch.typing():
                await ch.send(f"{who}sNora clip is ready 🐌🎬",
                              file=discord.File(path, filename=f"{jid}.mp4"))
        except discord.DiscordException:
            # channel upload may be blocked — DM-style fallback: edit is already done
            log.exception("deliver upload failed for %s", jid)

    # ---- entry points ----

    async def on_ready(self):
        log.info("sNora Discord bot as %s (id %s)", self.user, self.user.id)
        try:
            info = await self.application_info()
            app = {"id": str(info.id)}
            log.info("invite me with: https://discord.com/api/oauth2/authorize"
                     "?client_id=%s&permissions=274879306816&scope=bot%%20applications.commands",
                     app["id"])
        except discord.DiscordException:
            pass
        try:
            await self.tree.sync()
        except discord.DiscordException:
            log.warning("slash command sync failed (needs applications.commands scope)")
        # resume watching jobs that were in flight when we last exited
        for jid, e in list(_load_watch().items()):
            if e.get("done_notified"):
                continue
            try:
                ch = self.get_channel(e["channel"]) or await self.fetch_channel(e["channel"])
                ack = await ch.fetch_message(e["message"])
                user = self.get_user(e["user"])
            except Exception:
                continue
            plan = {"duration": 5, "aspect": DEFAULT_ASPECT, "quality": DEFAULT_QUALITY,
                    "prompt": e.get("ask", "")}
            log.info("resuming watch of %s", jid)
            asyncio.create_task(self.watch_job(jid, plan, ch, ack,
                                               user or ch.guild.get_member(e["user"]), _load_watch()))

    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        mentioned = self.user in message.mentions
        replying_to_bot = (message.reference is not None
                           and message.reference.cached_message is not None
                           and message.reference.cached_message.author.id == self.user.id)
        if not (mentioned or replying_to_bot):
            return
        ch = message.channel
        if not self.allowed(ch):
            return
        text = re.sub(r"<@!?\d+>", "", message.content).strip()
        imgs = tuple(a for a in message.attachments)
        if mentioned and not text and not imgs:
            await message.reply("Say what you'd like to see — e.g. "
                                "`@sNora a snail astronaut drifting past Saturn` — and I'll render it. "
                                "Defaults: 3s, 16:9, standard quality. Attach a photo to use it "
                                "as the shot's starting picture, or name one of the server's "
                                "standing library references you can borrow).")
            return
        if not text:
            # photo in, words none — animate the photo itself
            text = "bring this photo to life with natural, gentle cinematic motion"
        gap = time.time() - self.last_ask.get(message.author.id, 0)
        if _is_owner(message.author.id):
            gap = 9e9  # operator: no spam/concurrency guards, they triage the queue
        if gap < CFG["min_gap_secs"]:
            await message.reply(f"Easy there — one ask every {CFG['min_gap_secs']}s 🙂",
                                mention_author=False)
            return
        if self.active.get(message.author.id, 0) >= CFG["max_active_per_user"]:
            await message.reply("You already have a clip in the pipeline — I'll post it here when it's done.",
                                mention_author=False)
            return
        self.last_ask[message.author.id] = time.time()
        ack = await message.reply("🎬 On it — writing your shot…", allowed_mentions=discord.AllowedMentions.none())
        await self.handle_ask(ch, message.author, text, ack, imgs)

    # ---- slash command /video (same pipeline, no mention needed) ----

    def build_commands(self):
        @self.tree.command(name="video", description="Generate a video with the sNora render farm")
        @discord.app_commands.describe(idea="What should happen in the clip")
        async def _video(interaction: discord.Interaction, idea: str):
            if not self.allowed(interaction.channel):
                await interaction.response.send_message("I don't work in this channel.", ephemeral=True)
                return
            if (not _is_owner(interaction.user.id)
                    and self.active.get(interaction.user.id, 0) >= CFG["max_active_per_user"]):
                await interaction.response.send_message("You already have a clip in the pipeline.",
                                                        ephemeral=True)
                return
            await interaction.response.send_message("🎬 On it — writing your shot…")
            ack = await interaction.original_response()
            await self.handle_ask(interaction.channel, interaction.user, idea, ack)


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token_path = CFG["token_file"]
    try:
        token = open(token_path).read().strip()
    except FileNotFoundError:
        raise SystemExit(f"no Discord bot token at {token_path} — create the app at "
                         "discord.com/developers, enable Message Content intent, "
                         "put the bot token in that file, restart me.")
    client = SnoraClient()
    client.build_commands()
    await client.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
