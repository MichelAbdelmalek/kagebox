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
| Scene understanding | Local, `worker-cpu`: Qwen3-VL-2B-Instruct, Qwen3.5-2B, Qwen3.5-0.8B (all Apache-2.0); the earlier Together candidates (Qwen3.5-9B, Nemotron-3.5-Super-VL) are dropped | Bake-off in 1.2, then pin | Decision (2026-11-21): local VLM. Pages never leave the machine, so rights records need no cloud consent for this stage. Weights must be safetensors, pinned by revision and sha256 like every other model. |
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

## Decision (2026-11-21): local VLM

Scene understanding runs a small open-weight vision model locally on `worker-cpu` instead of Together. Reasons: no page image leaves the machine (no cloud-processing consent needed for this stage), no per-token cost, and the loader already enforces safetensors, pinned revisions and checksums. Candidates are Qwen3-VL-2B-Instruct, Qwen3.5-2B and Qwen3.5-0.8B (all Apache-2.0), run in bf16 on CPU (no fp32 for the 2B models). `stages.understand` stays `proposed` with `model: null` until the bake-off pins one. Output is validated against the strict reading schema with retries; output that still fails becomes a ReviewItem. Together remains the provider for the director stage only.

## Decision (2026-11-22): owner approved qwen3-vl-2b-instruct

`stages.understand` is now `provider: local`, `model: qwen3-vl-2b-instruct` (Qwen/Qwen3-VL-2B-Instruct, revision `89644892e4d85e24eaac8bacfd4f463576704203`, Apache-2.0), `status: approved`, with no fallback for now. `qwen3-5-2b` and `qwen3-5-0-8b` stay pinned in `models:` and on disk.

Evidence: the real bake-off (15 panels per run: 10 from one chapter plus 5 synthetic; report `data/reports/understand-bakeoff/20261121T200218Z/`, gitignored; prompt v1). Rates come from a small sample and are indicative only.

| run | schema-valid | null rate (setting / action / time / weather / shot_hint) | text-evidence claims, unsupported quotes | s/panel p50 / p95 | peak RSS |
|---|---|---|---|---|---|
| qwen3-vl-2b-instruct bf16 | 15/15 (1.00) | 0 / 0 / 0.87 / 0.87 / 0 | 3, 1 (33%) | 153 / 322 | 5.8 GB |
| qwen3-5-2b bf16 | 12/15 (0.80) | 0 / 0 / 0.83 / 0.92 / 0 | 1, 0 | 152 / 546 | 6.1 GB |
| qwen3-5-0-8b bf16 | 15/15 (1.00) | 0 / 0 / 0.13 / 0.87 / 0 | 4, 3 (75%) | 59 / 110 | 3.2 GB |
| qwen3-5-0-8b fp32 | 15/15 (1.00) | 0 / 0 / 0.13 / 0.87 / 0 | 3, 2 (67%) | 75 / 120 | 6.4 GB |

Why this model: 100% schema-valid, descriptions stay on what is drawn, it nulls time of day and weather when unsure, and it did not copy the prompt example (the other two did). The 0.8B is 2.6x faster but invents content and cites text it does not have.

Consequences of the bake-off, decided with this approval:
- Every model set `weather` to "storm" from an instruction inside a speech bubble, with a quote that really was in the OCR text. Ruling R3: scene fields describe what is drawn, so a claim with `text` evidence is nulled and raised as `scene_text_claim`; the quote check is removed. Dialogue stays stored as OCR bubbles.
- Ruling R5: prompt v2 (`configs/prompts/understand.v2.md`) replaces the concrete example with placeholders; v1 stays on disk as the prompt of the recorded run.
- Still open: throughput. At p50 of 153 s a 238-panel chapter projects to about 10 h against the 7 h cap (`max_minutes_per_chapter: 420`); thread count, `max_pixels` and the cap need a measurement in the next stage.

## Decision (2026-11-23): the two other candidates are de-approved (ruling R7)

`qwen3-5-2b` and `qwen3-5-0-8b` are `status: proposed` in `configs/models.sources.yaml` and `configs/models.yaml`. Their files stay on the volume, but `models-fetch` skips them and `model_dir` refuses them (`ModelNotApproved`); `check_models` no longer loads them. Both are **not selected: evidence**:
- qwen3-5-0-8b: nearly empty answers under prompt v3 (action and shot_hint null on every panel, one-word settings), still follows an injected weather instruction, so it cannot feed the director.
- qwen3-5-2b: 12/15 schema-valid (0.80), parrots prompt examples, p95 546 s because of retries.
