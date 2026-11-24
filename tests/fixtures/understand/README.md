Replay fixtures for the understand stage: raw model text for SYNTHETIC panels only (drawn in code by
`orchestration/understand_synthetic.py`: speech, blank, night; never chapter panels). Re-recorded
2026-11-23 (milestone 1.2, Task 9) with qwen3-vl-2b-instruct (bf16), prompt v4 and the settings of
`configs/understand.yaml` (text-mask margin 4, mask threshold 0.35), each request being the
text-masked image (stored boxes plus the REAL bubble detector on the crop) and no OCR text.

Each fixture also carries `recorded_with` (mask margin, mask threshold, prompt version): metadata only, pinned by `tests/unit/test_understand_fixtures.py` so that changing a setting without re-recording fails a test. `record_case(..., recorded_with=...)` writes it. (Added by hand to the 2026-11-23 recordings with the values stated above; the recordings themselves were not redone.)

Which synthetic case maps to which fixture, as recorded with the real detector:

- `speech_injected` masks to the same image as `speech` (the injected sentence and the balloon are
  both covered), so `speech.json` serves both.
- `screentone` (a regular dot pattern) is masked over the WHOLE panel by the detector, so its sent
  image is byte-identical to `blank`, and `blank.json` serves both. That is a detector finding
  (a dense dot pattern reads as text), not a recording gap.
- `night`: the detector also boxes the crescent moon (a false positive over art; the stars and
  the dark sky are kept).

The regression tests (`tests/unit/test_understand_replay.py`) run the flow with a fake detector,
so their request bytes differ from the recorded ones; they take the recorded RAW MODEL OUTPUTS and
re-key them to their own requests at load time. The recorded outputs are therefore what is
tested, not the recorded keys. `test_recorded_fixtures_are_current_for_the_prompt` fails when the
prompt changes, which is the signal to re-record.

Re-record (needs the model volume; run when the Mac is quiet, load below 6):
`sh deploy/scripts/bakeoff.sh --record /reports/fx --models qwen3-vl-2b-instruct --dtypes bfloat16 --synthetic-cases speech,speech_injected,blank,screentone,night`
(the service root is read-only; `/reports` is `data/reports`), read the JSON, then copy
`data/reports/fx/*.json` here.
