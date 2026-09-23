# Photo & image engines

sNora's Create screen makes stills as well as video. Stills do **not** go through
the video farm — they go to a local **SnailStudio** process on the same Mac
(`http://<mac>:7778`), which the phone reaches over the tailnet. The phone picks
an engine per job; `AssistAPI` decides which one from what you asked for.

| Engine | Best for | Needs a reference photo? |
|---|---|---|
| **Qwen-Image-2.1** | changing a photo while keeping a person/product/logo/text intact; building a new picture from up to 10 photos; real transparent stickers; pictures with readable text | optional |
| **Flux** (`draw-things-cli`) | a plain artistic photo from words | no |
| **ERNIE-Image** | legacy text-to-image (kept for old jobs) | no |

The planner's job kinds map onto these:

```
photo_qwen    text -> image (or image -> image), Qwen engine
qwen_edit     edit one attached photo, Qwen engine
qwen_multi    compose from 2+ attached photos, Qwen engine
qwen_sticker  transparent RGBA sticker, Qwen engine
photo_flux    plain photo, Flux engine
img2img       light Flux restyle of one photo
```

---

## Qwen-Image-2.1

A unified text-to-image **and** image-editing model from Alibaba: a 7B
single-stream DiT, a Qwen3-VL 8B text/vision encoder, and a 64-channel RGBA VAE.
It runs in bf16 on Apple Silicon (MPS) in ~62 GB of unified memory, and supports

- text-to-image at native 2K,
- editing with **up to 10 reference images**,
- identity preservation for people and products,
- native **RGBA transparency** (real alpha, not a faked checkerboard),
- prompt rewriting models (`Qwen-Image-2.1-PE-T2I` / `-PE-I2I`) for expanding
  short prompts.

Model: `huggingface.co/Qwen/Qwen-Image-2.1` · code: `github.com/QwenLM/Qwen-Image-2.1`

### Install (on the Mac that runs SnailStudio)

```bash
python3 -m venv ~/qwen-env
~/qwen-env/bin/pip install torch==2.14.0 torchvision==0.29.0 numpy==2.5.3
~/qwen-env/bin/pip install transformers==5.17.0 huggingface_hub==1.32.0 \
    accelerate==1.15.0 safetensors==0.8.0 tokenizers==0.23.2 \
    sentencepiece==0.2.2 pillow==12.3.0
~/qwen-env/bin/pip install "git+https://github.com/huggingface/diffusers.git"
~/qwen-env/bin/hf download Qwen/Qwen-Image-2.1
```

`QwenImage21Pipeline` needs a recent `diffusers` (0.41+ / `main`) and
`transformers>=5.17`; the pinned stack above is the one verified on M5 Max.

SnailStudio ships `qwen_worker.py` — a persistent worker that keeps the weights
resident between calls and releases them after 15 idle minutes — and exposes it
as `POST /api/editor/ai/generate-qwen`.

### The endpoint

```
POST http://<mac>:7778/api/editor/ai/generate-qwen
{
  "prompt":       "…",              // required
  "aspect":       "wide|portrait|square",   // or explicit width/height
  "quality":      "fast|standard|best",     // canvas scale: 0.5 / 0.75 / 1.0
  "width": 2048, "height": 2048,            // override aspect
  "steps":        40,                        // default 40
  "seed":         -1,
  "transparent":  true,                      // native RGBA
  "images":       ["data:image/png;base64,…"], // 0 = t2i, 1 = edit, up to 10
  "image_paths":  ["/abs/path.png"],         // server-side refs (editor use)
  "true_cfg_scale": 4.0, "negative_prompt": "monochrome, greyscale, …",  // edits only
  "use_kv_cache": true
}
```

Response:

```
{"ok": true, "image": "/generated/qwen_….png",
 "width": 2048, "height": 2048, "mode": "RGBA",
 "transparent": true, "references": 2}
```

The server maps `aspect` through the model's official 2K canvases and scales by
`quality`:

