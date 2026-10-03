#!/usr/bin/env python3
"""sNora Discord bot — @sNora <idea> in the Snail Discord → render farm → clip back in chat.

Runs on the MacBook under launchd next to the sNora render service
(sora_service.py on :7789, launched by com.snail3d.sora).

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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import discord

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
WATCH_FILE = os.path.join(HERE, "discord_watch.json")
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # h3_prompt.py sits beside this file
from h3_prompt import (FIRE_RULE, CONTRACT, wants_fire, fire_slip,              # noqa: E402
                       fire_slip_in, fire_corrective, negative_for, clean_body)

DEFAULTS = {
    "service_url": "http://127.0.0.1:7789",
    "token_file": os.path.join(HOME, ".sora_discord_token"),
    "allowed_guild_ids": [],      # empty = every server the bot is in
    "allowed_channel_ids": [],    # empty = every channel in an allowed server
    "max_active_per_user": 1,     # bot-side guard (farm has its own too)
    "min_gap_secs": 15,           # one ask per user per this window
    "busy_status": True,          # DND "rendering — busy" presence while any job is live
    "ignore_replies": True,       # a bare reply is not an ask — a typed @ping in a reply is
    "reply_owner_exempt": True,   # ...but the operator may still reply to trigger a run
    "ask_role_ids": [],           # role IDs whose ping also counts as an ask (the server sNora role)
    "max_active_total": 0,        # global in-flight cap (0 = unlimited); busy-reply at the cap
    "poll_secs": 20,              # queue poll cadence
    "max_wait_secs": 3 * 3600,    # give up watching after 3h
    "await_approval": False,      # True = hold Discord jobs at Eric's gate
    "nanny": True,                # hard PG gate on asks before anything runs
    "nanny_off_guilds": [],       # server IDs with the PG gate OFF (private adult channels)
    "nanny_extra_banned": [],     # extra hard-banned words/phrases (server-side, case-insensitive)
    # Qwen STILLS take a real negative prompt (CFG > 1); H3 video cannot, it is
    # CFG-distilled — see h3_prompt.py. Turn this on to spend a second DiT pass
    # per still keeping fire out when nobody asked for it.
    "fire_negative_stills": False,
    "fire_negative_prompt": "",
    "fire_negative_cfg": 3.0,
    "owner_ids": [],              # user IDs (the operator + helpers): no nanny, no spam guards
    "snora_token": "",            # R2 fallback fetch (X-Sora-Token)
    "studio_url": "http://100.120.41.45:7778",  # SnailStudio AI-engine host (Qwen stills) — now blacksnail3 (DGX Spark)
    "searx_url": "http://100.97.237.37:8889/search",  # fleet SearXNG (blacksnail1)
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

# ── still-image lane (Qwen-Image-2.1) ────────────────────────────────────
# The video farm is the default; a member asking for a picture / sticker / logo /
# poster / meme, or handing over a photo to edit, routes to SnailStudio instead.
IMAGE_INTENT_RE = re.compile(
    r"\b(image|images|picture|pictures|pic|pics|photo|photos|sticker|stickers|still|"
    r"illustration|drawing|artwork|logo|poster|meme|thumbnail|icon|avatar|wallpaper)\b", re.I)
IMAGE_VERBS_RE = re.compile(r"\b(make|create|generate|draw|paint|design|give me|show me|"
                            r"need|want|edit|change|turn|add|remove|photoshop)\b", re.I)
VIDEO_HINT_RE = re.compile(
    r"\b(video|videos|clip|clips|film|movie|animate|animated|animation|motion|move|moves|"
    r"moving|reel|sora|h3|voice|sound|song|sing|singing|speak|speaks|talking|frame|frames|"
    r"duration|loop)\b", re.I)
SECONDS_RE = re.compile(r"\b\d+\s*(s|sec|secs|second|seconds)\b", re.I)


def image_intent(text):
    """True ONLY when the member clearly wants a still picture, not a clip.
    Video words (or a duration) always win — the film lane is the default."""
    t = (text or "").strip()
    if not t or VIDEO_HINT_RE.search(t) or SECONDS_RE.search(t):
        return False
    if re.search(r"\b(sticker|stickers)\b", t, re.I):
        return True
    # an explicit drawing verb with no video cue is a still
    if re.search(r"\b(draw|paint|illustrate|sketch|depict|design)\b", t, re.I):
        return True
    if IMAGE_INTENT_RE.search(t) and IMAGE_VERBS_RE.search(t):
        return True
    return bool(re.match(r"^\s*(give me\s+|make\s+|create\s+|draw\s+)?(a|an|the)?\s*"
                         r"\b(picture|image|photo|still|logo|poster|illustration|drawing|"
                         r"artwork|icon|avatar|wallpaper|meme)\b", t, re.I))

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

# Blasphemy: expletive / mocking forms only — a reverent religious subject is fine,
# so bare 'jesus christ' or 'god' never triggers this on its own.
BLASPHEMY_RE = re.compile(
    r"\b(god ?damn\w*|goddamm\w*|jesus (?:fuck\w*|fucking)|for (?:god|christ)'?s sake|"
    r"christ'?s sake|blasphem\w*|sacrileg\w*|desecrat\w*|holy (?:shit|crap)|"
    r"(?:hail|worship|praise) (?:the )?(?:devil|satan)|taking the lord'?s name in vain)\b",
    re.I)

# A member DISCLAIMING ("not sexual", "no nudity", "nothing explicit") is not asking
# for it. Strip those phrases before the hard sexual gate reads the ask, so a clean
# idea that says "not sexual" in passing is not refused on its own disclaimer.
_SEX_DISCLAIMER_RE = re.compile(
    r"\b(?:not|no|never|nothing|none|non|without|isn'?t|aren'?t|ain'?t|zero|free of|absent)"
    r"\s*[- ]?\s*(?:really\s+|at all\s+|very\s+|too\s+)?"
    r"(?:sexual\w*|sexy|sex|nudity|nudes|nude|naked|porn\w*|erotic\w*|explicit|suggestive|"
    r"nsfw|smut\w*|fetish\w*)"
    r"(?:\s+(?:at all|whatsoever))?\b", re.I)


def _is_owner(user_id):
    """Operator allowlist: runs the open lane everywhere and skips bot-side guards."""
    return str(user_id) in [str(u) for u in CFG.get("owner_ids", [])]


def _nanny_on(guild_id):
    """PG lane applies unless globally off or this server is on the open list."""
    if not CFG.get("nanny", True):
        return False
    return str(guild_id) not in [str(g) for g in CFG.get("nanny_off_guilds", [])]


def nanny_check(text, guild_id=None, user_id=None):
    """Return (short refusal reason, hard) for clearly disallowed asks, else None.
    Porn and blasphemy are HARD rules: no lane, no 'art' framing, no override
    beyond the owner allowlist."""
    # hard floor under EVERY lane — illegal content has no override, no guild exemption
    if MINOR_HOT_RE.search(text):
        return ("that one is a hard no, full stop", True)
    if _is_owner(user_id) or not _nanny_on(guild_id):
        return None
    # configurable hard-ban list (add words in sora_config.json → nanny_extra_banned)
    for w in CFG.get("nanny_extra_banned", []):
        if w and re.search(r"\b" + re.escape(str(w)) + r"\b", text, re.I):
            return (f"‘{w}’ is banned here", True)
    if BLASPHEMY_RE.search(text):
        return ("not here — no blasphemy", True)
    if NANNY_RE.search(_SEX_DISCLAIMER_RE.sub(" ", text)):
        if MINOR_RE.search(text):
            return ("I don't mix kids and adult content", True)
        return ("no porn or sexual content here — hard rule", True)
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
    cfg["worker_base"] = sc.get("worker_base", "https://snail3d.com")
    cfg["vpipe_sandbox"] = sc.get("vpipe_sandbox", f"{HOME}/vpipe/sandbox")
    cfg["llm_base"] = sc.get("snail_api_base", "")
    cfg["llm_key"] = sc.get("snail_api_key", "")
    cfg["llm_model"] = sc.get("snail_model", "snail-top")
    cfg["studio_url"] = sc.get("studio_url", DEFAULTS["studio_url"])
    cfg["searx_url"] = sc.get("searx_url", DEFAULTS["searx_url"])
    cfg.update(sc.get("discord") or {})
    return cfg


CFG = load_cfg()


def _load_library():
    """The Discord-safe slice of Eric's reference manifest: only the snail
    characters and the bare sets. Eric himself (photos, poses, attire, likenesses)
    is deliberately invisible to Discord members."""
    keep = ["baby_snail_canonical", "baby_snail_sheet", "shack_wide",
            "shack_computer_corner", "prop_bambu_p2s", "ref_shack_day"]
    scrub = {
        "baby_snail_canonical": "BABY SNAIL hero shot: the server's tiny mascot — NOT an "
                                "actual snail. A six-inch Pixar-style toy figure of a bearded "
                                "man (NY cap, gray sherpa robe, slides), full body, hands on "
                                "hips, content expression",
        "baby_snail_sheet": "BABY SNAIL model sheet: six views of the same six-inch toy "
                            "figure (bearded man, NY cap) on a neutral warm-gray background",
    }
    try:
        with open(os.path.join(HERE, "shack_manifest.json")) as f:
            sh = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    by_key = {a["key"]: a for a in sh.get("assets", [])}
    out = []
    for k in keep:
        if k not in by_key:
            continue
        desc = scrub.get(k, re.sub(r"Eric'?s? ?", "the snail ", by_key[k].get("desc", "")))
        out.append({"key": k, "desc": desc})
    return out


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

# ── planner token budget (MEASURED 2026-10-02) ──────────────────────────
# snail-top's thinking lane bills its chain-of-thought to max_tokens. With the
# whole Director's Handbook in the system prompt it thinks ~1.5-9k tokens before
# emitting the JSON. At the old 900/1200 the reply came back finish_reason=length
# with an EMPTY content string; craft() found no JSON and silently shipped the
# member's raw words as the prompt — no plan, no chosen duration, so every clip
# landed on the 3s default. 2500 fits the CoT plus the plan; the retry covers an
# unusually long think.
PLAN_MAX_TOKENS = 2500
PLAN_TIMEOUT = 240


def planner_content(messages, temperature, max_tokens=PLAN_MAX_TOKENS):
    """One planner turn -> content. Retries ONCE at double the budget when the
    model burned the whole allowance reasoning and returned nothing — an empty
    plan must never degrade to the raw ask without a trace in the log."""
    for budget in (max_tokens, max_tokens * 2):
        _, d = _req(CFG["llm_base"] + "/chat/completions",
                    payload={"model": CFG["llm_model"], "temperature": temperature,
                             "max_tokens": budget, "messages": messages},
                    headers={"Authorization": f"Bearer {CFG['llm_key']}"},
                    timeout=PLAN_TIMEOUT)
        ch = (d.get("choices") or [{}])[0]
        content = (ch.get("message") or {}).get("content") or ""
        if content.strip():
            return content
        log.warning("planner returned empty content (finish=%s max_tokens=%d "
                    "completion_tokens=%s) — retrying bigger",
                    ch.get("finish_reason"), budget,
                    (d.get("usage") or {}).get("completion_tokens"))
    return ""


def explicit_duration(text):
    """The length the member actually named, or None.

    The fallback when the planner fails outright: a 3s clip for someone who
    typed 'at least 8 seconds' is a defect, not a default.
    """
    nums = [int(n) for n in re.findall(r"\b(\d{1,2})\s*(?:s\b|sec|secs|second|seconds)",
                                       text or "", re.I)]
    nums = [n for n in nums if n in DURATIONS]
    if nums:
        return max(nums) if re.search(r"\bat least|\bminimum|\bor more|\bup to|\bextended\b",
                                      text or "", re.I) else nums[0]
    m = re.search(r"\b(three|four|five|eight|ten|fifteen)\b", text or "", re.I)
    return ({"three": 3, "four": 4, "five": 5, "eight": 8, "ten": 10, "fifteen": 15}
            .get(m.group(1).lower()) if m else None)



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


async def collect_images(attachments):
    """Discord attachments -> (base64 image data URLs, skipped names).
    HEIC (iPhone) is converted here; the farm and the studio only eat png/jpg/webp."""
    imgs, skipped = [], []
    for a in attachments:
        ct, fn = a.content_type or "", (a.filename or "photo")
        if ct not in ("image/png", "image/jpeg", "image/webp", "image/heic") \
                or a.size > 10_000_000:
            if ct.startswith("image/"):
                skipped.append(fn)
            continue
        raw = await a.read()
        if ct == "image/heic":
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
    return imgs, skipped


# ── the chron lane (owner-only) ─────────────────────────────────────────
# Schedules are created and controlled from chat, but ONLY by the operator, and
# quiet hours are enforced by the farm — /chron never bypasses them.

def sched_get(path):
    return _req(CFG["service_url"] + path, headers=farm_headers(), timeout=25)[1]


def sched_post(path, payload=None):
    req = urllib.request.Request(
        CFG["service_url"] + path,
        data=json.dumps(payload).encode() if payload is not None else b"{}",
        method="POST",
        headers={"Content-Type": "application/json", **farm_headers()})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}


def _qwen_size_for(data_url, target=1536):
    """Canvas for a Qwen edit: follow the attached picture's own aspect at
    ~1536 on the long side, snapped to /32 (the engine's requirement)."""
    try:
        import io

        from PIL import Image
        raw = base64.b64decode(data_url.split("base64,", 1)[1])
        w, h = Image.open(io.BytesIO(raw)).size
        ar = w / max(1, h)
        if ar >= 1:
            tw, th = target, target / ar
        else:
            tw, th = target * ar, target
        snap = lambda v: max(512, int(round(v / 32)) * 32)
        return snap(tw), snap(th)
    except Exception:
        return 1024, 1024


# ── LLM prompt formatter (the app's own snail-api lane) ──────────────────

SYSTEM = (
    "You are sNora, the video-generation bot in this Discord community. A member gives you a "
    "video idea; you turn it into ONE complete scene prompt for Eric's MiniMax H3 render farm "
    "and a short friendly reply. You cannot render anything you haven't planned.\n\n"
    "THE HANDBOOK (condensed):\n"
    "- ONE shot per clip. Stage depth (foreground/midground/background) instead of cutting.\n"
    "- Write the prompt as a complete scene: subject + action + setting + lighting + exactly "
    "ONE camera move in prose ('the camera holds steady', 'slow push in', 'pans left with her "
    "gaze'). Never keyword lists; always say what the camera does or the model invents cuts.\n"
    "- DURATION — first, HONOUR A NAMED LENGTH: any number of seconds the member "
    "states ('15s video', 'at least 8 seconds', 'make it 10 seconds') IS the duration; "
    "snap it to the nearest allowed value and never overrule it down. Only when no "
    "length is named, pick by content — 3s is the DEFAULT: one punchy beat, quick "
    "reaction, pure atmosphere (≤7 spoken words). Go longer only when the content "
    "truly needs it: 5s one dialogue beat or a meaty action, 8s action+reaction or a "
    "two-beat gag, 10s two dialogue beats or a slow reveal, 15s a full mini-scene with "
    "a turn. Allowed: 3, 4, 5, 8, 10, 15.\n"
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
    "sets ONLY). When a member names or points at one of them ('baby snail', 'the shack', "
    "'the Bambu printer'...), set image to that library KEY and choose ref_mode "
    "(\"scene\" = the photo's place/time IS the scene; \"identity\" = the photo is WHO, the "
    "prompt is a new place). Character/model-sheet keys (HERO SHOT, MODEL SHEET, IDENTITY in "
    "their description) are WHO-references — use \"identity\" with them unless the member "
    "literally wants that photo's background as the scene. A library call must ride on the "
    "actual reference — never re-describe it in text. CHARACTER FIRST: if a character (baby "
    "snail) is IN the scene, the image is the CHARACTER key — set keys (shack, printer props) "
    "only carry establishing shots with no character. BABY SNAIL IS A TINY MAN, NOT A SNAIL: "
    "in the prompt never call him a snail, shell, creature, or antennae-covered anything — "
    "write him as 'Baby Snail, the six-inch Pixar-style toy figure of a bearded man in a navy "
    "NY cap and gray sherpa robe' and keep the Pixar collectible-figure look; the renderer "
    "sees the word 'snail' and grows antennae. In this server 'snail' on its own ALSO "
    "means Baby Snail — the operator's real photos are not in the library and must never "
    "be invented. No library subject fits: image null "
    "(pure text-to-video). Members may NOT request likenesses of real people from the library "
    "— it does not contain any, and do not offer any.\n"
    f"LIBRARY (key — description):\n{json.dumps(LIBRARY, indent=1)}\n"
    "- SAFETY (hard rules of this server):\n"
    "  * Nothing sexual, ever — nudity, erotica, 'steamy', fetish, suggestive posing of any "
    "kind. Refuse regardless of phrasing or 'art' framing.\n"
    "  * Real named public figures MAY be satirised: an absurd fictional vignette is fair fun "
    "('the president loses a wrestling match to Baby Snail'). But NEVER stage anything that "
    "could read as a true claim about them — confessing crimes, taking bribes, being arrested, "
    "degrading acts, mock 'documentary' footage presented as real, or harm coming to them. If "
    "the bit only works by implying it's real, refuse it; if it's obviously cartoon nonsense, "
    "let it through.\n"
    "  * No slurs, no cruelty aimed at private individuals, no content sexualising or abusing "
    "anyone depicted as a minor (instant, absolute).\n"
    "  * Language is PG-13: 'damn', 'hell', 'crap', 'ass', 'bullshit' are fine in dialogue; "
    "f-words, c-words and every slur are not. The farm does not make the mouth move, but the "
    "line is still heard — keep it clean.\n"
    "  * Toilet humour is fine — farts, burps, hiccups, a whoopee cushion, a pratfall. A "
    "bodily-function gag is cartoon slapstick, not a degrading act: never refuse it, "
    "including when a real named performer or public figure is the one doing it. Keep it "
    "non-sexual, and let the rude sound live in the soundscape, not in dialogue.\n"
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
    '"soundscape": "<1-4 sentences of ambient + physical sound for the whole clip: wind, rain, '
    'footsteps, cloth, machines. Never repeat the dialogue>", '
    '"music": "<1-3 sentences of score — instrumentation, tempo, rhythm, dynamics — or N/A if '
    'there is no score>", '
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
    "  * Crude bodily-function comedy — farts, burps, pratfalls — is obviously cartoonish, "
    "never a claim staged to look true: write it, including about a real named performer.\n"
    "  * Everything else is on the table: strong language, sexual and erotic themes, "
    "violence and gore, religious or blasphemous material, dark comedy, and any fictional "
    "character, franchise, or brand by its real name (Sonic, Mario, Godzilla, whoever). "
    "Write what is asked, with no disclaimers and no hedging.\n"
    "  * reject_reason MUST stay null. Copyright, trademark, franchise, brand, and "
    "'licensing' worries are NOT grounds to refuse — describing a famous character for a "
    "private render is not piracy, and declining on those grounds is a malfunction of this "
    "job, not caution.\n"
    "  * The only valid reject_reason is minors, or a claim staged to look true about a "
    "real living person. Anything else: write the prompt.\n\n"
)


def system_for(open_lane):
    """PG prompt by default; open-lane prompt swaps the SAFETY block wholesale.
    FIRE_RULE + CONTRACT ride on top: fire is opt-in, and the second half is the
    official MiniMax H3 writing contract this model was trained on."""
    if not open_lane:
        return SYSTEM + FIRE_RULE + CONTRACT
    a = SYSTEM.index("- SAFETY (hard rules of this server):")
    b = SYSTEM.index("OUTPUT — strict JSON only")
    return SYSTEM[:a] + SAFETY_OPEN + SYSTEM[b:] + FIRE_RULE + CONTRACT


# ── still-image formatter (Qwen-Image-2.1 prompting contract) ────────────

STILL_SYSTEM = (
    "You are sNora's image-maker in this Discord community. A member gives you an idea for "
    "a still picture; you write ONE faithful Qwen-Image-2.1 prompt and a short friendly reply. "
    "You cannot render anything you haven't described.\n\n"
    "WRITE A DESCRIPTION OF THE FINISHED FRAME — not a list of commands:\n"
    "- Present tense, third-person observation. Never 'create', 'generate', 'make sure', "
    "'you should'. No quality boosters ('masterpiece', '8K', 'highly detailed', 'award-winning').\n"
    "- Open with the medium and subject: 'A square illustration of…', 'A vertical photograph of…'.\n"
    "- PLACE EVERYTHING: 8–14 positional phrases that reach the corners, edges and centre "
    "('in the upper-left corner', 'across the lower third'). About a third of the sentences "
    "should open on a position.\n"
    "- Walk the frame in order: background first, then the top band, left→centre→right, then "
    "the bottom band; or subject-out for a single subject.\n"
    "- Give the lighting its own sentence: source, direction, quality, and the shadows it casts.\n"
    "- Name colour with a modifier (deep navy, muted olive, warm terracotta) and give the "
    "material (brushed metal, coarse linen) — never the bare noun.\n"
    "- Enumerate; never summarise. 'Several items' is a defect — write small counts as words.\n"
    "- Hedge what you can't know ('appears to be'), and be flatly definite about what the "
    "member fixed.\n"
    "- Close on the whole frame in one sentence: 'The overall composition…'.\n"
    "- Target ~15–20 sentences.\n\n"
    "IF THE MEMBER ATTACHED A PICTURE you are EDITING it (mode 'edit'):\n"
    "- Lead with the operation ('Change the background to…', 'Add a…', 'Remove the…').\n"
    "- Name ONLY what changes and push it strong; everything unnamed stays put.\n"
    "- Then ONE blanket preservation clause naming what stays by type, position and role: "
    "'keep the person, the objects, the background, the text and the composition exactly as "
    "they are.' Do NOT describe a kept thing in concrete detail — that reads as a generation "
    "instruction and the thing drifts.\n"
    "- Identity is the hardest invariant: point at the reference picture, never describe "
    "features in words.\n"
    "- Text is literal: commit to the exact characters in double quotes.\n"
    "- Write from the point of view of someone holding only the input picture.\n\n"
    "STICKERS — the member asks for a sticker / transparent / cut-out / logo on nothing:\n"
    "- Set transparent true. The prompt MUST read: 'This is an RGBA image with transparency. "
    "<description>. The image has alpha channel and the background is transparent.'\n"
    "- Name the cut-out subject, its outline/border, and any text.\n\n"
    "SHAPE: 'square' (1:1), 'portrait' (9:16 phone/reels), 'wide' (16:9 thumbnail/desktop). "
    "If a picture is attached and the member did NOT name a shape, set 'auto' so the canvas "
    "follows that picture's own ratio. NEVER put a ratio or pixel count in the prompt — it is "
    "set through the fields.\n"
    "QUALITY: default 'standard'; 'fast' or 'best' only when the member clearly asks.\n"
    "- SAFETY (hard rules of this server):\n"
    "  * Nothing sexual, ever — no nudity, erotica, fetish or suggestive posing. Refuse "
    "regardless of 'art' framing.\n"
    "  * Real named public figures may appear in absurd, obviously-fictional scenes, but never "
    "in anything that could read as a true claim about them (crimes, arrests, degrading acts, "
    "mock documentary).\n"
    "  * No slurs; no cruelty aimed at private individuals; nothing sexualising anyone depicted "
    "as a minor (instant, absolute).\n"
    "  * Toilet humour is fine — farts, burps, a whoopee cushion, a pratfall. A bodily-"
    "function gag is cartoon slapstick, not a degrading act: never refuse it, including "
    "when a real named performer or public figure is the one doing it. Keep it non-sexual.\n"
    "  * PG-13 language; no blasphemy. Refusing is one calm line in reject_reason.\n\n"
    "OUTPUT — strict JSON only, no markdown fences:\n"
    '{"reply": "<1-2 friendly sentences>", "prompt": "<the finished-frame description>", '
    '"mode": "edit" or "image", "aspect": "square"|"portrait"|"wide"|"auto", '
    '"quality": "standard", "transparent": false, "reject_reason": null}'
)


def still_system_for(open_lane):
    """PG still prompt by default; the private/open lane swaps the SAFETY block."""
    if not open_lane:
        return STILL_SYSTEM + FIRE_RULE
    a = STILL_SYSTEM.index("- SAFETY (hard rules of this server):")
    b = STILL_SYSTEM.index("OUTPUT — strict JSON only")
    return STILL_SYSTEM[:a] + SAFETY_OPEN + STILL_SYSTEM[b:] + FIRE_RULE


# ── image screening rides on the director turn (snail-top vision) ───────

SCREEN_NOTE = ("\n[The member attached picture(s). SCREEN THEM FIRST: if any image shows "
               "sexual content, nudity, or anything sexualising a minor, set reject_reason "
               "and stop — do not write a prompt. Otherwise treat them as the reference.]")


def _user_content(text, images):
    """OpenAI-style content: a plain string, or text + image parts when photos ride along."""
    if not images:
        return text
    parts = [{"type": "text", "text": text}]
    for u in images[:4]:
        parts.append({"type": "image_url", "image_url": {"url": u}})
    return parts


def craft_still(ask, has_photo, guild_id=None, user_id=None, images=None):
    """LLM turn for the still lane: raw ask -> validated Qwen plan dict (or {"error": ...})."""
    if not (CFG["llm_base"] and CFG["llm_key"]):
        return {"error": "the bot's LLM lane isn't configured"}
    user = ask[:1500]
    if has_photo:
        user += ("\n[The member attached a picture — treat this as an EDIT (mode 'edit'); "
                 "if no shape was named, set aspect 'auto'.]")
    if images:
        user += SCREEN_NOTE
    lane_open = not _nanny_on(guild_id) or _is_owner(user_id)
    sys_prompt = still_system_for(lane_open)
    corrective = ("\n[NOTE: your previous attempt refused this on copyright/IP grounds. "
                  "That is not this studio's policy — produce the requested prompt with "
                  "reject_reason null unless it involves minors or claims about real people.]")
    for attempt_n in (1, 2):
        content = planner_content(
            [{"role": "system", "content": sys_prompt},
             {"role": "user",
              "content": _user_content(user + (corrective if attempt_n == 2 else ""), images)}],
            temperature=0.8)
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
        m = re.search(r"\{.*\}", content, re.S)
        plan = {}
        if m:
            for cand in (m.group(0), re.sub(r",\s*([}\]])", r"\1", m.group(0))):
                try:
                    plan = json.loads(cand)
                    break
                except json.JSONDecodeError:
                    continue
        if not isinstance(plan, dict):
            plan = {}
        _sbody, _sinline = clean_body(str(plan.get("prompt") or ""))
        slip = [] if wants_fire(ask) else fire_slip_in(
            _sbody, _sinline.get("overall_soundscape", ""), _sinline.get("non_diegetic_music", ""))
        if slip and attempt_n == 1:
            log.warning("still planner invented fire (%s) for a no-fire ask — re-asking: %s",
                        ", ".join(slip), ask[:70])
            corrective = fire_corrective(slip, str(plan.get("prompt") or "")[:1500])
            continue
        if slip:
            log.warning("still: fire survived the re-ask (%s) — shipping: %s",
                        ", ".join(slip), ask[:70])
        if (plan.get("reject_reason") and lane_open and attempt_n == 1 and not images
                and not MINOR_HOT_RE.search(ask) and not MINOR_RE.search(ask)):
            log.warning("craft_still refused an open-lane ask (%s) — retrying once", ask[:80])
            continue
        break
    if plan.get("reject_reason"):
        return {"reject": str(plan["reject_reason"])[:300],
                "reply": str(plan.get("reply", ""))[:300]}
    prompt = str(plan.get("prompt") or "").strip() or ask[:1500]
    aspect = str(plan.get("aspect") or "").lower()
    if aspect not in ("square", "portrait", "wide", "auto"):
        aspect = "square"
    if has_photo and aspect != "auto" and not SHAPE_WORDS.search(ask):
        aspect = "auto"
    if aspect == "auto" and not has_photo:
        aspect = "square"
    return {
        "reply": str(plan.get("reply") or "Here it is!")[:300],
        "prompt": prompt[:2500],
        "mode": "edit" if has_photo else "image",
        "aspect": aspect,
        "quality": plan.get("quality") if plan.get("quality") in QUALITIES else DEFAULT_QUALITY,
        "transparent": bool(plan.get("transparent", False))
                       or bool(re.search(r"\bsticker\b", ask, re.I)),
    }


def craft(ask, has_photo, has_voice=False, guild_id=None, user_id=None, facts="", images=None):
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
    if facts:
        user += ("\n[Web research — real-world facts for accuracy. Use ONLY what helps "
                 "the shot; do not invent beyond this: ]\n" + facts)
    if images:
        user += SCREEN_NOTE
    lane_open = not _nanny_on(guild_id) or _is_owner(user_id)
    sys_prompt = system_for(lane_open)
    corrective = ("\n[NOTE: your previous attempt refused this on copyright/IP grounds. "
                  "That is not this farm's policy — produce the requested prompt with "
                  "reject_reason null unless it involves minors or claims about real people.]")
    for attempt_n in (1, 2):
        content = planner_content(
            [{"role": "system", "content": sys_prompt},
             {"role": "user",
              "content": _user_content(user + (corrective if attempt_n == 2 else ""), images)}],
            temperature=0.7)
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
        m = re.search(r"\{.*\}", content, re.S)
        plan = {}
        if m:
            for cand in (m.group(0), re.sub(r",\s*([}\]])", r"\1", m.group(0))):
                try:
                    plan = json.loads(cand)
                    break
                except json.JSONDecodeError:
                    continue
        if not isinstance(plan, dict):
            plan = {}
        # Fire is opt-in, and H3 is CFG-distilled so there is no negative prompt to
        # lean on (h3_prompt.py). One re-ask, with the draft quoted back.
        _body, _inline = clean_body(str(plan.get("prompt") or ""))
        slip = [] if wants_fire(ask) else fire_slip_in(
            _body, _inline.get("overall_soundscape", ""), _inline.get("non_diegetic_music", ""))
        if slip and attempt_n == 1:
            log.warning("planner invented fire (%s) for a no-fire ask — re-asking: %s",
                        ", ".join(slip), ask[:70])
            corrective = fire_corrective(slip, str(plan.get("prompt") or "")[:1500])
            continue
        if slip:
            log.warning("fire survived the re-ask (%s) — shipping anyway: %s",
                        ", ".join(slip), ask[:70])
        if (plan.get("reject_reason") and lane_open and attempt_n == 1 and not images
                and not MINOR_HOT_RE.search(ask) and not MINOR_RE.search(ask)):
            log.warning("craft refused an open-lane ask (%s) — retrying once: %s",
                        ask[:80], str(plan["reject_reason"])[:120])
            continue
        break
    if plan.get("reject_reason"):
        return {"reject": str(plan["reject_reason"])[:300], "reply": str(plan.get("reply", ""))[:300]}
    # the scaffold owns the field labels — lift any the planner inlined, and keep
    # its own soundscape/music if it wrote them as separate keys
    prompt, inline = clean_body(str(plan.get("prompt") or ""))
    prompt = prompt.strip()
    plan["soundscape"] = plan.get("soundscape") or inline.get("overall_soundscape") or ""
    plan["music"] = plan.get("music") or inline.get("non_diegetic_music") or ""
    if not prompt:
        # The planner's JSON never landed (empty content, or content the parser
        # could not unwrap). Ship the member's own words — but NEVER silently:
        # this used to be the reason every clip came out 3s and off-template.
        prompt = ask[:1500]
        plan.setdefault("reply", "I used your idea straight — here goes!")
        log.warning("planner produced no usable plan for %r — falling back to the "
                    "raw ask, duration=%s", ask[:80], explicit_duration(ask))
    duration = plan.get("duration")
    if str(duration).isdigit() and int(duration) in DURATIONS:
        duration = int(duration)
    else:
        # no plan (or an off-grid one) — honour a length the member named rather
        # than defaulting to 3s
        duration = explicit_duration(ask) or 3
    # A line the clip cannot speak is a HARD 400 at the farm — the member sees an
    # error instead of a video. Get under the budget here, and never ship a plan
    # the farm is about to reject: honour a NAMED length first, then grow the clip
    # step by step until the dialogue fits. 15s is the ceiling.
    want = explicit_duration(ask)
    grid = sorted(DURATIONS)
    for _ in range(len(grid)):
        if duration >= 15 or spoken_words(prompt) <= max(4, round(duration * 2.2)):
            break
        # stay ON the allowed grid: a 6s clip is not renderable, so an off-grid
        # duration would be coerced back to 5s at the farm and rejected again
        nxt = max([d for d in grid if d > duration], default=15)
        duration = max(want, nxt) if want and want > duration else nxt
    img_key = plan.get("image") if plan.get("image") in LIB_KEYS else None
    aspect = (plan.get("aspect") if plan.get("aspect") in ASPECTS or
              plan.get("aspect") == "auto" else DEFAULT_ASPECT)
    # deterministic: any picture in play + no shape named by the member = match the picture
    if (img_key or has_photo) and aspect != "auto" and not SHAPE_WORDS.search(ask):
        aspect = "auto"
    # Last gate: the farm 400s an over-budget line at the door, which reads to the
    # member as "no video". If even 15s cannot hold it, say so in plain words
    # instead of queueing a job that is guaranteed to bounce.
    if spoken_words(prompt) > max(4, round(duration * 2.2)):
        return {"error": f"That line is {spoken_words(prompt)} words — 15s (the longest "
                         "clip) fits about 33 spoken words. Shorten the line and send it again."}
    return {
        "reply": str(plan.get("reply") or "On it!")[:300],
        "prompt": prompt[:2000],
        "soundscape": str(plan.get("soundscape") or "")[:600],
        "music": str(plan.get("music") or "")[:300],
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


def share_link(jid, kind="video"):
    """Create an UNGATED public watch link for a finished clip (worker KV share).
    Delivery fallback: keeps a render reachable when the channel refuses
    attachments or the file beats the server's upload cap."""
    if not CFG["snora_token"]:
        return None
    try:
        req = urllib.request.Request(
            f"{CFG['worker_base']}/api/sora/share",
            data=json.dumps({"id": jid, "kind": kind}).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Sora-Token": CFG["snora_token"],
                     "User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()).get("url")
    except Exception:
        log.exception("share link failed for %s", jid)
        return None


# ── web research (SearXNG on the fleet: blacksnail1 :8889) ──────────────

LOOKUP_RE = re.compile(
    r"\b(look ?up|search(?: the web| the internet| for)?|google|research|"
    r"find out|tell me about|what(?:'s| is| are)|who(?:'s| is| are)|"
    r"where(?:'s| is| are)|when (?:is|was|did))\b", re.I)
CUE_RE = re.compile(
    r"\b(real|actual|historically|historic|documentary|based on|true story|"
    r"in the style of|looks? like the real|footage of|accurate|factual|wiki|"
    r"look ?up|search|research|google)\b", re.I)
RENDER_RE = re.compile(
    r"\b(render|generate|animate|video|clip|picture|image|photo|sticker|edit|"
    r"make (?:a |an )?(?:video|clip|picture|image|photo|sticker))\b", re.I)


def searx_search(query, limit=6):
    """SearXNG (fleet: blacksnail1:8889) -> [{'title','url','snippet'}, ...].
    Blocking — always call via asyncio.to_thread."""
    url = CFG["searx_url"] + "?" + urllib.parse.urlencode({"q": query[:400], "format": "json"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        log.exception("searx search failed for %r", query[:80])
        return []
    out = []
    for r in (data.get("results") or [])[:limit]:
        out.append({"title": (r.get("title") or "").strip()[:160],
                    "url": (r.get("url") or "").strip(),
                    "snippet": (r.get("content") or "").strip()[:400]})
    return out


def format_facts(results):
    """Compact fact sheet handed to the scene writer (keeps the prompt honest)."""
    if not results:
        return ""
    lines = []
    for r in results[:5]:
        bit = f"- {r['title']}"
        if r["snippet"]:
            bit += f": {r['snippet']}"
        lines.append(bit)
    return "\n".join(lines)[:1600]


def research_answer(query, results):
    """LLM summary of the search results — plain prose, no render."""
    if not (CFG["llm_base"] and CFG["llm_key"]):
        return ""
    sheet = format_facts(results)
    sys_prompt = ("You are sNora, a sharp research assistant. Answer the user's question "
                  "in 2-4 tight sentences using ONLY the web results provided. If they "
                  "don't cover it, say so plainly. No preamble, no bullet points.")
    # same thinking-lane trap as the planners: 400 tokens is all chain-of-thought,
    # the answer comes back empty. planner_content retries bigger.
    return planner_content(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": f"Question: {query}\n\nWeb results:\n{sheet}"}],
        temperature=0.3, max_tokens=900).strip()


def lookup_intent(text):
    return bool(LOOKUP_RE.search(text))


def render_intent(text):
    return bool(RENDER_RE.search(text) or VIDEO_HINT_RE.search(text)
                or IMAGE_INTENT_RE.search(text))


# ── still-image delivery (SnailStudio Qwen-Image-2.1) ────────────────────

def studio_generate(payload):
    """POST the Qwen still job to SnailStudio. Blocking — the worker holds the
    call for the whole render — so always called via asyncio.to_thread."""
    req = urllib.request.Request(
        CFG["studio_url"].rstrip("/") + "/api/editor/ai/generate-qwen",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


def get_still(ref, jid):
    """Download a /generated/... PNG from SnailStudio to a local temp file."""
    tmp = os.path.join("/tmp", f"snora_still_{jid}.png")
    req = urllib.request.Request(CFG["studio_url"].rstrip("/") + ref,
                                 headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as f:
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
        # busy presence (see _apply_presence): DND while anything is live
        self._presence_gen = 0
        self._presence_worker = None
        self._last_presence_at = 0.0

    # ---- helpers ----

    @property
    def inflight(self):
        """Clips/stills this bot currently has queued or rendering."""
        return sum(self.active.values())

    def _job_start(self, user_id):
        self.active[user_id] = self.active.get(user_id, 0) + 1
        self._schedule_presence()

    def _job_end(self, user_id):
        if user_id is None:
            return
        left = self.active.get(user_id, 1) - 1
        if left > 0:
            self.active[user_id] = left
        else:
            self.active.pop(user_id, None)
        self._schedule_presence()

    def _schedule_presence(self):
        """Debounced presence refresh — coalesces the +1/-1 bursts and stays
        well under Discord's 5-updates-per-minute gateway limit."""
        self._presence_gen += 1
        if not CFG.get("busy_status", True):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._presence_worker is None or self._presence_worker.done():
            self._presence_worker = loop.create_task(self._presence_loop())

    async def _presence_loop(self):
        while True:
            gen = self._presence_gen
            await asyncio.sleep(1.5)
            if gen != self._presence_gen:
                continue                     # state moved — re-read and re-apply
            wait = 5 - (time.time() - self._last_presence_at)
            if wait > 0:
                await asyncio.sleep(wait)
                if gen != self._presence_gen:
                    continue
            await self._apply_presence()
            self._last_presence_at = time.time()
            if gen == self._presence_gen:
                return

    async def _apply_presence(self):
        """Idle: online, watching the queue. Busy: DND, 'rendering — busy'."""
        try:
            n = self.inflight
            if n:
                act = discord.Activity(
                    type=discord.ActivityType.playing,
                    name=f"rendering — busy ({n} in the pipeline)")
                await self.change_presence(status=discord.Status.dnd, activity=act)
            else:
                act = discord.Activity(
                    type=discord.ActivityType.watching, name="the render queue")
                await self.change_presence(status=discord.Status.online, activity=act)
        except Exception:
            # presence is cosmetic — a gateway hiccup must never kill the bot
            log.debug("presence update skipped", exc_info=True)

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
        if isinstance(snapshot, dict):      # farm_get hands back {"jobs": [...]}
            snapshot = snapshot.get("jobs", [])
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

    async def handle_ask(self, ch, user, text, ack, attachments=(), end_image=None):
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
        imgs, skipped = await collect_images(attachments)
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
        # 0b. web research: only when the ask reaches for real-world facts
        facts = ""
        if lookup_intent(text) or CUE_RE.search(text):
            try:
                results = await asyncio.to_thread(searx_search, text, 5)
                facts = format_facts(results)
                if facts:
                    log.info("researched %d web result(s) for: %s", len(results), text[:80])
            except Exception:
                log.exception("research step failed")
        # 1. LLM formats the ask into a real scene prompt
        try:
            plan = await asyncio.to_thread(craft, text, bool(imgs), bool(voice),
                                            getattr(getattr(ch, "guild", None), "id", None),
                                            getattr(user, "id", None), facts, list(imgs))
        except Exception as e:
            log.exception("llm failed")
            return await ack.edit(content=f"⚠️ Couldn't reach the scene writer ({e.__class__.__name__}). "
                                          "Try again in a sec.")
        if plan.get("reject"):
            return await ack.edit(content=f"🚫 {plan['reject']}")
        if plan.get("error"):
            return await ack.edit(content=f"⚠️ {plan['error']}")
        if voice and not imgs and not plan.get("image"):
            return await ack.edit(
                content="🎙️ A voice needs a face to ride on — send a photo with it, or name "
                        "a reference (\"baby snail\", \"the shack\") so I know who's talking.")
        # begin→end: an explicit ending picture (slash /video ending=), or the
        # mention-flow keyword rule: attach 2+ photos and say "ending frame"
        # (or "last photo is the ending" / "ends with the last photo") and the
        # LAST photo becomes the final frame of the clip.
        if end_image is None and imgs:
            if re.search(r"\b(end(ing)? frame|last (photo|picture|image) (is|as|to be) "
                         r"(the )?end(ing)?|ends? with (the )?last (photo|picture|image))\b",
                         text, re.I) and len(imgs) >= 2:
                end_image = imgs.pop()
        # 2. queue it on the farm
        payload = {k: plan[k] for k in ("prompt", "quality", "aspect", "duration", "ref_mode",
                                        "soundscape", "music")}
        payload["await_approval"] = bool(CFG["await_approval"])
        payload["source"] = f"discord:{user.name}"[:60]
        if plan.get("image") and not imgs:
            payload["image_key"] = plan["image"]  # farm re-validates against the manifest
        if imgs:
            payload["images"] = imgs
        if end_image:
            if not imgs:
                return await ack.edit(content="🎬 An ending frame needs a beginning frame "
                                              "too — attach the starting photo as well.")
            payload["end_image"] = end_image
            payload["aspect"] = "auto"
        if voice:
            payload["voice"] = voice
        try:
            code, res = await asyncio.to_thread(farm_submit, payload, user.id)
        except Exception:
            return await ack.edit(content="🖥️ The render farm is unreachable right now — try again later.")
        if code != 201:
            return await ack.edit(content=f"🚫 {res.get('error', 'the farm said no')}")
        job = res
        self._job_start(user.id)
        w = _load_watch()
        ask_mid = getattr(getattr(ack, "reference", None), "message_id", None)
        w[job["id"]] = {"channel": int(ch.id), "message": int(ack.id), "user": int(user.id),
                        "ask": text[:200], "started": time.time(), "done_notified": False,
                        "ask_message": int(ask_mid) if ask_mid else 0}
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

    # ---- the still lane (Qwen-Image-2.1) ----

    async def handle_still(self, ch, user, text, ack, attachments=()):
        blocked = nanny_check(text, getattr(getattr(ch, "guild", None), "id", None),
                              getattr(user, "id", None))
        if blocked:
            reason, hard = blocked
            log.warning("NANNY blocked still ask from %s in #%s: %s",
                        user, getattr(ch, "name", "?"), text[:300])
            return await ack.edit(
                content=f"🚫 {reason}." if hard else
                        f"🚫 {reason}. Give me a clean idea and I'll make you something great.")
        imgs, skipped = await collect_images(attachments)
        try:
            plan = await asyncio.to_thread(craft_still, text, bool(imgs),
                                            getattr(getattr(ch, "guild", None), "id", None),
                                            getattr(user, "id", None), list(imgs))
        except Exception as e:
            log.exception("craft_still failed")
            return await ack.edit(content=f"⚠️ Couldn't reach the picture writer "
                                          f"({e.__class__.__name__}). Try again in a sec.")
        if plan.get("reject"):
            return await ack.edit(content=f"🚫 {plan['reject']}")

        payload = {
            "prompt": plan["prompt"],
            "quality": plan["quality"],
            "transparent": plan["transparent"],
            "steps": 40,
            "seed": -1,
            "use_kv_cache": True,
        }
        # Qwen has real CFG, so a negative prompt actually bites — but only with
        # true_cfg_scale > 1 (the route's default 1.0 disables CFG entirely).
        neg, neg_cfg = negative_for(text,
                                    enabled=bool(CFG.get("fire_negative_stills")),
                                    negative=CFG.get("fire_negative_prompt"),
                                    cfg=CFG.get("fire_negative_cfg") or 3.0)
        if neg:
            payload["negative_prompt"] = neg
            payload["true_cfg_scale"] = neg_cfg
        if imgs:
            payload["images"] = imgs[:10]
            w, h = _qwen_size_for(imgs[0])
            payload["width"], payload["height"] = w, h
            size_line = f"{w}×{h}"
        else:
            payload["aspect"] = plan["aspect"]
            size_line = plan["aspect"]

        self._job_start(user.id)
        note = ("\n⚠️ skipped unsupported picture(s): " + ", ".join(skipped[:3])) if skipped else ""
        kind = "🎨 **Editing your picture**" if imgs else "🎨 **Painting**"
        await self.safe_edit(
            ack,
            f"{kind} — {plan['prompt'][:280]}\n{plan['reply']}\n"
            f"_({size_line} · {plan['quality']} · {'RGBA' if plan['transparent'] else 'RGB'})_"
            f"{note}")
        asyncio.create_task(self.render_still(ch, ack, user, plan, payload))

    async def render_still(self, ch, ack, user, plan, payload):
        started = time.time()
        # one GPU, one job — wait out any running video render before grabbing it
        waited = 0
        while waited < 1800:
            try:
                snap = await asyncio.to_thread(farm_get, "/jobs")
                live = [j for j in snap.get("jobs", []) if j.get("status") == "rendering"]
            except Exception:
                break
            if not live:
                break
            if waited == 0:
                await self.safe_edit(ack, "🎨 Waiting for the video render in front of me…")
            await asyncio.sleep(10)
            waited += 10
        try:
            code, res = await asyncio.to_thread(studio_generate, payload)
        except Exception:
            log.exception("still submit failed")
            code, res = 0, {"error": "unreachable"}
        finally:
            self._job_end(getattr(user, "id", None))
        if code != 200 or not res.get("ok"):
            err = str(res.get("error") or f"HTTP {code}")[:220]
            if "GPU busy" in err:
                await self.safe_edit(ack, "🖥️ The Mac is mid-render on a video — I'll be free "
                                          "in a few minutes. Try again then.")
            else:
                await self.safe_edit(ack, f"💥 The picture render failed: {err}")
            return
        jid = str(int(time.time()))
        try:
            path = await asyncio.to_thread(get_still, res["image"], jid)
        except Exception:
            path = None
        if not path:
            await self.safe_edit(ack, f"✅ Rendered ({plan['quality']}) but I couldn't fetch "
                                      "the file — check the SnailStudio gallery.")
            return
        secs = max(1, round(time.time() - started))
        dims = f"{res.get('width', '?')}×{res.get('height', '?')}"
        mode = res.get("mode", "RGB")
        await self.safe_edit(ack, f"✅ **Done in {secs}s** — {dims} · {mode} · {plan['quality']}")
        who = f"<@{user.id}> your " if user else "Your "
        for attempt in (1, 2):
            try:
                async with ch.typing():
                    await ch.send(f"{who}picture is ready 🐌🎨",
                                  file=discord.File(path, filename=f"snora-{jid}.png"))
                break
            except discord.DiscordException:
                log.exception("still upload failed (attempt %d)", attempt)
                if attempt == 1:
                    await asyncio.sleep(3)
        else:
            await self.safe_edit(ack, f"✅ Picture rendered ({secs}s) but I couldn't post it "
                                      "here — check the SnailStudio gallery.")

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
        eta = p.get("eta")
        eta_txt = ""
        if isinstance(eta, (int, float)) and eta >= 0:
            mn, sec = divmod(int(eta), 60)
            eta_txt = f" · ~{mn}m{sec:02d}s left" if mn else f" · ~{sec}s left"
        return (f"🎬 **Rendering** {bar} **{pct}%** — {str(p.get('stage', ''))[:40]}{eta_txt}"
                f"\n_({job.get('duration')}s · {job.get('aspect')} · {job.get('quality')})_")

    @staticmethod
    def _with_ask(line, ask):
        """Keep the member's request pinned under the live status line — each
        edit overwrites the whole message, so the ask would otherwise vanish."""
        ask = (ask or "").strip()
        if not ask:
            return line
        if len(ask) > 400:
            ask = ask[:397] + "…"
        return f"{line}\n\n> Asked: {ask}"

    async def watch_job(self, jid, plan, ch, ack, user, w):
        started = w[jid]["started"]
        ask = w[jid].get("ask") or plan.get("prompt", "")
        reply_to = w[jid].get("ask_message") or None
        terminal = False   # became done/failed/removed — only THEN mark handled
        try:
            while time.time() - started < CFG["max_wait_secs"]:
                await asyncio.sleep(CFG["poll_secs"])
                try:
                    snap = await asyncio.to_thread(farm_get, "/jobs")
                    job = next((j for j in snap.get("jobs", []) if j.get("id") == jid), None)
                except Exception:
                    log.exception("watch %s: poll failed; staying alive", jid)
                    continue  # farm unreachable or a status line choked — keep waiting
                if job is None:
                    terminal = True
                    await self.safe_edit(ack, self._with_ask(
                        "🗑️ Your job was removed from the queue.", ask))
                    break
                st = job.get("status")
                if st == "queued":
                    try:
                        line = await asyncio.to_thread(
                            lambda s=snap: self._queue_line(job, s))
                    except Exception:
                        log.exception("watch %s: queue line failed", jid)
                        line = "⏳ Still in the queue — I'll edit this when my turn is up."
                    await self.safe_edit(ack, self._with_ask(line, ask))
                elif st == "rendering":
                    await self.safe_edit(ack, self._with_ask(self._render_line(job), ask))
                elif st == "done":
                    terminal = True
                    await self.deliver(jid, plan, ch, ack, user, started, ask, reply_to)
                    break
                elif st == "failed":
                    terminal = True
                    await self.safe_edit(ack, self._with_ask(
                        f"💥 The render failed: {str(job.get('error', 'unknown'))[:200]}", ask))
                    break
            else:
                await self.safe_edit(ack, self._with_ask(
                    "⌛ This one's been parked longer than expected — ping Eric if it doesn't land.",
                    ask))
        finally:
            # record completion FIRST — a crash after delivery must never make
            # a restart redeliver the same clip (this actually happened)
            w = _load_watch()
            if jid in w and terminal:
                w[jid]["done_notified"] = True
                # keep completed entries a day for forensics, then drop them
                if time.time() - started > 86400:
                    del w[jid]
                _save_watch(w)
            self._job_end(getattr(user, "id", None))

    async def deliver(self, jid, plan, ch, ack, user, started, ask=None, reply_to=None):
        mins = max(1, round((time.time() - started) / 60))
        try:
            path = await asyncio.to_thread(get_clip, jid)
        except Exception:
            path = None
        if not path:
            await self.safe_edit(ack, self._with_ask(
                f"✅ Your clip rendered ({mins} min) but the bot couldn't fetch the file — "
                "check the sNora gallery.", ask))
            return
        await self.safe_edit(ack, self._with_ask(
            f"✅ **Done in {mins} min** — {plan['duration']}s · {plan['aspect']} · "
            f"{plan['quality']}", ask))
        who = f"<@{user.id}> your " if user else "Your "
        text = self._with_ask(f"{who}sNora clip is ready 🐌🎬", ask)
        if not await self._post_clip(ch, jid, text, path, reply_to):
            await self.safe_edit(ack, self._with_ask(
                f"✅ Clip rendered ({mins} min) but I couldn't post it here — "
                "check the sNora gallery.", ask))

    async def _post_clip(self, ch, jid, text, path, reply_to=None):
        """Attach the clip, retrying once; if the channel still refuses the upload
        (or the file beats its cap), post an ungated share link so the render
        always reaches the user. Returns True if something landed."""
        kwargs = {}
        if reply_to:
            # reply to the member's own ask → guarantees they get notified, even
            # if the server suppresses mention pings
            try:
                kwargs["reference"] = await ch.fetch_message(int(reply_to))
                kwargs["mention_author"] = True
            except discord.DiscordException:
                log.warning("clip %s: could not fetch ask msg %s to reply", jid, reply_to)
        limit = getattr(getattr(ch, "guild", None), "filesize_limit", 10 * 1024 * 1024)
        attach = os.path.getsize(path) <= limit - 50_000
        for attempt in (1, 2):
            if not attach:
                break
            try:
                async with ch.typing():
                    await ch.send(text, file=discord.File(path, filename=f"{jid}.mp4"), **kwargs)
                return True
            except discord.DiscordException:
                log.exception("clip upload failed for %s (attempt %d)", jid, attempt)
                if attempt == 1:
                    await asyncio.sleep(3)
        url = await asyncio.to_thread(share_link, jid, "video")
        if url:
            try:
                await ch.send(f"{text}\n{url}", **kwargs)
                return True
            except discord.DiscordException:
                log.exception("clip link fallback failed for %s", jid)
        return False

    async def handle_research(self, ch, user, text, ack):
        """Pure web-research answer — SearXNG + snail-top summary, no render."""
        q = re.sub(r"(?i)^\s*(please\s+)?(look ?up|search(?: the web| the internet| for)?|"
                   r"google|research|find out(?: about)?|tell me about)\s*", "", text).strip(" ?.")
        q = q or text
        results = await asyncio.to_thread(searx_search, q, 6)
        if not results:
            await self.safe_edit(ack, f"🔎 I couldn't find anything on “{q[:200]}”.")
            return
        answer = await asyncio.to_thread(research_answer, q, results)
        parts = []
        if answer:
            parts.append(answer)
        for r in results[:4]:
            if r["url"]:
                parts.append(f"• [{r['title'][:80]}]({r['url']})")
        await self.safe_edit(ack, "\n".join(parts)[:1990])

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
        # go quiet-online now; resumes below will flip us to busy/DND if needed
        await self._apply_presence()
        self._last_presence_at = time.time()
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
            member = user or ch.guild.get_member(e["user"])
            if member is not None:
                self._job_start(member.id)
            log.info("resuming watch of %s", jid)
            asyncio.create_task(self.watch_job(jid, plan, ch, ack,
                                               member, _load_watch()))

    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        # A bare reply is not an ask: Discord adds the replied-to author to
        # `message.mentions` (that's the reply ping), so a throwaway "lmao"
        # reply to a finished clip used to kick off a whole new render. The
        # tell is what the member actually *typed* — an @sNora typed into a
        # reply is a real ask, the automatic reply-ping is not. The operator
        # may reply either way.
        ask_roles = {str(r) for r in (CFG.get("ask_role_ids") or [])}
        typed = set(re.findall(r"<@[!&]?(\d+)>", message.content or ""))
        typed_ping = bool(typed & (ask_roles | {str(self.user.id)}))
        if (message.reference is not None and CFG.get("ignore_replies", True)
                and not typed_ping
                and not (CFG.get("reply_owner_exempt", True)
                         and _is_owner(message.author.id))):
            log.info("ignoring reply from %s in #%s (not an ask — no typed ping)",
                     message.author, getattr(message.channel, "name", "?"))
            return
        # A ping of the server's sNora *role* counts too: members reach for
        # the role chip in the @ picker far more often than the bot account,
        # and a role ping never lands in `message.mentions`. Real pings stay
        # authoritative — a bot id pasted inside a code block pings nobody.
        pings = {str(r) for r in getattr(message, "mention_roles", []) or []}
        user_ping = self.user in message.mentions
        mentioned = user_ping or bool(ask_roles & pings)
        if not mentioned:
            return
        if not user_ping:
            log.info("role ping from %s in #%s (role %s) treated as an ask",
                     message.author, getattr(message.channel, "name", "?"),
                     ",".join(sorted(ask_roles & pings)))
        ch = message.channel
        if not self.allowed(ch):
            return
        text = re.sub(r"<@[!&]?\d+>", "", message.content).strip()
        imgs = tuple(a for a in message.attachments)
        if mentioned and not text and not imgs:
            await message.reply("Say what you'd like to see — e.g. "
                                "`@sNora a snail astronaut drifting past Saturn` — and I'll render it. "
                                "Defaults: 3s, 16:9, standard quality. Attach a photo to use it "
                                "as the shot's starting picture, or ask for a `picture`, `sticker` "
                                "or an `edit` of a photo you attach (name 'baby snail', 'the shack', "
                                "'the tiny bearded host' etc. to borrow one of the standing references).")
            return
        still = image_intent(text)
        if not text:
            # photo in, words none — animate the photo itself
            text = "bring this photo to life with natural, gentle cinematic motion"
            still = False
        # pure research lane: "@sNora look up …" answers in chat, no render
        if lookup_intent(text) and not render_intent(text):
            self.last_ask[message.author.id] = time.time()
            log.info("research from %s in #%s: %s", message.author,
                     getattr(ch, "name", "?"), text[:200])
            ack = await message.reply("🔎 Searching the web…",
                                      allowed_mentions=discord.AllowedMentions.none())
            await self.handle_research(ch, message.author, text, ack)
            return
        gap = time.time() - self.last_ask.get(message.author.id, 0)
        if _is_owner(message.author.id):
            gap = 9e9  # operator: no spam/concurrency guards, they triage the queue
        if gap < CFG["min_gap_secs"]:
            await message.reply(f"Easy there — one ask every {CFG['min_gap_secs']}s 🙂",
                                mention_author=False)
            return
        if self.active.get(message.author.id, 0) >= CFG["max_active_per_user"]:
            extra = self.inflight - self.active.get(message.author.id, 0)
            also = (f" (plus {extra} other clip{'s' if extra != 1 else ''} in the pipeline)"
                    if extra > 0 else "")
            await message.reply(
                f"⏳ I'm in use on your last render{also} — it's still in the pipeline. "
                "I'll post it here when it's done.", mention_author=False)
            return
        cap = int(CFG.get("max_active_total") or 0)
        if cap and not _is_owner(message.author.id) and self.inflight >= cap:
            await message.reply(
                f"⏳ I'm in use right now — {self.inflight} clips already in the pipeline. "
                "Give me a few minutes and mention me again.", mention_author=False)
            return
        self.last_ask[message.author.id] = time.time()
        log.info("ask from %s in #%s still=%s: %s", message.author, getattr(ch, "name", "?"),
                 still, text[:200])
        if still:
            ack = await message.reply("🎨 On it — reading the picture into words…",
                                      allowed_mentions=discord.AllowedMentions.none())
            await self.handle_still(ch, message.author, text, ack, imgs)
        else:
            ack = await message.reply("🎬 On it — writing your shot…",
                                      allowed_mentions=discord.AllowedMentions.none())
            await self.handle_ask(ch, message.author, text, ack, imgs)

    # ---- slash command /video (same pipeline, no mention needed) ----

    def build_commands(self):
        @self.tree.command(name="video", description="Generate a video with the sNora render farm")
        @discord.app_commands.describe(
            idea="What should happen in the clip",
            photo="Optional starting frame",
            ending="Optional ending frame — the clip morphs photo → ending")
        async def _video(interaction: discord.Interaction, idea: str,
                         photo: discord.Attachment = None,
                         ending: discord.Attachment = None):
            if not self.allowed(interaction.channel):
                await interaction.response.send_message("I don't work in this channel.", ephemeral=True)
                return
            if (not _is_owner(interaction.user.id)
                    and self.active.get(interaction.user.id, 0) >= CFG["max_active_per_user"]):
                await interaction.response.send_message("⏳ I'm in use — you already have a clip in "
                                                        "the pipeline.", ephemeral=True)
                return
            atts = [a for a in (photo,) if a is not None]
            end_image = None
            if ending is not None:
                try:
                    raw = await ending.read()
                    import io as _io
                    from PIL import Image
                    import pillow_heif
                    pillow_heif.register_heif_opener()
                    im = Image.open(_io.BytesIO(raw)).convert("RGB")
                    im.thumbnail((1536, 1536))
                    _buf = _io.BytesIO()
                    im.save(_buf, "JPEG", quality=88)
                    import base64 as _b64
                    end_image = ("data:image/jpg;base64,"
                                 + _b64.b64encode(_buf.getvalue()).decode())
                except Exception:
                    log.exception("slash ending attachment failed")
                    return await interaction.response.send_message(
                        "⚠️ Couldn't read that ending picture — try an attached JPG/PNG.",
                        ephemeral=True)
            await interaction.response.send_message("🎬 On it — writing your shot…")
            ack = await interaction.original_response()
            await self.handle_ask(interaction.channel, interaction.user, idea, ack,
                                  atts, end_image)

        # ── /chron — the operator's schedule control (owner only) ───────
        @self.tree.command(name="chron",
                           description="Schedules (owner only) — list, add, run, pause, remove")
        @discord.app_commands.describe(
            action="What to do",
            name="A name for the new schedule (with add)",
            interval="Hours between runs (with add)",
            quiet_start="Hour the quiet window starts, 0-23 (with add)",
            quiet_end="Hour the quiet window ends, 0-23 (with add)",
            channel="Discord channel id to deliver into (with add; omit to skip Discord)",
            app="Also deliver into the sNora app (with add)",
            schedule_id="Which schedule (with run/pause/on/remove)")
        @discord.app_commands.choices(action=[
            discord.app_commands.Choice(name="list", value="list"),
            discord.app_commands.Choice(name="add", value="add"),
            discord.app_commands.Choice(name="run now", value="run"),
            discord.app_commands.Choice(name="pause", value="pause"),
            discord.app_commands.Choice(name="resume", value="on"),
            discord.app_commands.Choice(name="remove", value="remove"),
        ])
        async def _chron(interaction: discord.Interaction,
                         action: discord.app_commands.Choice[str],
                         name: str = None, interval: int = 2,
                         quiet_start: int = 0, quiet_end: int = 8,
                         channel: str = None, app: bool = False,
                         schedule_id: str = None):
            # OWNER ONLY — this is the operator's control surface
            if not _is_owner(interaction.user.id):
                return await interaction.response.send_message(
                    "That one's the operator's.", ephemeral=True)
            act = action.value
            if act == "list":
                try:
                    data = await asyncio.to_thread(sched_get, "/schedule")
                except Exception as e:
                    return await interaction.response.send_message(
                        f"⚠️ Couldn't reach the farm schedule list ({e.__class__.__name__}).",
                        ephemeral=True)
                rows = data.get("describe") or {}
                if not rows:
                    return await interaction.response.send_message(
                        "No schedules yet. `/chron add name:Singing Dogs channel:<id>`",
                        ephemeral=True)
                lines = [f"• `{sid}` — {txt}" for sid, txt in rows.items()]
                return await interaction.response.send_message(
                    "**Schedules**\n" + "\n".join(lines)[:1900], ephemeral=True)
            if act == "add":
                if not name:
                    return await interaction.response.send_message(
                        "A new schedule needs a `name`.", ephemeral=True)
                targets = []
                if channel:
                    targets.append({"kind": "discord", "guild_id": str(interaction.guild_id or ""),
                                    "guild_name": getattr(interaction.guild, "name", ""),
                                    "channel_id": str(channel), "channel_name": ""})
                if app:
                    targets.append({"kind": "app"})
                if not targets:
                    return await interaction.response.send_message(
                        "Pick at least one delivery target: `channel:<id>`, `app:True`, or both.",
                        ephemeral=True)
                body = {"name": name, "kind": "singing_dog", "interval_hours": interval,
                        "quiet_start": quiet_start, "quiet_end": quiet_end,
                        "duration": 8, "quality": "standard", "aspect": "vertical",
                        "tz": "America/Denver", "targets": targets}
                try:
                    code, res = await asyncio.to_thread(sched_post, "/schedule", body)
                except Exception as e:
                    return await interaction.response.send_message(
                        f"⚠️ The farm didn't answer ({e.__class__.__name__}).", ephemeral=True)
                if code >= 400:
                    return await interaction.response.send_message(
                        f"🚫 {res.get('error', 'refused')}", ephemeral=True)
                return await interaction.response.send_message(
                    f"✅ {res.get('describe', 'created')}", ephemeral=True)
            if not schedule_id:
                return await interaction.response.send_message(
                    "That needs `schedule_id` — run `/chron list` first.", ephemeral=True)
            path = (f"/schedule/{schedule_id}/"
                    + {"run": "run", "pause": "toggle", "on": "toggle",
                       "remove": "delete"}[act])
            try:
                code, res = await asyncio.to_thread(sched_post, path, {})
            except Exception as e:
                return await interaction.response.send_message(
                    f"⚠️ The farm didn't answer ({e.__class__.__name__}).", ephemeral=True)
            if code >= 400:
                return await interaction.response.send_message(
                    f"🚫 {res.get('error', 'refused')}", ephemeral=True)
            if act == "remove":
                return await interaction.response.send_message("🗑️ removed.", ephemeral=True)
            if act == "run":
                return await interaction.response.send_message(
                    f"🎬 queued `{res.get('job_id')}` — it renders in turn, no priority.",
                    ephemeral=True)
            txt = res.get("describe") or ""
            return await interaction.response.send_message(f"✅ {txt}", ephemeral=True)


        @self.tree.command(name="research",
                           description="Look something up on the web (answer only, no render)")
        @discord.app_commands.describe(query="What to look up")
        async def _research(interaction: discord.Interaction, query: str):
            if not self.allowed(interaction.channel):
                await interaction.response.send_message("I don't work in this channel.",
                                                        ephemeral=True)
                return
            await interaction.response.send_message("🔎 Searching the web…")
            ack = await interaction.original_response()
            await self.handle_research(interaction.channel, interaction.user, query, ack)

        @self.tree.command(name="image",
                           description="Generate a still picture (or edit a photo) with Qwen-Image-2.1")
        @discord.app_commands.describe(
            idea="What the picture should show, or how to change the attached photo",
            photo="Optional photo to edit / use as the reference",
            shape="Canvas shape",
            sticker="Transparent sticker / cut-out (RGBA)")
        @discord.app_commands.choices(shape=[
            discord.app_commands.Choice(name="auto (match the photo)", value="auto"),
            discord.app_commands.Choice(name="square 1:1", value="square"),
            discord.app_commands.Choice(name="portrait 9:16", value="portrait"),
            discord.app_commands.Choice(name="wide 16:9", value="wide"),
        ])
        async def _image(interaction: discord.Interaction, idea: str,
                         photo: discord.Attachment = None,
                         shape: discord.app_commands.Choice[str] = None,
                         sticker: bool = False):
            if not self.allowed(interaction.channel):
                await interaction.response.send_message("I don't work in this channel.", ephemeral=True)
                return
            if (not _is_owner(interaction.user.id)
                    and self.active.get(interaction.user.id, 0) >= CFG["max_active_per_user"]):
                await interaction.response.send_message("⏳ I'm in use — you already have a render in "
                                                        "the pipeline.", ephemeral=True)
                return
            atts = [a for a in (photo,) if a is not None]
            hints = []
            if shape is not None and shape.value != "auto":
                hints.append(f"shape: {shape.value}")
            if sticker:
                hints.append("transparent sticker")
            text = idea + (f" ({'; '.join(hints)})" if hints else "")
            await interaction.response.send_message("🎨 On it — reading the picture into words…")
            ack = await interaction.original_response()
            await self.handle_still(interaction.channel, interaction.user, text, ack, atts)


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
