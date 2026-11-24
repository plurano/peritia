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
import traceback
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx2

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import classify  # noqa: E402
import judge_groups  # noqa: E402
from llm_client import LLMClient, from_env  # noqa: E402

_AMBIENT_OPENAI_ENV = {
    "OPENAI_API_KEY": "fixture-env-api-key",
    "OPENAI_ORG_ID": "org-ambient",
    "OPENAI_PROJECT_ID": "proj-ambient",
    "OPENAI_BASE_URL": "https://ambient.invalid/v1",
    "OPENAI_ADMIN_KEY": "fixture-env-admin-key",
    "OPENAI_CUSTOM_HEADERS": "X-Leak: 1\nAuthorization: Bearer sk-ambient",
}
_EXPECTED_HEADER_NAMES = frozenset({
    "accept",
    "accept-encoding",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "host",
    "user-agent",
})


def _recording_transport(
    seen: list[httpx2.Request],
    status: int = 200,
    chat_reply: Any = None,
    embed_reply: Any = None,
    headers: dict[str, str] | None = None,
) -> httpx2.MockTransport:
    """Record outgoing httpx2 requests and return canned JSON replies."""
    effective_chat = {"choices": [{"message": {"content": "ok"}}]} if chat_reply is None else chat_reply
    effective_embed = {"data": []} if embed_reply is None else embed_reply

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        payload = effective_embed if request.url.path.endswith("/embeddings") else effective_chat
        return httpx2.Response(status, json=payload, headers=headers)

    return httpx2.MockTransport(handler)


