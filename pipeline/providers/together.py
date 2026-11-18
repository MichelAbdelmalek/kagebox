"""Together AI backend (OpenAI-compatible chat completions with JSON-schema output)."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx
from pydantic import ValidationError

from pipeline.providers.base import M, ModelSpec, StructuredOutputFailed, fence_untrusted

log = logging.getLogger(__name__)

TOGETHER_BASE_URL = "https://api.together.xyz/v1"
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class TogetherProvider:
    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        backoff_s: float = 1.0,
    ) -> None:
        if not api_key:
            raise ValueError("Together API key is empty")
        self._client = client or httpx.Client(
            base_url=TOGETHER_BASE_URL, timeout=httpx.Timeout(120.0, connect=10.0)
        )
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._max_attempts = max_attempts
        self._backoff_s = backoff_s

    def llm_structured(
        self, spec: ModelSpec, instructions: str, content: str, schema: type[M]
    ) -> M:
        return self._structured(
            spec, instructions, [{"type": "text", "text": fence_untrusted(content)}], schema
        )

    def vlm_structured(
        self, spec: ModelSpec, instructions: str, content: str, images: list[bytes], schema: type[M]
    ) -> M:
        parts: list[dict[str, Any]] = [{"type": "text", "text": fence_untrusted(content)}]
        for img in images:
            b64 = base64.b64encode(img).decode()
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            )
        return self._structured(spec, instructions, parts, schema)

    def _structured(
        self, spec: ModelSpec, instructions: str, parts: list[dict[str, Any]], schema: type[M]
    ) -> M:
        if spec.provider != "together":
            raise ValueError(f"{spec.model} is not a Together model")
        body = {
            "model": spec.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": parts},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            },
        }
        last_error = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                r = self._client.post("/chat/completions", json=body, headers=self._headers)
            except httpx.TransportError as exc:
                last_error = type(exc).__name__
            else:
                if r.status_code == 200:
                    try:
                        text = r.json()["choices"][0]["message"]["content"]
                        return schema.model_validate_json(text)
                    except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
                        # Log the error class only: model output may contain source text.
                        last_error = f"invalid output ({type(exc).__name__})"
                elif r.status_code in RETRYABLE_STATUS:
                    last_error = f"HTTP {r.status_code}"
                else:
                    raise StructuredOutputFailed(spec, attempt, f"HTTP {r.status_code}")
            log.warning(
                "together %s attempt %d/%d failed: %s",
                spec.model,
                attempt,
                self._max_attempts,
                last_error,
            )
            if attempt < self._max_attempts:
                time.sleep(self._backoff_s * 2 ** (attempt - 1))
        raise StructuredOutputFailed(spec, self._max_attempts, last_error)
