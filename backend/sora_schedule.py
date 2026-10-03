"""sNora farm — time-based schedules (the "chron" lane).

A schedule produces ONE job on a timer and, when that clip finishes, posts it to
a Discord channel and/or marks it for the app. Three deliberate properties:

  * a schedule only ENQUEUES — it uses the same queue as everything else and
    takes no priority, so a chron can never jump ahead of a live request;
  * QUIET HOURS ARE ENFORCED whenever the schedule ticks (also on "run now"),
    so the chrons stay silent overnight;
  * delivery is by REST with the bot token, so the farm does not need the
    gateway that `discord_bot.py` holds.

Timezone comes from `zoneinfo` (present in the system python 3.9 that launchd
runs the service with).
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:                                    # pragma: no cover
    ZoneInfo = None

HOME = os.path.expanduser("~")
DEFAULT_TZ = "America/Denver"
DISCORD_API = "https://discord.com/api/v10"

# ── reference images the dog lane can draw from ─────────────────────────
# Any of these manifests on the MacBook may hold a dog photo; a schedule may
# instead name an exact asset key from the farm's own library.
MANIFEST_CANDIDATES = (
    f"{HOME}/snail3d.com/sora/shack_manifest.json",
    f"{HOME}/snail3d.com/sora/jambo_manifest.json",
)
DOG_RE = re.compile(r"\b(dog|puppy|pup|canine|corgi|retriever|shepherd|terrier|hound|"
                    r"beagle|poodle|bulldog|husky|malamute|collie|dachshund|chihuahua|"
                    r"labrador|rottweiler|doberman|pug|shiba|akita|setter|spaniel)\b", re.I)

# ── the SINGING scaffold ────────────────────────────────────────────────
# The farm's normal SPOKEN_CLAUSE says "delivers the quoted line clearly in
# natural English ... lips moving in sync". That is the wrong instruction for a
# dog singing a foreign anthem: an animal has no lips to sync and the line is
# not English. Both clauses fight the engine, so the chron lane scaffolds the
# singing shot itself.
SING_CLAUSE = ("The subject sings the quoted line aloud on the actual melody, in the "
               "language named in the tag and no other, one continuous take, the mouth "
               "and throat open and working on every phrase; every other moment carries "
               "only the scene's own sound.")

# Reference photos for the dog lane: any image dropped here is picked up
# automatically (newest wins), so a dog can be added without editing a manifest.
DOG_PHOTO_DIRS = (
    f"{HOME}/snail3d.com/sora/dogs",
    f"{HOME}/vpipe/sandbox/uploads/dogs",
)
PHOTO_EXT = (".png", ".jpg", ".jpeg", ".webp")


def scaffold_sing(prompt, soundscape=None, music=None):
    """The scaffold for a sung shot — never the spoken-English one."""
    body = f"{prompt} {SING_CLAUSE}"
    sound = ("one clear non-human voice singing the quoted line, plus "
             + " ".join(str(soundscape or "").split()))
    m = " ".join(str(music or "").split()) or "N/A"
    return (f"integrated_multimodal_description: [Shot 1] {body}\n"
            f"overall_soundscape: {sound[:600]}\n"
            f"non_diegetic_music: {m[:300]}")


def find_dog_photo():
    """Newest image in the dog photo folders, or None."""
    best, best_m = None, -1
    for d in DOG_PHOTO_DIRS:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.lower().endswith(PHOTO_EXT):
                continue
            p = os.path.join(d, n)
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m > best_m:
                best, best_m = p, m
    return best


# ══════════════════════════════════════════════════════════════════════
# Discord REST — no gateway, so the farm itself can deliver
# ══════════════════════════════════════════════════════════════════════

def bot_token(cfg=None):
    """The bot token: sora_config.json's `discord.token_file` (default ~/.sora_discord_token)."""
    path = None
    if isinstance(cfg, dict):
        path = ((cfg.get("discord") or {}).get("token_file")
                or cfg.get("token_file"))
    path = path or f"{HOME}/.sora_discord_token"
    try:
        with open(os.path.expanduser(path)) as f:
            return f.read().strip()
    except OSError:
        return ""


