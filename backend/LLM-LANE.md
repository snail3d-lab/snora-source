# The LLM lane (the "sauce")

sNora renders video on your Mac. But before a render, it rewrites your short
line into a rich scene prompt, and the "director" that plans a shot out of a
voice note needs to talk to a language model too. That's the LLM lane: any
**OpenAI-compatible** chat endpoint you point it at. It is separate from the
render farm, and it can live anywhere.

You wire it up with three keys in `sora_config.json`:

```
"snail_api_base": "http://127.0.0.1:8000/v1",
"snail_api_key":  "any-token-your-server-expects",
"snail_model":    "your-model-name"
```

Point it at a hosted API (OpenAI, Anthropic-via-openai-shim, OpenRouter, a
friend's box) and you are done. A lot of people would rather this part stay
local too, so here are two self-hosted recipes that work with this code. Both
are just a server on a `/v1/chat/completions` URL — swap in whichever one you
build.

## Why bother local?

Prompt rewriting and shot planning run on every single job. On a hosted API
that's a per-request cost and an outbound call, forever, for a product whose
whole point is that it runs on your own hardware. A local model answers for
free and the whole loop stays on your network.

## Recipe A — a Mac with spare unified memory (simplest)

If your Mac has 64–128 GB, run a 7–14B chat model beside the render farm.
Any llama.cpp, Ollama, LM Studio, or MLX server that exposes `/v1` works —
they are all OpenAI-compatible. Example with llama.cpp:

```
llama-server -hf ggml-org/gemma-3-12b-it-GGUF --port 8000
```

Then set `snail_api_base` to `http://127.0.0.1:8000/v1` and `snail_model` to
the model's name. This lane shares memory with the video model, so on smaller
boxes keep the chat model small; a 7–12B instruct model is plenty for prompt
formatting, which is a light job.

## Recipe B — NVIDIA boxes / DGX Spark cluster (fastest, uncensored-lane friendly)

For speed and an uncensored prompt lane, people run a big MoE chat model on
NVIDIA hardware and point sNora at it. Two things to know up front:

**A model that refuses your prompts is a bad fit here.** sNora already ships
its own content gate (the nanny) that you configure per-server. You do not
want a *second*, invisible filter inside the prompt-writer quietly rejecting or
sanitizing shots the nanny already cleared. Pick a model build that answers.
Abliterated / uncensored chat fine-tunes are common for exactly this; several
popular open models have well-regarded ones (search the Hugging Face model
hub for the base model you like plus "abliterated" or "uncensored").

**How many boxes, and why.** On Grace-Blackwell (DGX Spark, GB10) the host
RAM *is* GPU memory, and a big MoE only fits cleanly when its dimensions divide
evenly by your tensor-parallel width. As a rule of thumb:

- A 2-bit or heavy-quant build fits on **2** Sparks and runs fine for this.
- The full-precision build wants **4** Sparks: that's the width where every
  matrix divides with no padding, and it leaves real room for the KV cache.
  Odd widths (like 3) need head-padding hacks.

Serving stack that's proven on this class of hardware: **SGLang** (tensor
parallel across the boxes, RDMA/ConnectX interconnect, spec decode), or
vLLM. A good starting point for DeepSeek-class MoE on Spark is the community
repo `MiaAI-Lab/DeepSeek-v4.1-Flash-DGX-Sparks` (re-check it before deploying,
it moves fast). Whatever you serve, sNora only cares that it answers
`POST /v1/chat/completions`.

### Model and weight links (Hugging Face)

These are starting points, not the only options — match the model to your
VRAM and your appetite for self-hosting.

| Purpose | Model | Where |
|---|---|---|
| Uncensored prompt lane (NVIDIA/Spark) | DeepSeek V4.1-Flash (MoE, native MXFP4) | `huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash` |
| same, Spark bring-up scripts | community DGX-Spark deploy repo | `github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-DGX-Sparks` |
| Small lane on a spare Mac | Gemma 3 / Llama 3.1 instruct GGUFs | `huggingface.co/collections/ggml-org` (search `*-it-GGUF`) |

### GB10 / Spark gotchas worth pre-paying for

Written the hard way on a 2-box cluster; they apply to any box where host RAM
is the VRAM:

- **A swapfile on every node before the first model load** (~64 GB). A cluster
  load peaks above physical RAM once; without a swapfile that's an OOM, with
  one it's a slow first load.
- **Cap the NCCL connection buffers.** Connection setup pins host RAM, and on
  these boxes pinned RAM comes straight out of your model budget. Serving-stack
  defaults over-allocate; leave the reduced `NCCL_BUFFSIZE`-style settings in.
- **Never offload weights to host RAM.** On a unified-memory box that evicts
  the model you are trying to load. Offload to NVMe or not at all.
- **Exclusive GPUs.** Stop every other serving container first. And if you have
  an auto-restart / keepalive service for an older engine, disable it during a
  swap-in — a watchdog that force-restarts the *old* container mid-load will
  OOM the new one.

Then run its serve command, note the head node's `/v1` URL, and put that in
`snail_api_base`. sNora doesn't know or care what's behind it.
