# Render engines & model weights

> This file covers **video**. Making stills instead? See
> [`PHOTO-ENGINES.md`](PHOTO-ENGINES.md) for the photo engines
> (Qwen-Image-2.1, Flux) and how to prompt them.

sNora ships wired for two render engines. Both drive the same
MiniMax-H3 video model family; pick one.

## Engine 1 — Vpipe Manager (the default, and the easy one)

A macOS GUI app that manages the model weights and runs pipeline files.

1. Install the **Vpipe Manager** app (the official .dmg from the vendor).
2. Launch it once and let it download the MiniMax-H3 models — it stores them
   under `~/vpipe/models/` and the 8-bit packs under `~/vpipe/models/local/`.
3. Copy `backend/pipelines/minimax-h3-begin-end-to-video.vpipeline` into
   `~/vpipe/pipelines/` — that's the one this repo adds, and the two-photo
   morph needs it. The stock pipelines (text-to-video, image-to-video,
   reference modes) ship with Vpipe itself.
4. Point `vpipe_bin` / `vpipe_cwd` in `sora_config.json` at the install —
   the defaults are:
   `"/Applications/Vpipe Manager.app/Contents/Helpers/vpipe"` and `~/vpipe`.

That's it. The service calls the helper binary with a pipeline file; Vpipe
handles weights and memory.

## Engine 2 — ComfyUI (fallback path, works on Mac)

The service can submit a stock workflow to a local ComfyUI instead
(`"backend": "comfy"` in `sora_config.json`, default URL
`http://127.0.0.1:8188`). The workflow template lives beside the service as
`comfy_t2va_template.json`.

### Model files ComfyUI needs

Get them from the official Hugging Face repos (browse them — you want the
bf16/int8 files named below):

| ComfyUI folder | File | Source |
|---|---|---|
| `diffusion_models/` | `minimax_h3_fl2va_pruned_bf16.safetensors` (any pruned/bf16 variant) | `huggingface.co/Comfy-Org/MiniMax-H3` |
| `text_encoders/` | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` (int8 keeps it Mac-sized) | `huggingface.co/Comfy-Org/MiniMax-H3` |
| `vae/` | `minimax_h3_video_vae_fp16.safetensors` | `huggingface.co/Comfy-Org/MiniMax-H3` |
| `vae/` | `minimax_h3_audio_vae_fp32.safetensors` | `huggingface.co/Comfy-Org/MiniMax-H3` |
| `loras/` | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` (turbo LoRA; if yours is a newer version, edit the name in the template) | `huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora` |

The template references these by exact filename — if yours differ by version,
open `comfy_t2va_template.json` and fix nodes `1`, `2`, `3`, `4`, `5`.

Run ComfyUI itself however you like on the Mac (the desktop app, or
`git clone` + `python main.py`); 64 GB+ of unified memory gets you real
resolution, less of it gets you small drafts.

### Point sNora at it

```
"backend": "comfy",
"comfy_url": "http://127.0.0.1:8188",
"comfy_output": "~/ComfyUI/output"   # where SaveVideo lands
```

The service fills `__JOBID__` / `__PROMPT__` placeholders in the template,
POSTs it, polls for the finished mp4, and takes it from there.

## The memory math (why 96 GB)

The FL2VA weights run resident at roughly 60 GB in 8-bit, plus activations
during a render. That's the whole requirement story: 64 GB boxes physically
cannot hold the model, 96 GB fits with room, 128 GB is comfortable. The
ComfyUI path at bf16 wants even more, which is why the Vpipe 8-bit path is
the default on Macs.
