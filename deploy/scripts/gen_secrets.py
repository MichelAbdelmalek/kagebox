"""Create missing Docker secret files in deploy/secrets/. Existing files are never overwritten.

Run inside a container (see `make secrets`); the directory is gitignored.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

GENERATED = (
    "db_password",
    "s3_access_key",
    "s3_secret_key",
    "session_secret",
    "media_signing_key",
    "qdrant_api_key",
)
MANUAL = ("together_api_key",)  # filled in by the user; left empty here


def main(directory: str) -> int:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    for name in GENERATED:
        p = d / f"{name}.txt"
        if not p.exists():
            p.write_text(secrets.token_hex(32))
            os.chmod(p, 0o600)
            print(f"created {p.name}")
    for name in MANUAL:
        p = d / f"{name}.txt"
        if not p.exists():
            p.touch(mode=0o600)
            print(f"created empty {p.name} (paste your key into it)")
    cfg = d / "s3_config.json"
    if not cfg.exists():
        ak = (d / "s3_access_key.txt").read_text().strip()
        sk = (d / "s3_secret_key.txt").read_text().strip()
        cfg.write_text(
            json.dumps(
                {
                    "identities": [
                        {
                            "name": "kagebox",
                            "credentials": [{"accessKey": ak, "secretKey": sk}],
                            "actions": ["Read", "Write", "List", "Tagging", "Admin"],
                        }
                    ]
                },
                indent=2,
            )
        )
        os.chmod(cfg, 0o600)
        print(f"created {cfg.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "deploy/secrets"))
