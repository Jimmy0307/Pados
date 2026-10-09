"""A deterministic in-process stand-in for a llama.cpp server (tests only; never evidence about any model).

It speaks the subset of the server API the native adapter and runtime probe use: ``GET /health``, ``GET /v1/models``,
``GET /props`` and non-streamed ``POST /v1/chat/completions``. Answers are the same canned replies as ``FakeOllama``.

Generic scenarios (same meaning as ``FakeOllama``; not tied to any model):

* ``reasoning_tokens=N``: unless the request disables reasoning (``chat_template_kwargs.enable_thinking`` false),
  a reasoning phase of N tokens spends the output budget (``max_tokens``) first; when the budget is not larger than
  N the visible content stays EMPTY (``finish_reason=length``). ``thinking_despite_disabled`` ignores the switch;
  ``think_inline`` returns the reasoning inside the content as a ``<think>`` block instead of ``reasoning_content``.
* ``answer_tokens=N``: an answer needs N tokens; a smaller budget cuts it off (``finish_reason=length``).
* ``empty_content``: the model produces no visible text.
* ``n_ctx``: the per-slot context window the server was "started" with (fixed for the server's lifetime unless a
  test changes it to simulate a restart); ``props_available=False`` hides ``/props``.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from fake_ollama import ANSWERS


class FakeLlamaServer:
    def __init__(self, *, alias: str, model_path: str, n_ctx: int = 8192, build_info: str = "b6123-1a2b3c4",
                 reasoning_tokens: int = 0, thinking_despite_disabled: bool = False, think_inline: bool = False,
                 empty_content: bool = False, answer_tokens: int = 0, props_available: bool = True,
                 extra_models: tuple[str, ...] = (), restart_after_chat: int | None = None,
                 model_path_after_chat: str | None = None) -> None:
        self.alias = alias
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.build_info = build_info
        self.reasoning_tokens = reasoning_tokens
        self.thinking_despite_disabled = thinking_despite_disabled
        self.think_inline = think_inline
        self.empty_content = empty_content
        self.answer_tokens = answer_tokens
        self.props_available = props_available
        self.extra_models = extra_models
        self.restart_after_chat = restart_after_chat  # a new n_ctx the "restarted" server reports after a chat
        self.model_path_after_chat = model_path_after_chat  # another file the "restarted" server serves afterwards
        self.requests: list[dict[str, Any]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> FakeLlamaServer:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    def chats(self) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if r["path"] == "/v1/chat/completions"]

    def props(self) -> dict[str, Any]:
        return {"default_generation_settings": {"n_ctx": self.n_ctx, "params": {"temperature": 0.8, "seed": -1}},
                "total_slots": 1, "model_path": self.model_path, "build_info": self.build_info,
                "chat_template": "{{ messages }}"}

    @staticmethod
    def answer(body: dict[str, Any]) -> str:
        prompt = body["messages"][-1]["content"]
        for needle, reply in ANSWERS:
            if needle in prompt and reply is not None:
                return json.dumps(reply, ensure_ascii=False) if isinstance(reply, dict) else str(reply)
        return "I do not know."

    def complete(self, body: dict[str, Any]) -> tuple[str, str, str, int]:
        """(reasoning, visible content, finish_reason, completion tokens) under the request's switch and budget."""
        content = self.answer(body)
        if self.empty_content:
            return "", "", "stop", 1
        budget = int(body.get("max_tokens") or 128)
        kwargs = body.get("chat_template_kwargs") or {}
        reasons = self.reasoning_tokens and (kwargs.get("enable_thinking") is not False
                                             or self.thinking_despite_disabled)
        if not reasons:
            if budget < self.answer_tokens:
                return "", content[: max(1, len(content) // 2)], "length", budget
            return "", content, "stop", 40
        if budget <= self.reasoning_tokens:
            return "r" * budget, "", "length", budget
        return "r" * self.reasoning_tokens, content, "stop", self.reasoning_tokens + 40

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return None

            def _json(self, payload: Any, status: int = 200) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._json({"status": "ok"})
                elif self.path == "/v1/models":
                    data = [{"id": name, "object": "model", "owned_by": "llamacpp", "meta": {"n_ctx_train": 40960}}
                            for name in (fake.alias, *fake.extra_models)]
                    self._json({"object": "list", "data": data})
                elif self.path == "/props" and fake.props_available:
                    self._json(fake.props())
                else:
                    self._json({"error": {"code": 404, "message": "File Not Found"}}, 404)

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                fake.requests.append({"path": self.path, "body": body})
                if self.path != "/v1/chat/completions":
                    self._json({"error": {"code": 404, "message": "File Not Found"}}, 404)
                    return
                reasoning, content, finish, used = fake.complete(body)
                message: dict[str, Any] = {"role": "assistant", "content": content}
                if reasoning and fake.think_inline:
                    message["content"] = f"<think>{reasoning}</think>{content}"
                elif reasoning:
                    message["reasoning_content"] = reasoning
                prompt_tokens = len(body["messages"][-1]["content"]) // 3
                if fake.restart_after_chat is not None:
                    fake.n_ctx = fake.restart_after_chat
                if fake.model_path_after_chat is not None:
                    fake.model_path = fake.model_path_after_chat
                self._json({"id": "chatcmpl-fake", "object": "chat.completion", "model": body.get("model"),
                            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": used,
                                      "total_tokens": prompt_tokens + used}})

        return Handler
