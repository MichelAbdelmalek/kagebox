"""Worker entry point: serve the chapter pipeline to the Prefect server on the internal network."""

from __future__ import annotations

from orchestration.flows import chapter_pipeline


def main() -> None:
    chapter_pipeline.serve(name="chapter-pipeline", limit=2)


if __name__ == "__main__":
    main()
