#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Unit tests for the shared OpenAI-compatible LLM client and its callers."""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import classify  # noqa: E402
import judge_groups  # noqa: E402
from llm_client import LLMClient, from_env  # noqa: E402


class StubHandler(http.server.BaseHTTPRequestHandler):
    """Stub HTTP request handler for OpenAI-compatible completions and embeddings."""

    def log_message(self, format: str, *args: Any) -> None:
        """Silences HTTP request logging during test runs."""
        pass

    def do_POST(self) -> None:
        """Handles POST requests and dispatches to server.custom_handler or returns default."""
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""
        body = json.loads(raw_body) if raw_body else {}
        self.server.requests.append({  # type: ignore[attr-defined]
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        handler = getattr(self.server, "custom_handler", None)
        if handler:
            status, reply = handler(self.path, body, self.headers)
        elif self.path == "/v1/chat/completions":
            status, reply = 200, {"choices": [{"message": {"content": "ok"}}]}
        elif self.path == "/v1/embeddings":
            status, reply = 200, {"data": []}
        else:
            status, reply = 404, {"error": "not found"}

        data = json.dumps(reply).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


class LLMTests(unittest.TestCase):
    """Tests for LLMClient and its integration with classify and judge_groups."""

    def setUp(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        self.server.requests = []  # type: ignore[attr-defined]
        self.server.custom_handler = None  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self.client = LLMClient(self.base_url, timeout=5.0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)

    def test_positive_schema_request(self) -> None:
        """Schema request carries response_format json_schema, strict false; reply content parsed."""
        schema = {"type": "object", "properties": {"answer": {"type": "number"}}}
        self.server.custom_handler = lambda path, body, headers: (  # type: ignore[attr-defined]
            200, {"choices": [{"message": {"content": json.dumps({"answer": 42})}}]}
        )
        result = self.client.chat_json(
            model="test-model",
            messages=[{"role": "user", "content": "hello"}],
            schema=schema,
            schema_name="math_answer",
        )
        self.assertEqual(result, {"answer": 42})
        req = self.server.requests[-1]  # type: ignore[attr-defined]
        self.assertEqual(req["path"], "/v1/chat/completions")
        body = req["body"]
        self.assertEqual(body["model"], "test-model")
        self.assertFalse(body["stream"])
        rf = body.get("response_format")
        self.assertIsNotNone(rf)
        self.assertEqual(rf["type"], "json_schema")
        self.assertFalse(rf["json_schema"]["strict"])
        self.assertEqual(rf["json_schema"]["name"], "math_answer")
        self.assertEqual(rf["json_schema"]["schema"], schema)

    def test_negative_planted_malformed_answer_not_json_classify(self) -> None:
        """Stub returns content 'not json'; classify_one returns None."""
        self.server.custom_handler = lambda path, body, headers: (  # type: ignore[attr-defined]
            200, {"choices": [{"message": {"content": "not json"}}]}
        )
        classifier = classify.SkillClassifier(self.client, "test-model")
        self.assertIsNone(classify.classify_one(classifier.ask, "skill body"))

    def test_negative_planted_malformed_answer_invalid_label_classify(self) -> None:
        """Stub returns label outside FUNCTIONS; classify_one returns None."""
        bad_labels = json.dumps({"functions": ["unsupported-function"], "domains": ["other"]})
        self.server.custom_handler = lambda path, body, headers: (  # type: ignore[attr-defined]
            200, {"choices": [{"message": {"content": bad_labels}}]}
        )
        classifier = classify.SkillClassifier(self.client, "test-model")
        self.assertIsNone(classify.classify_one(classifier.ask, "skill body"))

    def test_negative_planted_malformed_answer_judge_returns_zero(self) -> None:
        """Stub returns content 'not json'; judge_groups JudgeHarness.judge returns 0.0."""
        self.server.custom_handler = lambda path, body, headers: (  # type: ignore[attr-defined]
            200, {"choices": [{"message": {"content": "not json"}}]}
        )
        harness = judge_groups.JudgeHarness(self.client, "test-model", "test-judge")
        self.assertEqual(harness.judge("evaluate prompt"), 0.0)

    def test_negative_planted_malformed_score_judge_returns_zero(self) -> None:
        """Stub returns content with missing score; judge_groups JudgeHarness.judge returns 0.0."""
        self.server.custom_handler = lambda path, body, headers: (  # type: ignore[attr-defined]
            200, {"choices": [{"message": {"content": json.dumps({"no_score": 1})}}]}
        )
        harness = judge_groups.JudgeHarness(self.client, "test-model", "test-judge")
        self.assertEqual(harness.judge("evaluate prompt"), 0.0)

    def test_negative_reply_without_choices(self) -> None:
        """Reply without choices raises ValueError."""
        self.server.custom_handler = lambda path, body, headers: (  # type: ignore[attr-defined]
            200, {"choices": []}
        )
        with self.assertRaises(ValueError):
            self.client.chat("test-model", [{"role": "user", "content": "hi"}])

        self.server.custom_handler = lambda path, body, headers: (  # type: ignore[attr-defined]
            200, {"other_key": 123}
        )
        with self.assertRaises(ValueError):
            self.client.chat("test-model", [{"role": "user", "content": "hi"}])

    def test_negative_embed_count_mismatch(self) -> None:
        """Embed count mismatch raises ValueError."""
        self.server.custom_handler = lambda path, body, headers: (  # type: ignore[attr-defined]
            200, {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}
        )
        with self.assertRaises(ValueError):
            self.client.embed("test-model", ["text1", "text2"])

    def test_boundary_empty_texts_to_embed(self) -> None:
        """Empty texts list to embed sends empty input and returns empty list."""
        self.server.custom_handler = lambda path, body, headers: (  # type: ignore[attr-defined]
            200, {"data": []}
        )
        res = self.client.embed("test-model", [])
        self.assertEqual(res, [])
        req = self.server.requests[-1]  # type: ignore[attr-defined]
        self.assertEqual(req["body"]["input"], [])

    def test_boundary_authorization_header(self) -> None:
        """api_key sets Authorization header; absent key sends none."""
        auth_client = LLMClient(self.base_url, timeout=5.0, api_key="test-secret")
        auth_client.chat("test-model", [{"role": "user", "content": "hi"}])
        req = self.server.requests[-1]  # type: ignore[attr-defined]
        self.assertEqual(req["headers"].get("Authorization"), "Bearer test-secret")

        no_auth_client = LLMClient(self.base_url, timeout=5.0, api_key=None)
        no_auth_client.chat("test-model", [{"role": "user", "content": "hi"}])
        req2 = self.server.requests[-1]  # type: ignore[attr-defined]
        self.assertNotIn("Authorization", req2["headers"])

    def test_boundary_from_env(self) -> None:
        """from_env without PERITIA_LLM_BASE_URL raises SystemExit; with env returns client."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                from_env()
            self.assertIn("PERITIA_LLM_BASE_URL", str(ctx.exception))

        with patch.dict(os.environ, {"PERITIA_LLM_BASE_URL": "http://127.0.0.1:1234/v1", "PERITIA_LLM_API_KEY": "key1"}):
            client = from_env()
            self.assertEqual(client.base_url, "http://127.0.0.1:1234/v1")
            self.assertEqual(client.api_key, "key1")


if __name__ == "__main__":
    unittest.main()
