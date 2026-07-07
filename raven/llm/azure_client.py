from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Callable

from openai import AzureOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from raven.config import AzureOpenAIConfig


class AzureGPT4OClient:
    def __init__(self, config: AzureOpenAIConfig, log_callback: Callable[..., None] | None = None):
        self.config = config
        self.log_callback = log_callback
        self.client = AzureOpenAI(
            azure_endpoint=config.endpoint,
            api_key=config.api_key,
            api_version=config.api_version,
        )

    @retry(wait=wait_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(3))
    def complete_json(
        self,
        system: str,
        user_text: str,
        *,
        images: list[Path] | None = None,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for image in images or []:
            mime = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                }
            )

        started_at = _now()
        start = time.perf_counter()
        response = self.client.chat.completions.create(
            model=self.config.deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            response_format={"type": "json_object"},
            temperature=temperature,
        )
        raw = response.choices[0].message.content or "{}"
        self._log_call(
            name="complete_json",
            started_at=started_at,
            duration_ms=int((time.perf_counter() - start) * 1000),
            prompt_chars=len(system) + len(user_text),
            image_count=len(images or []),
            response_chars=len(raw),
            usage=response.usage,
        )
        return json.loads(raw)

    @retry(wait=wait_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(3))
    def complete_text(self, system: str, user_text: str, temperature: float = 0.1) -> str:
        started_at = _now()
        start = time.perf_counter()
        response = self.client.chat.completions.create(
            model=self.config.deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            temperature=temperature,
        )
        raw = response.choices[0].message.content or ""
        self._log_call(
            name="complete_text",
            started_at=started_at,
            duration_ms=int((time.perf_counter() - start) * 1000),
            prompt_chars=len(system) + len(user_text),
            image_count=0,
            response_chars=len(raw),
            usage=response.usage,
        )
        return raw

    def _log_call(
        self,
        *,
        name: str,
        started_at: str,
        duration_ms: int,
        prompt_chars: int,
        image_count: int,
        response_chars: int,
        usage: Any,
    ) -> None:
        if not self.log_callback:
            return
        usage_dict = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            "total_tokens": getattr(usage, "total_tokens", 0) if usage else 0,
        }
        self.log_callback(
            name=name,
            model=self.config.deployment,
            started_at=started_at,
            duration_ms=duration_ms,
            prompt_chars=prompt_chars,
            image_count=image_count,
            response_chars=response_chars,
            usage=usage_dict,
        )


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
