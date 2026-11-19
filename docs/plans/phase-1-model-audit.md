# Phase 1 model and license audit (milestone 1.0)

Date: 2026-11-19. Method: one research agent per candidate, two independent skeptics per candidate (one on licensing, one on technical facts), then a completeness critic. 46 agents ran. All facts come from primary sources (Hugging Face API and model cards, GitHub, PyPI, Together docs). Nothing was downloaded or installed. I re-checked the decisive claims myself: Magi v2's licence text, manga-ocr's base-model licence, and two weight hashes.

## Verdicts

| Stage | Candidate | Verdict | Why |
|---|---|---|---|
| Ingest: panels, bubbles, speakers | **Magi v2** | **Blocked** | Weights limited to "personal, research, non-commercial, and not-for-profit" use (GitHub readme says academic only). Code has no licence (all rights reserved). Pickle-only weights. Needs `trust_remote_code`, which fetches unpinned Hub assets. Its OCR is English-only anyway. **Default D3 is withdrawn.** |
| Ingest: bubbles / text boxes | NorwayFish/manga-bubble-detector @ `bcf342f2` | Use, with legal note | Apache-2.0 tag (no LICENSE file). RT-DETRv2, native in transformers (no remote code). `model.safetensors`. Training data (about 11k comic pages) is undisclosed. |
| Ingest: panels | OpenCV gutter/contour detection + our own reading order | Use | No weights. Fits the regular grid of the sample chapter. |
| Ingest: speaker linking | Bubble-tail direction + nearest character box | Use | Deterministic. Low-confidence links go to review. |
| OCR Japanese | manga-ocr @ `aa6573bd` | Use, after conversion | **Effective licence CC-BY-SA-4.0** (decoder starts from cl-tohoku/bert-base-japanese-char-v2), not the Apache-2.0 on its card. Main ships only `pytorch_model.bin` (pickle). Convert it ourselves in a sandbox, and cross-check against the unmerged safetensors PR (`refs/pr/4`, sha256 `218bc3b6…`). |
| OCR Korean / English | PaddlePaddle PP-OCRv5 mobile det + ko/en rec, **safetensors** repos | Use | Apache-2.0. Runs natively in transformers, so no format exception is needed. PaddleOCR itself has no Linux arm64 wheel. RapidOCR (ONNX) is the fallback, and would need a format exception. |
| Character embeddings | google/siglip-so400m-patch14-384 @ `9fdffc58` | Use | Apache-2.0. safetensors only. |
| Layer masks | facebook/sam2.1-hiera-small @ `ee5bba1d` | Use | Apache-2.0. Fetch `model.safetensors` only, never the `.pt` beside it. base-plus is the gpu-profile option. |
| Depth order | depth-anything/Depth-Anything-V2-Small-hf @ `5426e4f0` | Use | Apache-2.0. Base, Large and Giant are CC-BY-NC, so excluded. |
| Inpainting | Big-LaMa, safetensors copy michaelgold/big-lama @ `98ab7cfa` | Use, with checks | Upstream is Apache-2.0; the copy's "mit" tag is invalid, so we treat it as Apache-2.0. It is a third-party conversion, so check it against our own sandboxed conversion of the official checkpoint. Places365 training-data terms are an open legal question. Fallback: OpenCV `cv2.inpaint` (no weights). |
| TTS | FunAudioLLM/CosyVoice2-0.5B @ `eec1ae6c` | Use, after conversion + exception | Apache-2.0 (card tag only; the readme's "academic purposes" disclaimer is read as covering demo content, recorded as an ambiguity). `llm.pt`, `flow.pt`, `hift.pt` are pickle → convert. `campplus.onnx` and `speech_tokenizer_v2.onnx` have no safetensors version → ONNX exception. Its HyperPyYAML config can instantiate arbitrary callables → vendor and review it as code. Upstream pins torch 2.3.1, which has a known bypass of `torch.load(weights_only=True)` → never ship torch < 2.6. |
| TTS fallback | ResembleAI/chatterbox | Deferred | MIT, but its Japanese path hard-depends on pykakasi (GPL-3.0+), and its built-in Perth watermark loads a pickle. Not needed while CosyVoice2 covers JA + EN. |
| Watermark | facebook/audioseal @ `3c19eba5` | Use, after conversion | MIT (weights included). All weights are `.pth` pickles, and the library loads them with `weights_only=False` → convert and bypass its loader. |
| Speech check | openai/whisper-large-v3-turbo safetensors via transformers | Use | MIT. No format exception. Slower on CPU than faster-whisper, whose CTranslate2 `model.bin` is not a pickle but would need an exception. |
| Score draft | ACE-Step 1.5 | Deferred to the gpu profile | MIT, but its loader hard-codes `trust_remote_code` and ships a small pickle. Per D2 the cloud profile skips the score, so this audit happens when a GPU host exists. |
| Scene understanding | Together: Qwen/Qwen3.5-9B (Apache-2.0) vs nvidia/NVIDIA-Nemotron-3.5-Super-VL (OpenMDW-1.1) | Bake-off in 1.2 | Both serve JSON-schema output. Qwen is cheaper per output token and plainly licensed. OpenMDW's licence ends if you sue over patents *or copyright*. |
| Director | Together: openai/gpt-oss-120b (Apache-2.0) | Use | Structured outputs supported. Fallback: deepseek-ai/DeepSeek-V4.1-Flash (MIT). |
| Sample chapter | Planet Comics #41, "The Lost World" (Fiction House, on sale 1945-12-28); Comic Book Plus dlid 69256 | Use, pending a manual check | US public domain by non-renewal. Before use: check the 1972–73 Catalog of Copyright Entries renewal volumes directly, then download (24 MB zip) and pin its sha256. Public domain in the US only. |

## Policy consequences

1. **Offline conversion job.** Five models ship pickle weights. Proposal: a one-shot `models-convert` job in a throwaway container with no network, read-only input and a non-root user. It uses torch ≥ 2.6, `weights_only=True` with the smallest safe-globals list (never `weights_only=False`), and writes `.safetensors` plus reviewed JSON configs. Source and output hashes go into `configs/models.yaml`, and a golden-output test checks equivalence. A CI rule bans `torch.load` and `torch.jit.load` under `pipeline/`.
2. **ONNX exception, CosyVoice2 only:** `campplus.onnx` and `speech_tokenizer_v2.onnx`. Pinned onnxruntime, no custom ops, external-data paths must resolve inside the model directory.
3. **Separate TTS image.** Engines need conflicting torch and transformers versions, so TTS gets its own `worker-tts` image and lockfile. Vision, OCR and validation share `worker-cpu`.
4. **Configs that act as code** (CosyVoice HyperPyYAML, AudioSeal OmegaConf) are vendored and reviewed, never loaded from a snapshot. `models-fetch` uses explicit file allowlists, so remote `.py` files are never fetched.
5. **No network at inference.** Several libraries download on first use (manga-ocr, CosyVoice, RapidOCR, AudioSeal). Workers run with HF offline mode, and a CI test asserts that inference makes no outbound connections.
6. **Python 3.11.** Several latest releases (numpy 2.5, scipy 1.18) need Python ≥ 3.12, so locks resolve slightly older versions. We stay on 3.11 per the brief.
7. **FFmpeg.** Debian's FFmpeg is a GPL build. That is fine for running the service. It matters only if images are ever distributed.

## Open legal items (not blockers for an internal Phase 1)

Training-data terms for Places365 (LaMa), Manga109-s (manga-ocr), the NorwayFish pages, and the CosyVoice and AudioSeal data. The CC-BY-SA attribution for manga-ocr. Together's terms and zero data retention: you must opt in, and it applies only from that point.

## Decisions (2026-11-19)

The owner delegated these to engineering and noted the project is **not commercial yet**. The choices still assume it may become commercial, so nothing has to be redone later.

1. **Ingest: permissive mix.** OpenCV panels + our own reading order, NorwayFish bubble detector, and a tail/nearest-character speaker heuristic. Magi v2's non-commercial terms would allow use today, but its unlicensed code, pickle-only weights and remote-code requirement fail the security rules regardless, and adopting it now would mean replacing it at the first commercial step.
2. **Pickle weights: convert offline.** A network-less `models-convert` job (torch ≥ 2.6, `weights_only=True`) writes safetensors with equivalence tests. Separate `worker-tts` image. ONNX exception only for CosyVoice2's `campplus.onnx` and `speech_tokenizer_v2.onnx`.
3. **Whisper: transformers + safetensors** (openai/whisper-large-v3-turbo). No format exception. The CPU speed cost is acceptable for one chapter.
4. **Sample chapter:** the owner will supply a chapter and an anime sample. This waits on their rights status (see the conversation). Planet Comics #41 remains the public-domain fallback.

## Not yet measured

CPU time and memory per model on arm64, and the combined RAM of all models against Docker Desktop's VM limit. Whether CosyVoice2 works on torch ≥ 2.6. Watermark survival after mixing with the score and AAC encoding. These are covered by the 1.1–1.7 milestone tests.
