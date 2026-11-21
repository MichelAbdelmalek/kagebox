"""Generate the `models:` section of configs/models.yaml from configs/models.sources.yaml.

For every approved source, list the repo at its pinned commit through the Hugging Face API and
record each allowed file's sha256 and size. Weight files are Git LFS objects whose sha256 the API
reports. Small non-LFS files (JSON, vocabularies) report only a git blob id, so they are fetched
at the pinned commit and hashed here. Run inside a container: `make pin-models`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
import yaml

HF = "https://huggingface.co"
FORMATS = {
    ".safetensors": "safetensors",
    ".json": "json",
    ".txt": "text",
    ".jinja": "text",
    ".model": "sentencepiece",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def tree(client: httpx.Client, repo: str, rev: str) -> list[dict[str, Any]]:
    r = client.get(f"{HF}/api/models/{repo}/tree/{rev}", params={"recursive": "true"})
    r.raise_for_status()
    items: list[dict[str, Any]] = r.json()
    return [i for i in items if i.get("type") == "file"]


def pin(client: httpx.Client, name: str, src: dict[str, Any]) -> dict[str, Any]:
    repo, rev = src["repo"], src["revision"]
    files = []
    for item in sorted(tree(client, repo, rev), key=lambda i: i["path"]):
        path = item["path"]
        if not any(fnmatch.fnmatch(path, pat) for pat in src["allow"]):
            continue
        fmt = FORMATS.get(PurePosixPath(path).suffix.lower())
        if fmt is None:
            raise SystemExit(f"{name}: {path} matched allow but has no permitted format")
        lfs = item.get("lfs")
        if lfs:
            sha, size = lfs["oid"], int(lfs["size"])
        else:
            r = client.get(f"{HF}/{repo}/resolve/{rev}/{quote(path)}", follow_redirects=True)
            r.raise_for_status()
            sha, size = hashlib.sha256(r.content).hexdigest(), len(r.content)
        files.append({"path": path, "sha256": sha, "size_bytes": size, "format": fmt})
    missing = [p for p in src["allow"] if not any(fnmatch.fnmatch(f["path"], p) for f in files)]
    if missing:
        raise SystemExit(f"{name}: nothing in {repo}@{rev} matched {missing}")
    return {
        "status": "approved",
        "repo": repo,
        "revision": rev,
        "license": src["license"],
        "source": f"{HF}/{repo}/tree/{rev}",
        "files": files,
    }


def lfs_file(client: httpx.Client, repo: str, rev: str, path: str) -> dict[str, Any]:
    for item in tree(client, repo, rev):
        if item["path"] == path:
            if not item.get("lfs"):
                raise SystemExit(f"{repo}@{rev}/{path} is not an LFS file")
            return {"sha256": item["lfs"]["oid"], "size_bytes": int(item["lfs"]["size"])}
    raise SystemExit(f"{repo}@{rev}: {path} not found")


def carry_outputs(new: dict[str, Any], prior: dict[str, Any] | None) -> dict[str, str | None]:
    """Output hashes for every step. A previously pinned hash is kept only when the revision and
    every source hash are unchanged; otherwise the output must be re-converted and re-reviewed."""
    given = new.get("outputs") or {}
    out: dict[str, str | None] = {s["dst"]: given.get(s["dst"]) for s in new["steps"]}
    if (
        prior
        and prior.get("revision") == new["revision"]
        and (
            {s["path"]: s["sha256"] for s in prior.get("sources", [])}
            == {s["path"]: s["sha256"] for s in new["sources"]}
        )
    ):
        for dst in out:
            out[dst] = out[dst] or (prior.get("outputs") or {}).get(dst)
    return out


def pin_conversion(client: httpx.Client, name: str, src: dict[str, Any]) -> dict[str, Any]:
    repo, rev, conv = src["repo"], src["revision"], src["convert"]
    data = pin(client, name, src)["files"] if src.get("allow") else []
    out: dict[str, Any] = {
        "status": "approved",
        "repo": repo,
        "revision": rev,
        "license": src["license"],
        "source": f"{HF}/{repo}/tree/{rev}",
        "sources": [
            {"path": p, **lfs_file(client, repo, rev, p), "format": "torch-pickle"}
            for p in conv["sources"]
        ],
        "steps": conv["steps"],
        "data_files": data,
        "outputs": conv.get("outputs") or {s["dst"]: None for s in conv["steps"]},
    }
    if ref := conv.get("reference"):
        out["reference"] = {
            "step_dst": ref["step_dst"],
            "repo": ref["repo"],
            "revision": ref["revision"],
            "path": ref["path"],
            **lfs_file(client, ref["repo"], ref["revision"], ref["path"]),
        }
    if conv.get("tied"):
        out["tied"] = conv["tied"]
    if conv.get("safe_globals"):
        out["safe_globals"] = conv["safe_globals"]
    return out


def main(config_dir: str) -> int:
    cfg = Path(config_dir)
    sources = yaml.safe_load((cfg / "models.sources.yaml").read_text()) or {}
    models_path = cfg / "models.yaml"
    current = yaml.safe_load(models_path.read_text()) or {}
    with httpx.Client(timeout=60, headers={"user-agent": "kagebox-pin-models"}) as client:
        models = {n: pin(client, n, s) for n, s in sources.items() if "convert" not in s}
        conversions = {
            n: pin_conversion(client, n, s) for n, s in sources.items() if "convert" in s
        }
        for n, c in conversions.items():
            c["outputs"] = carry_outputs(c, (current.get("conversions") or {}).get(n))
    for name, m in models.items():
        print(
            f"{name}: {len(m['files'])} file(s), "
            f"{sum(f['size_bytes'] for f in m['files']) / 1e6:.0f} MB"
        )
    current["models"] = models
    current["conversions"] = conversions
    for n, c in conversions.items():
        print(f"{n}: convert {len(c['sources'])} source(s), {len(c['data_files'])} data file(s)")
    header = models_path.read_text().split("\nversion:", 1)[0]
    body = yaml.safe_dump(current, sort_keys=False, width=100)
    models_path.write_text(header.rstrip() + "\n\n" + body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "configs"))