def _api(token, method, path, payload=None, timeout=25):
    url = DISCORD_API + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bot {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "snora-farm/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def list_guilds(cfg=None):
    """Every server the bot is in — the picker for "which Discord"."""
    tok = bot_token(cfg)
    if not tok:
        return []
    out = []
    for g in _api(tok, "GET", "/users/@me/guilds"):
        out.append({"id": str(g["id"]), "name": g.get("name", "?")})
    return sorted(out, key=lambda g: g["name"].lower())


def list_text_channels(guild_id, cfg=None):
    """Postable text channels in one server (type 0 = text, 5 = announcement)."""
    tok = bot_token(cfg)
    if not tok:
        return []
    out = []
    for c in _api(tok, "GET", f"/guilds/{int(guild_id)}/channels"):
        if int(c.get("type", -1)) in (0, 5):
            out.append({"id": str(c["id"]), "name": c.get("name", "?")})
    return sorted(out, key=lambda c: c["name"].lower())


def _multipart(fields, filename, content_type, blob):
    """Hand-rolled multipart body — urllib only, no extra deps."""
    b = "----snora" + os.urandom(8).hex()
    out = []
    for name, value in fields.items():
        out.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                   f"{value}\r\n".encode())
    out.append(f"--{b}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
               f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode())
    out.append(blob)
    out.append(f"\r\n--{b}--\r\n".encode())
    return b, b"".join(out)


