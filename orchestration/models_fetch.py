"""One-shot job: download pinned .safetensors weights listed in configs/models.yaml.

Phase 0 has no approved local models, so this only validates the config. Phase 1 adds the
download (Hugging Face, pinned commit) followed by registry.verify_weight_file on every file.
"""

from __future__ import annotations

from pipeline.providers.registry import ALLOWED_WEIGHT_SUFFIXES, load_yaml


def main() -> int:
    models = load_yaml("models.yaml").get("models") or {}
    for name, entry in models.items():
        for f in entry.get("files", []):
            if not any(f["path"].endswith(s) for s in ALLOWED_WEIGHT_SUFFIXES):
                raise SystemExit(f"{name}: {f['path']} is not a .safetensors file")
            if len(f.get("sha256", "")) != 64:
                raise SystemExit(f"{name}: {f['path']} has no sha256")
    print(f"{len(models)} local model(s) configured; nothing to download in Phase 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
