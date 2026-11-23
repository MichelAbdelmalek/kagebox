Replay fixtures for the understand stage: raw model text for SYNTHETIC panels only (drawn in code by
`orchestration/understand_synthetic.py`: speech, blank, night; `speech_injected` masks to the same
image as `speech`, so one fixture serves both). Never chapter panels. Recorded 2026-11-23 with
qwen3-vl-2b-instruct (bf16, prompt v4, settings of `configs/understand.yaml`), each request being
the text-masked image (stored boxes plus bubble detection on the crop) and no OCR text. Re-record:
`sh deploy/scripts/bakeoff.sh --record /reports/fx --models qwen3-vl-2b-instruct --dtypes bfloat16 --synthetic-cases speech,speech_injected,blank,night`
(the service root is read-only; `/reports` is `data/reports`), read the JSON, then copy
`data/reports/fx/*.json` here.