def post_clip(channel_id, path, content="", cfg=None, timeout=180):
    """Upload a finished clip to a channel. Returns the message dict."""
    tok = bot_token(cfg)
    if not tok:
        raise RuntimeError("no Discord bot token configured")
    with open(path, "rb") as f:
        blob = f.read()
    if not blob:
        raise RuntimeError("clip file is empty")
    boundary, body = _multipart({"payload_json": json.dumps({"content": content[:1900]})},
                                os.path.basename(path), "video/mp4", blob)
    req = urllib.request.Request(
        f"{DISCORD_API}/channels/{int(channel_id)}/messages", data=body, method="POST",
        headers={"Authorization": f"Bot {tok}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "snora-farm/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def channel_label(guild_id, channel_id, cfg=None):
    """Human name for a delivery target, for the app's schedule list."""
    try:
        names = {c["id"]: c["name"] for c in list_text_channels(guild_id, cfg)}
        return "#" + names.get(str(channel_id), str(channel_id))
    except Exception:
        return "#" + str(channel_id)


# ══════════════════════════════════════════════════════════════════════
# The singing-dog generator
# ══════════════════════════════════════════════════════════════════════
#
# Public-domain songs only (authors dead well over a century — no royalties,
# no takedowns). Each carries the language it is nationally known in, a short
# opening line inside the clip's word budget, and a setting that fits the song,
# so the visual and the soundscape both match the language.

DOG_SONGS = [
    {"id": "fr_marseillaise", "country": "France", "title": "La Marseillaise",
     "lang_name": "French", "lang_code": "fr",
     "lyric": "Allons enfants de la patrie !",
     "setting": "a cramped Parisian boulangerie at dawn, croissants stacked on the "
                "counter and a chalkboard menu in French on the wall",
     "music": "a solo accordion and a snare drum march in a brisk French national "
              "style, rising under the vocal line",
     "soundscape": "the low hum of a bakery oven, a bell over the shop door, trays "
                   "settling on a steel rack"},
    {"id": "de_lied", "country": "Germany", "title": "Das Lied der Deutschen",
     "lang_name": "German", "lang_code": "de",
     "lyric": "Einigkeit und Recht und Freiheit",
     "setting": "a timber-framed Bavarian beer hall, long shared tables and hanging "
                "iron lanterns, a brass band's instruments stacked nearby",
     "music": "a low brass and tuba oom-pah in a German beer-hall style, steady and "
              "broad under the vocal",
     "soundscape": "steins clinking, a distant crowd murmur, the creak of wooden "
                   "benches"},
    {"id": "it_mameli", "country": "Italy", "title": "Il Canto degli Italiani",
     "lang_name": "Italian", "lang_code": "it",
     "lyric": "Fratelli d'Italia, l'Italia s'è desta",
     "setting": "a sunlit Venetian side canal, laundry strung overhead and a small "
                "wooden boat moored at the stone edge",
     "music": "a mandolin tremolo and a single operatic tenor register in an "
              "Italian bel canto style",
     "soundscape": "water lapping the stone, a rope creaking against a mooring "
                   "post, gulls high overhead"},
    {"id": "es_himno", "country": "Spain", "title": "Marcha Real",
     "lang_name": "Spanish", "lang_code": "es",
     "lyric": "¡Viva España! Alzad la frente",
     "setting": "a whitewashed Andalusian plaza at golden hour, orange trees in "
                "terracotta pots and a tiled fountain at the centre",
     "music": "a nylon-string guitar and castanets in a Spanish march rhythm, "
              "warm and proud under the vocal",
     "soundscape": "the fountain's steady water, cicadas in the orange trees, a "
                   "distant church bell"},
    {"id": "ru_anthem", "country": "Russia", "title": "Gosudarstvenny Gimn",
     "lang_name": "Russian", "lang_code": "ru",
     "lyric": "Россия — священная наша держава",
     "setting": "a snowbound Russian village at dusk, frost on the porch rail and "
                "warm lamplight in the windows",
     "music": "a deep male-choir pad and a slow orchestral swell in a Russian "
              "anthem style",
     "soundscape": "dry snow squeaking underfoot, wind moving through a bare birch, "
                   "a stove ticking"},
    {"id": "jp_kimigayo", "country": "Japan", "title": "Kimigayo",
     "lang_name": "Japanese", "lang_code": "ja",
     "lyric": "君が代は 千代に八千代に",
     "setting": "a quiet Kyoto garden in spring, a stone lantern beside a koi pond "
                "and cherry petals on the water",
     "music": "a solo koto and a low shakuhachi line in a Japanese court style, "
              "sparse and slow",
     "soundscape": "water trickling into the pond, a bamboo clapper, wind in the "
                   "cherry branches"},
    {"id": "br_hino", "country": "Brazil", "title": "Hino Nacional Brasileiro",
     "lang_name": "Portuguese", "lang_code": "pt",
     "lyric": "Ouviram do Ipiranga as margens plácidas",
     "setting": "a Rio de Janeiro rooftop at sunset with Sugarloaf in the distance, "
                "tiled floor and potted palms",
     "music": "a nylon-string guitar and a soft cavaquinho in a Brazilian choro "
              "style, warm and rolling",
     "soundscape": "distant surf, city traffic far below, a light breeze across the "
                   "roof tiles"},
    {"id": "in_jana", "country": "India", "title": "Jana Gana Mana",
     "lang_name": "Bengali Hindi", "lang_code": "hi",
     "lyric": "जन गण मन अधिनायक जय हे",
     "setting": "a Marigold-decorated courtyard in Jaipur at dawn, carved sandstone "
                "arches and a brass water vessel on the step",
     "music": "a sitar drone and a soft tabla pulse in a Hindustani raga style, "
              "gentle under the vocal",
     "soundscape": "birds in the courtyard, a temple bell far off, water poured "
                   "from a brass pot"},
    {"id": "us_anthem", "country": "United States", "title": "The Star-Spangled Banner",
     "lang_name": "English", "lang_code": "en",
     "lyric": "Oh, say can you see, by the dawn's early light",
     "setting": "a small-town American baseball diamond at dusk, chalk lines bright "
                "under floodlights and a flag on the pole beyond the fence",
     "music": "a single clean trumpet and a slow snare roll in an American anthem "
              "style",
     "soundscape": "floodlight hum, a distant crowd murmur, cleats on packed dirt"},
    {"id": "mx_himno", "country": "Mexico", "title": "Himno Nacional Mexicano",
     "lang_name": "Spanish", "lang_code": "es",
     "lyric": "Mexicanos, al grito de guerra",
     "setting": "a Oaxacan courtyard kitchen at night, terracotta tiles and hanging "
                "mole pots, warm bulb light",
     "music": "a mariachi trumpet and vihuela in a Mexican ranchera style, bold and "
              "brassy",
     "soundscape": "pots simmering, a distant dog bark, a wooden spoon against "
                   "clay"},
    {"id": "il_hatikvah", "country": "Israel", "title": "Hatikvah",
     "lang_name": "Hebrew", "lang_code": "he",
     "lyric": "כל עוד בלבב פנימה",
     "setting": "a stone Jerusalem balcony at sunrise, an olive tree in a clay pot "
                "and pale limestone walls",
     "music": "a solo clarinet and a low string drone in a minor-key Jewish "
              "liturgical style",
     "soundscape": "a light morning wind, a distant dove, stone warming in the "
                   "sun"},
    {"id": "cn_march", "country": "China", "title": "March of the Volunteers",
     "lang_name": "Chinese", "lang_code": "zh",
     "lyric": "起来！不愿做奴隶的人们！",
     "setting": "a Sichuan teahouse courtyard in the morning, bamboo chairs and a "
                "steaming brass kettle on the table",
     "music": "a solo erhu and a bright pipa line in a Chinese national march "
              "style",
     "soundscape": "tea poured from a long-spout kettle, bamboo chairs scraping, "
                   "distant conversation"}]


def song_by_id(sid):
    for s in DOG_SONGS:
        if s["id"] == sid:
            return s
    return None


def pick_song(avoid=None, seed=None):
    """The next song in rotation, never the one we just used unless there is only one."""
    avoid = avoid or []
    pool = [s for s in DOG_SONGS if s["id"] not in avoid] or DOG_SONGS
    if seed is None:
        import random
        seed = random.getrandbits(32)
    return pool[seed % len(pool)]


# ── reference dog photos ────────────────────────────────────────────────

def _find_dog_asset(cfg=None):
    """(path, key, description) for a dog in the farm's reference manifests."""
    for mf in MANIFEST_CANDIDATES:
        try:
            with open(mf) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for a in data.get("assets", []):
            blob = " ".join(str(a.get(k) or "") for k in ("key", "desc", "set", "file"))
            if DOG_RE.search(blob):
                path = a.get("file")
                if path and not os.path.isabs(str(path)):
                    path = os.path.join(os.path.dirname(mf), str(path))
                if path and os.path.isfile(str(path)):
                    return str(path), a.get("key"), a.get("desc") or ""
    return None, None, None


# ── the prompt ──────────────────────────────────────────────────────────

# ── the cast: every singer is a super-ripped EXOTIC American Bully ──────
# Eric's casting call (2026-10-03): exotic bullies, "super ripped". No bully
# photo exists in any manifest or drop-in folder, so the type has to be named
# outright or the renderer gives a generic dog. "Exotic" carries real meaning to
# this model: an extremely short muzzle, an oversized blocky head and a squat,
# overbuilt frame. The look is ROTATED by song so a 2-hourly schedule does not
# post the same animal every time.
BULLY_LOOKS = (
    "a heavily muscled EXOTIC American Bully — an extremely short muzzle, a huge "
    "blocky head, tiny rose ears and a wide bow-legged stance, with the muscle "
    "ridged and shredded under a short black coat, veins standing out across the "
    "shoulders and thighs",
    "a massively built exotic American Bully with a very short snout, a broad "
    "domed skull and a deep barrel chest tapering to a tiny waist, the shredded "
    "shoulders and forearms bulging under a blue-grey coat with a white chest",
    "a super-ripped exotic American Bully with a stunted muzzle, an enormous "
    "wide head and thick-set bow legs, muscle striations cut deep into its "
    "chest, shoulders and haunches under a chocolate-and-white coat",
    "an extreme exotic American Bully with a flat short muzzle, heavy jowls and a "
    "massive overbuilt chest, every muscle group separated and bulging under a "
    "tight fawn coat with a black mask",
    "an extremely muscular exotic American Bully with a tiny muzzle, a wide "
    "square head and a squat powerful frame, veins ridging over the shoulders "
    "and the thighs heavily carved under a champagne coat",
    "a wildly over-muscled exotic American Bully with a very short snout and a "
    "huge rounded skull, its chest and shoulders shredded and deeply cut under a "
    "merle coat with white markings",
    "a shredded exotic American Bully with a stubby muzzle, an oversized blocky "
    "head and a broad muscular frame, the muscle ridged and veined across its "
    "chest and forearms under a white coat with brindle patches",
    "a heavyweight exotic American Bully with a very short muzzle, a massive "
    "square skull and a low heavy stance, deeply striated muscle bulging over "
    "its shoulders and haunches under a lilac coat",
)
BULLY_NOTE = ("The singer is an exotic American Bully: a dog, NOT a person, with a dog's "
              "short muzzle and dog vocal anatomy. Its build is exaggeratedly muscular "
              "and shredded, and it never speaks words — it only sings them on the "
              "melody.")


def bully_look(song_id, override=None):
    """The look for this song — stable per song, so the cast stays recognisable."""
    if override:
        return override
    if not song_id:
        return BULLY_LOOKS[0]
    i = sum(ord(c) for c in str(song_id)) % len(BULLY_LOOKS)
    return BULLY_LOOKS[i]


def build_dog_prompt(song, ref_desc=None, ref_from_photo=True, ref_mode="scene",
                     look=None):
    """A vertical singing-dog shot for one song.

    The dog is the subject the whole way through: it sings the line, and every
    other sound in the mix stays non-vocal so the vocal branch has nothing to
    fight. The <d>[Language] tag is what the engine actually sings in.
    """
    if ref_desc:
        who = ("the American Bully from the reference photograph — "
               + ref_desc.strip().rstrip("."))
    elif ref_from_photo:
        who = "the American Bully from the reference photograph"
    else:
        # no reference image: the shot has to carry the whole animal itself
        who = bully_look(song.get("id"), look)
    prompt = (
        f"Live-action, cinematic, a vertical medium close-up frames {who}, in "
        f"{song['setting']}. {who.capitalize()} sits up on its hind legs with its "
        f"chest out and sings with visible effort: muzzle opening wide on every "
        f"phrase, tongue and throat working, ears lifting and settling with the "
        f"melody, head tipping back slightly on the high notes, front paws keeping "
        f"a loose rhythm, jowls flapping on the loud notes while the muscle ridges "
        f"and flexes across its chest and shoulders with every phrase. It sings the opening "
        f"of \"{song['title']}\", "
        f"{song['country']}'s national song, in {song['lang_name']}, on the actual "
        f"melody, in a full carrying voice. Whoever is filming holds the phone at "
        f"the dog's eye level and stays put, one locked vertical frame, the dog "
        f"centred with its head and chest inside the frame the whole time. "
        f"Nobody else sings, speaks or makes any vocal sound. "
        f"{BULLY_NOTE} "
        f"The dog sings: <d>[{song['lang_name']}] {song['lyric']}</d>"
    )
    return prompt


def dog_soundscape(song):
    return (song["soundscape"] +
            ". No human voices, no crowd, no singing other than the dog itself.")


def make_dog_job(schedule, cfg=None):
    """Build the job payload for one tick of a `singing_dog` schedule."""
    song = song_by_id(schedule.get("last_song_id") or "")
    if song is None:
        song = pick_song(avoid=[schedule.get("last_song_id")] if schedule.get("last_song_id") else [])
    else:
        # the next song in the rotation after the one we used
        idx = DOG_SONGS.index(song)
        song = DOG_SONGS[(idx + 1) % len(DOG_SONGS)]

    image_key = schedule.get("image_key")
    image_path, ref_desc, ref_mode = None, None, "scene"
    if not image_key:
        path, key, desc = _find_dog_asset(cfg)
        if key:
            image_key, ref_desc = key, desc
        else:
            # no manifest entry — the drop-in dog folder is the next best thing
            image_path = find_dog_photo()

    return {
        "prompt": build_dog_prompt(song, ref_desc,
                                   ref_from_photo=bool(image_key or image_path)),
        "soundscape": dog_soundscape(song),
        "music": song["music"],
        # 9:16, per Eric — vertical-hd (480x854) is the bucket that FITS; the
        # 704x1280 "vertical" needs a ~2.9 GB scratch forward the engine refuses
        # when other models hold the GPU (measured 2026-10-02)
        "aspect": schedule.get("aspect") or "vertical-hd",
        "duration": int(schedule.get("duration") or 8),
        "quality": schedule.get("quality") or "standard",
        "image_key": image_key,
        "image_path": image_path,
        "ref_mode": ref_mode,
        # tells the renderer to rewrite the spoken scaffold for a sung foreign line
        "sing": True,
        "sing_lang": f"{song['lang_name']} (language code {song['lang_code']})",
        "song_id": song["id"],
        "song_title": song["title"],
        "song_lang": song["lang_name"],
    }


# ══════════════════════════════════════════════════════════════════════
# Schedule arithmetic
# ══════════════════════════════════════════════════════════════════════

def zone_of(schedule):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(schedule.get("tz") or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def in_quiet_hour(hour, q_start, q_end):
    """True when `hour` is inside QUIET hours; the window [q_start, q_end) may wrap
    midnight (00:00-08:00 and 22:00-06:00 are both valid). q_start == q_end means
    "never quiet" — the schedule may run around the clock."""
    if q_start == q_end:
        return False
    if q_start < q_end:
        return q_start <= hour < q_end
    return hour >= q_start or hour < q_end


def quiet_hours(schedule):
    return int(schedule.get("quiet_start") or 0), int(schedule.get("quiet_end") or 0)


def in_active_window(schedule, now=None):
    """False while the schedule is in its quiet window — chrons never fire there."""
    z = zone_of(schedule)
    now = now or time.time()
    hour = datetime.fromtimestamp(now, z).hour if z else datetime.fromtimestamp(now).hour
    return not in_quiet_hour(hour, *quiet_hours(schedule))


def next_run_time(schedule, frm=None):
    """The first slot after `frm` that lands OUTSIDE quiet hours.

    Slots are `interval_hours` apart; a slot that falls in the quiet window rolls
    forward to the hour the window closes, so a 2h schedule with quiet 00:00-08:00
    runs 08,10,...,22 and then 08 the next morning.
    """
    z = zone_of(schedule)
    interval = max(1, int(schedule.get("interval_hours") or 2)) * 3600
    qs, qe = quiet_hours(schedule)
    t = (frm if frm is not None else time.time()) + interval
    # snap to the hour so repeated rolls stay on clean boundaries
    # CEIL to the hour (a slot already on the hour must not drift): 08:00 + 2h
    # stays 10:00, while 08:30 + 2h becomes 11:00.
    t = -(-int(t) // 3600) * 3600
    for _ in range(96):                    # at most 4 days of hourly steps
        hour = datetime.fromtimestamp(t, z).hour if z else datetime.fromtimestamp(t).hour
        if not in_quiet_hour(hour, qs, qe):
            return t
        t += 3600
    return t


def due(schedule, now=None):
    """(bool, slot) — is this schedule owed a run?"""
    if not schedule.get("enabled"):
        return False, None
    now = now or time.time()
    nxt = schedule.get("next_run")
    if not nxt:
        return False, None
    if now >= float(nxt):
        return True, float(nxt)
    return False, None


def advance(schedule, now=None):
    """Move `next_run` past `now`, skipping quiet hours."""
    now = now or time.time()
    schedule["next_run"] = next_run_time(schedule, now)


def new_schedule(name, kind="singing_dog", interval_hours=2, quiet_start=0, quiet_end=8,
                 tz=DEFAULT_TZ, targets=None, enabled=True, **extra):
    """A schedule record. `targets` is a list of delivery targets:
        {"kind": "discord", "guild_id": "...", "guild_name": "...", "channel_id": "...",
         "channel_name": "..."}
        {"kind": "app"}

    `quiet_start`/`quiet_end` define the hours the schedule stays SILENT (may wrap
    midnight; equal values mean "never quiet"). They are enforced on every tick
    and on a manual run-now — a chron never fires inside them.
    """
    import hashlib
    sid = "sched_" + hashlib.sha1(f"{name}{time.time()}".encode()).hexdigest()[:10]
    rec = {
        "id": sid,
        "name": name,
        "kind": kind,
        "enabled": bool(enabled),
        "interval_hours": max(1, int(interval_hours)),
        "quiet_start": max(0, min(23, int(quiet_start))),
        "quiet_end": max(0, min(24, int(quiet_end))),
        "tz": tz,
        "targets": list(targets or []),
        "created": time.time(),
        "last_run": None,
        "last_job_id": None,
        "last_song_id": None,
        "next_run": None,
    }
    rec.update(extra)
    rec["next_run"] = next_run_time(rec)
    return rec


def describe(schedule, now=None):
    """One line the app and Discord can both render."""
    now = now or time.time()
    z = zone_of(schedule)
    nxt = schedule.get("next_run")
    when = (datetime.fromtimestamp(float(nxt), z).strftime("%a %H:%M")
            if nxt and z else "—")
    tzname = datetime.fromtimestamp(now, z).strftime("%Z") if z else ""
    state = "on" if schedule.get("enabled") else "off"
    return (f"{schedule['name']} — every {schedule['interval_hours']}h "
            f"(quiet {schedule['quiet_start']:02d}:00-{schedule['quiet_end']:02d}:00 "
            f"{tzname}) [{state}] · next {when}")


# ══════════════════════════════════════════════════════════════════════
# The dispatcher tick — the service calls this; `submit` enqueues, `save` persists
# ══════════════════════════════════════════════════════════════════════

def tick(store, submit, save=None, now=None, log=print):
    """Fire every due schedule. Returns the list of (schedule, job) spawned.

    `submit(payload, source)` must create a normal queued job (no priority) and
    return its id. Quiet hours are checked here and in `advance`, so a tick can
    never fire overnight even if `next_run` was written badly.
    """
    now = now or time.time()
    fired = []
    for s in store.get("schedules", []):
        if not s.get("enabled"):
            continue
        is_due, slot = due(s, now)
        if not is_due:
            continue
        if not in_active_window(s, now):
            # never fire in quiet hours — push to the next open slot
            advance(s, now)
            log(f"[sched] {s['id']} {s['name']}: quiet hours, rolled to "
                f"{time.strftime('%H:%M', time.localtime(s['next_run']))}")
            continue
        try:
            job = build_job(s)
        except Exception as e:
            log(f"[sched] {s['id']} {s['name']}: build failed: {e}")
            advance(s, now)
            continue
        payload = job.pop("payload")
        meta = job.pop("meta", {})
        try:
            jid = submit(payload, f"sched:{s['id']}")
        except Exception as e:
            log(f"[sched] {s['id']} {s['name']}: enqueue failed: {e}")
            advance(s, now)
            continue
        s["last_run"] = now
        s["last_job_id"] = jid
        s["last_song_id"] = meta.get("song_id")
        s["last_song_title"] = meta.get("song_title")
        s["last_song_lang"] = meta.get("song_lang")
        s["pending_job"] = jid
        s["delivered"] = False
        advance(s, now)
        fired.append((s, jid))
        log(f"[sched] {s['id']} {s['name']}: queued {jid} "
            f"({meta.get('song_title')} / {meta.get('song_lang')}) next "
            f"{time.strftime('%H:%M', time.localtime(s['next_run']))}")
    if fired and save:
        save(store)
    return fired


def build_job(schedule):
    """The job for one schedule kind, split into (farm payload, delivery meta)."""
    kind = schedule.get("kind") or "singing_dog"
    if kind == "singing_dog":
        d = make_dog_job(schedule)
        meta = {"song_id": d.pop("song_id"), "song_title": d.pop("song_title"),
                "song_lang": d.pop("song_lang")}
        # an explicit file path is only set when no manifest key matched
        if not d.get("image_path"):
            d.pop("image_path", None)
        return {"payload": d, "meta": meta}
    if kind == "custom":
        return {"payload": dict(schedule.get("payload") or {}),
                "meta": {"title": schedule.get("name")}}
    raise ValueError(f"unknown schedule kind {kind!r}")