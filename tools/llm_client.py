#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""OpenAI-compatible HTTP API client for chat completions and embeddings."""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from input_identity import validate_base_url

DEFAULT_CHAT_TEMPERATURE = 0.0
DEFAULT_CHAT_SEED = 1


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


class LLMClient:
    """OpenAI-compatible HTTP API client for chat completions and embeddings."""

    def __init__(self, base_url: str, timeout: float = 300.0, api_key: str | None = None) -> None:
        """Store a validated endpoint and request settings."""
        self.base_url = validate_base_url(base_url).rstrip("/")
        self.timeout = timeout
        self.api_key = api_key

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Performs a POST request with JSON payload to {base_url}{path}."""
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
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
        body = {"model": model, "messages": messages, **settings}
        reply = self._post("/chat/completions", body)
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
        body = {"model": model, "input": texts}
        reply = self._post("/embeddings", body)
        if not isinstance(reply, dict):
            raise ValueError(f"expected JSON object reply, got {type(reply).__name__}")
        data = reply.get("data")
        if not isinstance(data, list):
            raise ValueError("reply has no data list")
        if len(data) != len(texts):
            raise ValueError(f"expected {len(texts)} embeddings, got {len(data)}")
        try:
            sorted_data = sorted(data, key=lambda d: d["index"])
            return [d["embedding"] for d in sorted_data]
        except (KeyError, TypeError) as err:
            raise ValueError(f"malformed embedding item in reply: {err}") from err


def from_env(timeout: float = 300.0) -> LLMClient:
    """Creates an LLMClient from environment variables."""
    base_url = os.environ.get("PERITIA_LLM_BASE_URL")
    if not base_url:
        raise SystemExit("PERITIA_LLM_BASE_URL environment variable is required")
    api_key = os.environ.get("PERITIA_LLM_API_KEY")
    return LLMClient(base_url=base_url, timeout=timeout, api_key=api_key)