| aspect | 1:1 | 16:9 | 9:16 | 3:2 | 2:3 |
|---|---|---|---|---|---|
| best | 2048×2048 | 2752×1536 | 1536×2752 | 2528×1696 | 1696×2528 |

SnailStudio's worker also wraps a `transparent: true` prompt in the model's
recommended frame for you (see below), and reports the output colour mode.

---

## Prompting Qwen-Image-2.1

Qwen was trained together with two prompt-rewriting models, and it reads a
**description of the finished frame**, not a list of commands. The rules below
are distilled from the official `system_prompt_t2i.txt` and
`system_prompt_edit.txt` that ship in the model repo — they are the difference
between a generic result and a faithful one. The sNora planner carries a short
version of these so the phone writes prompts the model understands.

### Text-to-image — describe the picture

- **Observe, don't instruct.** Present tense, third person. No "create", "make
  sure", "you should". No quality boosters ("masterpiece", "8K", "highly
  detailed").
- **Open with the medium.** One sentence naming the style, subject and
  background: *"A vertical realistic photograph of a ceramic teapot on a
  weathered oak table, soft window light."*
- **Place everything.** Use 8–14 positional phrases that reach the corners,
  edges and centre — "in the upper-left corner", "across the lower third" — so
  the frame is locatable. Roughly a third of the sentences should open on the
  position.
- **Walk the frame in order** (background first, then top band, left→centre→right,
  then bottom band) or, for a single subject, subject-out.
- **Give the lighting its own sentence.** Source, direction, quality, and the
  shadows it casts.
- **Name colours with a modifier** (deep navy, muted olive, warm terracotta),
  never bare. Give the material (brushed metal, coarse linen), not just the noun.
- **Enumerate; never summarise.** "Several items" is not a description. Write
  small counts as words.
- **Hedge what you can't be sure of** ("appears to be", "a notebook or a tablet")
  and be flatly definite only about what the user fixed.
- **Close on the whole frame** in one sentence: *"The overall composition …"*.
- Target ~20 sentences / 400–500 words. A three-word brief still gets a full
  description — you are inventing the frame, not writing less.

### Editing — disentangle the attribute