def _raising_transport(exc: Exception, counter: list[int] | None = None) -> httpx2.MockTransport:
    """Raise a transport exception on each request and optionally count calls."""
    def handler(_request: httpx2.Request) -> httpx2.Response:
        if counter is not None:
            counter.append(1)
        raise exc

    return httpx2.MockTransport(handler)


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
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)

    def _assert_clean_headers(self, headers: httpx2.Headers) -> None:
        """Verify no ambient OpenAI or custom header survived the request allow-list."""
        lower_names = {name.lower() for name in headers.keys()}
        self.assertNotIn("openai-organization", lower_names)
        self.assertNotIn("openai-project", lower_names)
        self.assertNotIn("x-leak", lower_names)
        for name in lower_names:
            self.assertTrue(
                name in _EXPECTED_HEADER_NAMES or name.startswith("x-stainless-"),
                f"unexpected header on wire: {name}",
            )

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

    def test_env_isolation_keyed_positive_and_negative(self) -> None:
        """Keyed client sends only its own Bearer token to its base_url and drops ambient env headers."""
        seen: list[httpx2.Request] = []
        transport = _recording_transport(seen, embed_reply={"data": [{"index": 0, "embedding": [0.5]}]})
        with patch.dict(os.environ, _AMBIENT_OPENAI_ENV):
            client = LLMClient("https://target.test:8443/v1", timeout=5.0, api_key="explicit-key", transport=transport)
            self.assertEqual(client.chat("m", [{"role": "user", "content": "hi"}]), "ok")
            self.assertEqual(client.embed("e", ["doc"]), [[0.5]])
        self.assertEqual(len(seen), 2)
        for req in seen:
            self.assertEqual(req.url.host, "target.test")
            self.assertEqual(req.url.port, 8443)
            self.assertEqual(req.headers.get_list("authorization"), ["Bearer explicit-key"])
            self._assert_clean_headers(req.headers)

    def test_env_isolation_keyless_boundary(self) -> None:
        """Keyless client sends no Authorization under ambient env or empty OPENAI_* env."""
        seen: list[httpx2.Request] = []
        transport = _recording_transport(seen)
        with patch.dict(os.environ, _AMBIENT_OPENAI_ENV):
            for blank_key in (None, ""):
                client = LLMClient("https://target.test:8443/v1", timeout=5.0, api_key=blank_key, transport=transport)
                self.assertEqual(client.chat("m", [{"role": "user", "content": "hi"}]), "ok")
        for req in seen:
            self.assertEqual(req.url.host, "target.test")
            self.assertNotIn("authorization", req.headers)
            self._assert_clean_headers(req.headers)

        seen.clear()
        cleared_openai = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")}
        with patch.dict(os.environ, cleared_openai, clear=True):
            client = LLMClient("https://target.test:8443/v1", timeout=5.0, api_key=None, transport=transport)
            self.assertEqual(client.embed("e", []), [])
        self.assertEqual(seen[-1].url.host, "target.test")
        self.assertNotIn("authorization", seen[-1].headers)

    def test_wire_body_exact_equality_positive_negative_and_boundary(self) -> None:
        """Chat (plain, schema, zero seed) and embeddings (non-empty, empty) match exact wire keys."""
        seen: list[httpx2.Request] = []
        embed_rows = {"data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [2.0]}]}
        client = LLMClient("https://wire.test/v1", timeout=7.5, transport=_recording_transport(seen, embed_reply=embed_rows))
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

        client.chat("m-plain", msgs)
        self.assertEqual(json.loads(seen[-1].content), {
            "model": "m-plain", "messages": msgs, "temperature": 0.0, "seed": 1, "stream": False,
        })

        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        client.chat("m-struct", msgs, schema=schema, schema_name="check", temperature=0.25, seed=0)
        self.assertEqual(json.loads(seen[-1].content), {
            "model": "m-struct", "messages": msgs, "temperature": 0.25, "seed": 0, "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {"name": "check", "strict": False, "schema": schema}},
        })

        client.embed("m-emb", ["a", "b"])
        self.assertEqual(json.loads(seen[-1].content), {"model": "m-emb", "input": ["a", "b"]})

        empty_client = LLMClient("https://wire.test/v1", timeout=7.5, transport=_recording_transport(seen))
        self.assertEqual(empty_client.embed("m-emb", []), [])
        self.assertEqual(json.loads(seen[-1].content), {"model": "m-emb", "input": []})

    def test_http_503_and_transport_errors_map_to_oserror_contract(self) -> None:
        """HTTP 503 includes status + provider_id only; connection/timeout omit status; timeout propagates."""
        base = "https://fixture-user:fixture-pass@example.test:8443/v1///"
        seen_503: list[httpx2.Request] = []
        t_503 = _recording_transport(seen_503, status=503, chat_reply={"error": "fixture-503-secret"},
                                     headers={"X-Fixture": "fixture-503-hdr"})
        c_503 = LLMClient(base, timeout=12.5, api_key="fixture-api-key", transport=t_503)
        self.assertEqual(c_503._http_client.timeout, httpx2.Timeout(12.5))
        self.assertEqual(c_503._client.timeout, 12.5)
        with self.assertRaises(OSError) as err_503:
            c_503.chat("m", [{"role": "user", "content": "hi"}])
        self.assertEqual(str(err_503.exception), "LLM request failed (HTTP 503) for https://example.test:8443/v1/chat/completions")
        self.assertNotIn("fixture-", "".join(traceback.format_exception(err_503.exception)))

        for exc in (httpx2.ConnectError("fixture-conn"), httpx2.ReadTimeout("fixture-timeout")):
            with self.subTest(exc=type(exc).__name__):
                client = LLMClient(base, timeout=5.0, api_key="fixture-api-key", transport=_raising_transport(exc))
                with self.assertRaises(OSError) as caught:
                    client.chat("m", [{"role": "user", "content": "hi"}])
                self.assertEqual(str(caught.exception), "LLM request failed for https://example.test:8443/v1/chat/completions")
                self.assertNotIn("HTTP ", str(caught.exception))
                self.assertNotIn("fixture-", "".join(traceback.format_exception(caught.exception)))

    def test_single_request_per_call_without_sdk_retries(self) -> None:
        """503 status, connection failure, timeout, and 200 OK each issue exactly one transport request."""
        seen_503: list[httpx2.Request] = []
        t_503 = _recording_transport(seen_503, status=503, headers={"retry-after": "0", "x-should-retry": "true"})
        client_503 = LLMClient("https://retry.test/v1", timeout=5.0, transport=t_503)
        with self.assertRaisesRegex(OSError, r"\(HTTP 503\)"):
            client_503.chat("m", [{"role": "user", "content": "hi"}])
        self.assertEqual(len(seen_503), 1)

        conn_calls: list[int] = []
        client_conn = LLMClient(
            "https://retry.test/v1", timeout=5.0,
            transport=_raising_transport(httpx2.ConnectError("down"), conn_calls),
        )
        with self.assertRaises(OSError):
            client_conn.embed("e", ["x"])
        self.assertEqual(len(conn_calls), 1)

        seen_ok: list[httpx2.Request] = []
        client_ok = LLMClient("https://retry.test/v1", timeout=5.0, transport=_recording_transport(seen_ok))
        self.assertEqual(client_ok.chat("m", [{"role": "user", "content": "hi"}]), "ok")
        self.assertEqual(len(seen_ok), 1)

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
        """Reply without choices or with malformed choice/message/content raises ValueError."""
        bad_chat_replies: list[Any] = [
            [],
            {"choices": []},
            {"other_key": 123},
            {"choices": ["not-an-object"]},
            {"choices": [{"message": "not-an-object"}]},
            {"choices": [{"message": {"content": None}}]},
            {"choices": [{"message": {"content": 123}}]},
        ]
        for payload in bad_chat_replies:
            with self.subTest(payload=payload):
                client = LLMClient("https://bad.test/v1", transport=_recording_transport([], chat_reply=payload))
                with self.assertRaises(ValueError):
                    client.chat("test-model", [{"role": "user", "content": "hi"}])

    def test_negative_embed_count_mismatch(self) -> None:
        """Embed count mismatch or non-object/non-list reply raises ValueError."""
        for payload in ([], {"data": "bad"}, {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}):
            with self.subTest(payload=payload):
                client = LLMClient("https://bad.test/v1", transport=_recording_transport([], embed_reply=payload))
                with self.assertRaises(ValueError):
                    client.embed("test-model", ["text1", "text2"])

    def test_transport_failure_traceback_uses_safe_endpoint(self) -> None:
        """Sanitize both the displayed request endpoint and the underlying error."""
        base = "https://fixture-user:fixture-pass@example.test:8443/v1"
        failure = httpx2.ConnectError(f"{base}?fixture-query#fixture-fragment")
        client = LLMClient(base, transport=_raising_transport(failure))
        with self.assertRaisesRegex(OSError, r"https://example\.test:8443/v1/embeddings$") as error:
            client.embed("model", ["sample"])
        self.assertNotIn("fixture-", "".join(traceback.format_exception(error.exception)))

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
        self.addCleanup(auth_client.close)
        auth_client.chat("test-model", [{"role": "user", "content": "hi"}])
        req = self.server.requests[-1]  # type: ignore[attr-defined]
        self.assertEqual(req["headers"].get("Authorization"), "Bearer test-secret")

        no_auth_client = LLMClient(self.base_url, timeout=5.0, api_key=None)
        self.addCleanup(no_auth_client.close)
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
            self.addCleanup(client.close)
            self.assertEqual(client.base_url, "http://127.0.0.1:1234/v1")
            self.assertEqual(client.api_key, "key1")

    def test_embed_reorders_out_of_order_indices_and_rejects_bad_permutations(self) -> None:
        """Reorder distinct out-of-order indices and reject duplicate, missing or non-int indices."""
        self.server.custom_handler = lambda _path, _body, _headers: (  # type: ignore[attr-defined]
            200, {"data": [{"index": 1, "embedding": [0.3, 0.4]}, {"index": 0, "embedding": [0.1, 0.2]}]}
        )
        self.assertEqual(self.client.embed("test-model", ["first", "second"]), [[0.1, 0.2], [0.3, 0.4]])

        bad_payloads = {
            "duplicate index": [{"index": 0, "embedding": [0.1]}, {"index": 0, "embedding": [0.2]}],
            "missing index value": [{"index": 0, "embedding": [0.1]}, {"index": 2, "embedding": [0.2]}],
            "negative index": [{"index": -1, "embedding": [0.1]}, {"index": 0, "embedding": [0.2]}],
            "boolean indices": [{"index": False, "embedding": [0.1]}, {"index": True, "embedding": [0.2]}],
            "missing index key": [{"embedding": [0.1]}, {"index": 1, "embedding": [0.2]}],
        }
        for label, items in bad_payloads.items():
            with self.subTest(label=label):
                self.server.custom_handler = lambda _p, _b, _h, rows=items: (  # type: ignore[attr-defined]
                    200, {"data": rows}
                )
                with self.assertRaises(ValueError):
                    self.client.embed("test-model", ["first", "second"])

    def test_http_errors_keep_status_code_without_url_headers_or_body(self) -> None:
        """Distinguish HTTP 401 and 429 while omitting URL userinfo, headers and body."""
        base = "https://fixture-user:fixture-pass@example.test:8443/v1"
        messages_by_code = {}
        for status_code in (401, 429):
            transport = _recording_transport(
                [], status=status_code, embed_reply={"error": "fixture-body-secret"},
                headers={"X-Fixture-Header": "fixture-header-secret"},
            )
            client = LLMClient(base, timeout=5.0, transport=transport)
            with self.assertRaises(OSError) as caught:
                client.embed("model", ["sample"])
            rendered = "".join(traceback.format_exception(caught.exception))
            self.assertIn(f"HTTP {status_code}", str(caught.exception))
            self.assertIn("https://example.test:8443/v1/embeddings", str(caught.exception))
            self.assertNotIn("fixture-", rendered)
            self.assertNotIn("X-Fixture-Header", rendered)
            messages_by_code[status_code] = str(caught.exception)
        self.assertNotEqual(messages_by_code[401], messages_by_code[429])

        self.server.custom_handler = lambda _p, _b, _h: (  # type: ignore[attr-defined]
            429, {"error": "fixture-rate-limit-body"}
        )
        with self.assertRaisesRegex(OSError, r"HTTP 429") as live_err:
            self.client.chat("test-model", [{"role": "user", "content": "hi"}])
        self.assertNotIn("fixture-rate-limit-body", str(live_err.exception))


if __name__ == "__main__":
    unittest.main()
