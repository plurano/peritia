#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""OpenAI-compatible HTTP API client for chat completions and embeddings."""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any, cast

import httpx2
import openai

from input_identity import provider_id, validate_base_url

DEFAULT_CHAT_TEMPERATURE = 0.0
DEFAULT_CHAT_SEED = 1
_PLACEHOLDER_API_KEY = "peritia-keyless-placeholder"
_ALLOWED_HEADERS = frozenset({
    "accept",
    "accept-encoding",
    "connection",
    "content-length",
    "content-type",
    "host",
    "user-agent",
})


def _is_allowed_header(name: str) -> bool:
    """Allow only transport framing and SDK telemetry headers."""
    lower = name.lower()
    return lower in _ALLOWED_HEADERS or lower.startswith("x-stainless-")


def _generation_parameters(temperature: float | None, seed: int | None) -> dict[str, Any]:
    """Resolve the same generation defaults for structured and plain chat."""
    return {
        "temperature": DEFAULT_CHAT_TEMPERATURE if temperature is None else temperature,
        "seed": DEFAULT_CHAT_SEED if seed is None else seed,
        "stream": False,
    }


def structured_request_parameters(
    schema: dict[str, Any], schema_name: str, *, temperature: float | None = None, seed: int | None = None,
) -> dict[str, Any]:
    """Return the effective structured generation settings used by transport and resume keys."""
    return {
        **_generation_parameters(temperature, seed),
        "response_format": {
            "type": "json_schema",
            # The server guides output; callers check the returned structure.
            "json_schema": {"name": schema_name, "strict": False, "schema": schema},
        },
    }


def _embedding_index(item: Any) -> int:
    """Extract an integer embedding index, rejecting Booleans and non-objects."""
    if not isinstance(item, dict):
        raise TypeError("item is not an object")
    index = item["index"]
    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("index is not an integer")
    return index


class LLMClient:
    """OpenAI-compatible HTTP API client for chat completions and embeddings."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 300.0,
        api_key: str | None = None,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        """Store a validated endpoint and configure an isolated OpenAI SDK client."""
        self.base_url = validate_base_url(base_url).rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self._http_client = httpx2.Client(
            event_hooks={"request": [self._sanitize_request]},
            timeout=timeout,
            transport=transport,
        )
        self._client = openai.OpenAI(
            base_url=self.base_url,
            api_key=api_key or _PLACEHOLDER_API_KEY,
            max_retries=0,
            timeout=timeout,
            http_client=self._http_client,
        )

    def close(self) -> None:
        """Close the underlying HTTP transport client."""
        self._http_client.close()

    def _sanitize_request(self, request: httpx2.Request) -> None:
        """Strip ambient environment headers and set explicit bearer auth when configured."""
        for name in list(request.headers.keys()):
            if not _is_allowed_header(name):
                del request.headers[name]
        if self.api_key:
            request.headers["Authorization"] = f"Bearer {self.api_key}"

    def _request_json(self, path: str, send: Callable[[], Any]) -> Any:
        """Decode a POST reply and keep transport diagnostics free of userinfo and secrets."""
        try:
            response = send()
            raw = response.http_response.read().decode("utf-8")
        except openai.APIStatusError as err:
            code = err.status_code
            status = f" (HTTP {code})" if isinstance(code, int) and not isinstance(code, bool) else ""
            raise OSError(f"LLM request failed{status} for {provider_id(self.base_url)}{path}") from None
        except (openai.OpenAIError, httpx2.HTTPError, OSError, UnicodeDecodeError):
            raise OSError(f"LLM request failed for {provider_id(self.base_url)}{path}") from None
        return json.loads(raw)

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        schema_name: str = "answer",
        temperature: float | None = None,
        seed: int | None = None,
    ) -> str:
        """Call /chat/completions and return the first choice's message content."""
        if schema is None:
            settings = _generation_parameters(temperature, seed)
        else:
            settings = structured_request_parameters(schema, schema_name, temperature=temperature, seed=seed)
        reply = self._request_json(
            "/chat/completions",
            lambda: self._client.chat.completions.with_raw_response.create(
                model=model,
                messages=cast(Any, messages),
                timeout=self.timeout,
                **settings,
            ),
        )
        if not isinstance(reply, dict):
            raise ValueError(f"expected JSON object reply, got {type(reply).__name__}")
        choices = reply.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("reply has no choices")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise ValueError("choice is not an object")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("choice message is not an object")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("message has no content string")
        return content

    def chat_json(
        self,
        model: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        schema_name: str = "answer",
        temperature: float | None = None,
        seed: int | None = None,
    ) -> Any:
        """Call /chat/completions with a schema and return parsed JSON content."""
        content = self.chat(
            model=model,
            messages=messages,
            schema=schema,
            schema_name=schema_name,
            temperature=temperature,
            seed=seed,
        )
        return json.loads(content)

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """Calls /embeddings and returns embeddings ordered by index."""
        reply = self._request_json(
            "/embeddings",
            lambda: self._client.embeddings.with_raw_response.create(
                model=model,
                input=texts,
                extra_body={"encoding_format": openai.omit},
                timeout=self.timeout,
            ),
        )
        if not isinstance(reply, dict):
            raise ValueError(f"expected JSON object reply, got {type(reply).__name__}")
        data = reply.get("data")
        if not isinstance(data, list):
            raise ValueError("reply has no data list")
        if len(data) != len(texts):
            raise ValueError(f"expected {len(texts)} embeddings, got {len(data)}")
        try:
            sorted_data = sorted(data, key=_embedding_index)
            indices = [d["index"] for d in sorted_data]
            vectors = [d["embedding"] for d in sorted_data]
        except (KeyError, TypeError) as err:
            raise ValueError(f"malformed embedding item in reply: {err}") from err
        if indices != list(range(len(texts))):
            raise ValueError(f"expected embedding indices 0..{len(texts) - 1}, got {indices}")
        return vectors


def from_env(timeout: float = 300.0) -> LLMClient:
    """Creates an LLMClient from environment variables."""
    base_url = os.environ.get("PERITIA_LLM_BASE_URL")
    if not base_url:
        raise SystemExit("PERITIA_LLM_BASE_URL environment variable is required")
    api_key = os.environ.get("PERITIA_LLM_API_KEY")
    return LLMClient(base_url=base_url, timeout=timeout, api_key=api_key)
