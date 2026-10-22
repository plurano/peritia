#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""OpenAI-compatible HTTP API client for chat completions and embeddings."""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


class LLMClient:
    """OpenAI-compatible HTTP API client for chat completions and embeddings."""

    def __init__(self, base_url: str, timeout: float = 300.0, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
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
        temperature: float = 0.0,
        seed: int = 1,
    ) -> str:
        """Calls /chat/completions and returns the choices[0].message.content string."""
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "seed": seed,
            "stream": False,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    # strict mode on OpenAI demands additionalProperties
                    # false and every property required; the label and
                    # judge schemas carry neither, so the server guides
                    # the answer and the callers refuse a malformed one.
                    "strict": False,
                    "schema": schema,
                },
            }
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
        temperature: float = 0.0,
        seed: int = 1,
    ) -> Any:
        """Calls /chat/completions with schema and returns parsed JSON content."""
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