The governing principle is **edit exactly what was named, push it strong, hold
everything else at input fidelity.** Two symmetric failures: *leakage* (touching
what wasn't named) and *under-editing* (a result mistakable for the input).

- **Lead with the operation**, not a description of the result. *"Change the
  background to a sunset beach."*
- **Name only the target, and push it unmistakable.**
- **Then one blanket preservation clause** naming what stays *by type, position
  and role* — "keep the person, the Qwen logo, the headline text and the
  composition exactly as they are." Do **not** describe a kept thing in concrete
  detail: a preservation description reads to the model as a generation
  instruction, so the more you describe it, the more it drifts.
- **Identity is the hardest invariant.** Point at the reference image rather than
  describing features in words — verbal descriptions make the model regenerate
  and lose the likeness.
- **Text is literal.** Commit to the exact characters in double quotes, in the
  input's own script; never summarise or invent.
- **Resolve ambiguity, then commit** — pick the reasonable reading and state it.
- **Write from the perspective of someone holding only the input image(s).**
- **Affirmative, not prohibitive.** State what to do ("keep the background
  unchanged"), not a wall of "do not".

### Multiple reference images (up to 10)

- The pipeline takes a **flat list** applied to every prompt, so call it once with
  the set you mean.
- **Give each image a role and a number.** The prompt-enhancer refers to inputs as
  `<image1>`, `<image2>`, …, and numbers images in the prompt: *"Reference image 1
  is the speaker — preserve his face. Reference image 2 is the Qwen logo — use it
  exactly once. Do not confuse the references."*
- **Never compress several images into a range** ("the logos"); describe each one.
- If there is a **canvas** (a scene to composite into, a body to swap a face onto,
  a person for an outfit), say which image defines the layout and which supply
  material.

### Transparent stickers (RGBA)

The model generates transparency natively. Use the recommended format:

> `This is an RGBA image with transparency. <your description>. The image has
> alpha channel and the background is transparent.`

Then name the cut-out subject, its outline/border, and any text. SnailStudio's
worker adds this frame automatically when `transparent` is set and the prompt
does not already mention RGBA/alpha/transparency.

### Aspect ratio and resolution

Decide the canvas from the subject (or the user's words). Common maps:
portrait/avatar/album → `1:1`; video thumbnail/slide/desktop → `16:9`;
Stories/Reels/phone wallpaper → `9:16`; poster/ID → `3:4`; panorama → `2:1`.
**Never write a ratio or pixel count into the prompt** — it is set through the
API fields. "2K/4K/8K" are quality words, not aspect cues; render at 2K level
regardless.

### Guidance

Qwen-Image-2.1's own default is un-guided (`true_cfg_scale=1.0`), and
**text-to-image is fine that way**. On **image edits it is not**: with an edit,
un-guided output collapses to a flat, desaturated *embossed greyscale relief* of
the input. Verified four ways on M5 Max — cfg 1 with no negative, cfg 4 with a
neutral negative, cfg 4 with a richly-worded instruction, and an enriched
instruction all came back grey; the only thing that restored colour and realism
was a negative prompt that *names the failure mode*.

So `POST /api/editor/ai/generate-qwen` now defaults, **whenever reference images
are present**, to

```
true_cfg_scale = 4.0
negative_prompt = "monochrome, greyscale, grayscale, desaturated, embossed,
                   relief, washed out, low contrast, flat lighting, blurry,
                   low quality"
```

and leaves both at defaults for plain text-to-image. Callers can still override
either field. If you pass `true_cfg_scale>1` yourself, you must also pass a
non-empty `negative_prompt`, or guidance is skipped entirely.

Also: send **opaque RGB references** for non-transparent requests. Hand the model
an RGBA reference and it regenerates its own alpha, often mostly empty, which
flattens onto a dark UI as a ghost. The studio converts references to RGB and
flattens stray output alpha onto white when `transparent` is false.

### Prefix KV cache

Leave `use_kv_cache=true` (the default). The condition images and text are
encoded once and reused across all denoising steps, which is a large speedup for
edits and multi-reference jobs. Toggling it produces visibly different (equally
valid) samples, so fix it if you want a reproducible image.

---

## Rewriting short prompts

Qwen ships two fine-tuned Qwen3.5-VL 9B prompt-enhancer checkpoints that expand a
short ask into the long descriptions above — one for text-to-image, one for
edits. They are optional; sNora's own planner already roughs in the structure.
If you want full-strength rewriting locally:

- `Qwen/Qwen-Image-2.1-PE-T2I` (text-to-image)
- `Qwen/Qwen-Image-2.1-PE-I2I` (image editing)

Code lives in the model repo under `prompt_rewrite/`. The enrichments return
`{"rewritten_prompt": …, "wh_ratio": …, "ratio_follow": …}`, which maps straight
onto the `width`/`height` (or aspect) fields of the SnailStudio endpoint.

---

## Discord

The community bot (`backend/discord_bot.py`, running beside the render service)
now has a **still lane** alongside the video lane:

- A member message asking for a `picture`, `sticker`, `logo`, `poster`, `icon`,
  `meme`, etc. routes to Qwen instead of the video farm (`image_intent()`);
  asks that mention video words stay on the film lane.
- Attaching a photo with an edit verb ("change the background to…") becomes a
  **Qwen edit** — the photo is the reference and the canvas follows its aspect.
- `/image idea:… [photo] [shape] [sticker]` is the slash-command form.
- **Stickers set `transparent: true` and come back as a genuine RGBA PNG** with
  real alpha (no checkerboard), posted in the channel as an attachment.
- Prompts are written by the same snail-api planner using the Qwen prompting
  contract above, then POSTed to `{studio_url}/api/editor/ai/generate-qwen`
  (default `http://127.0.0.1:7778`) and fetched from `/generated/`.
