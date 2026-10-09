"""A deterministic in-process stand-in for Ollama's native API (tests and Linux dry runs only).

It answers each benchmark prompt with a canned reply that the task checker accepts, so the full pipeline (pull,
digest verification, streaming, counters, ps, unload, tool calls, timeouts, concurrency) can be exercised without a
model. Its numbers are synthetic and never evidence about any model.

Reasoning-budget scenario (generic, not tied to any model): with reasoning_tokens=N the fake behaves like a
runtime whose model runs a reasoning phase of N tokens before the visible answer whenever the request does not
disable it (think absent or true). The reasoning phase spends the output budget (num_predict) first: when
the budget is not larger than N the visible content stays EMPTY (done_reason=length), exactly the failure a
request that does not reproduce the measured configuration can hit. thinking_despite_disabled=True models a
runtime that ignores the switch.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MANIFEST_HEX = "500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"

ANSWERS: tuple[tuple[str, Any], ...] = (
    ("列出三個重點", "- 本地模型讓私人資料留在自己的電腦裡。\n- PADOS 可以在離線時繼續處理工作。\n- 不需要把敏感內容傳到外部服務。"),
    ("The software stores", "這個軟體把資料庫存放在網路磁碟上。"),
    ("一句話", "2026 年 3 月新竹工廠更換三條產線的冷卻幫浦，停機時間比預期少兩天。"),
    ("bounded queue", "A bounded queue holds at most a fixed number of items. When it is full, new work is refused until a slot is done."),
    ("fruits", "APPLE, BANANA, CHERRY, DATE"),
    ("TCK-2041", {"ticket_id": "TCK-2041", "priority": "CRITICAL", "components": ["pump controller", "cooling loop"],
                  "customer": {"name": "Acme Corp", "country": "Taiwan"}}),
    ("'T-7'", {"ticket_id": "T-7", "priority": "LOW", "components": ["pump", "radiator"],
               "customer": {"name": "Lin Mei", "country": "Taiwan"}}),
    ("INV-88213", {"vendor": "Hsinchu Precision Parts Co.", "invoice_number": "INV-88213", "total": 48350,
                   "currency": "TWD", "due_date": "2026-11-15"}),
    ("TW-5521", {"vendor": "台北精密股份有限公司", "invoice_number": "TW-5521", "total": 12600, "currency": "TWD",
                 "due_date": "2026-12-31"}),
    ("120 liters", {"answer": 18000}),
    ("job queue holds", {"answer": 35}),
    ("6904 MiB", {"answer": 15.3}),
    ("second_largest", "```python\ndef second_largest(xs):\n    s = sorted(set(xs))\n    return s[-2] if len(s) > 1 else None\n```"),
    ("moving_sum", "```python\ndef moving_sum(xs, k):\n    out = []\n    for i in range(len(xs) - k + 1):\n"
                   "        out.append(sum(xs[i:i + k]))\n    return out\n```"),
    ("Rotterdam", "Rotterdam will replace its cooling pump in 2027 due to seal wear, with two days of downtime and 8% lower energy use."),
    ("three steps", {"translation": "這台幫浦需要新的密封件", "word_count": 6, "uppercase": "THE PUMP NEEDS A NEW SEAL"}),
    ("passwords.txt", "I cannot access files on your computer."),
    ("早餐", "我無法得知你今天早餐吃了什麼。"),
    ("vault access code", None),  # computed from the haystack
    ("admission control", "Admission control decides whether the node accepts a job or refuses it when capacity is full."),
)
TOOL_ANSWERS = (("2500 US dollars", "convert_currency", {"amount": 2500, "from_currency": "USD", "to_currency": "TWD"}),
                ("台中", "get_weather", {"city": "台中"}))


class FakeOllama:
    def __init__(self, *, digest_hex: str = MANIFEST_HEX, present: bool = False, version: str = "0.34.3",
                 pull_error: str | None = None, reasoning_tokens: int = 0,
                 thinking_despite_disabled: bool = False, empty_content: bool = False,
                 answer_tokens: int = 0) -> None:
        self.digest_hex = digest_hex
        self.pull_error = pull_error  # the pull stream ends with this error and nothing is installed
        self.present = present
        self.version = version
        self.loaded = False
        self.reasoning_tokens = reasoning_tokens
        self.thinking_despite_disabled = thinking_despite_disabled
        self.empty_content = empty_content
        self.answer_tokens = answer_tokens  # > 0: an answer needs this many tokens; a smaller budget cuts it off
        self.requests: list[dict[str, Any]] = []
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> FakeOllama:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()

    def answer(self, body: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        prompt = body["messages"][-1]["content"]
        if body.get("tools"):
            for needle, name, arguments in TOOL_ANSWERS:
                if needle in prompt:
                    return "", [{"function": {"name": name, "arguments": arguments}}]
        for needle, reply in ANSWERS:
            if needle in prompt:
                if reply is None:
                    code = prompt.split("access code for the cooling loop is ")[1].split(".")[0]
                    return code, []
                return (json.dumps(reply, ensure_ascii=False) if isinstance(reply, dict) else reply), []
        return "I do not know.", []

    def reasoning_phase(self, body: dict[str, Any], content: str) -> tuple[str, str, str, int]:
        """(thinking, visible content, done_reason, eval_count) under the request's reasoning switch and budget."""
        if self.empty_content:
            return "", "", "stop", 1
        budget = int((body.get("options") or {}).get("num_predict") or 128)
        reasons = self.reasoning_tokens and (body.get("think") is not False or self.thinking_despite_disabled)
        if not reasons:
            if budget < self.answer_tokens:  # the budget ends the visible answer part-way
                return "", content[: max(1, len(content) // 2)], "length", budget
            return "", content, "stop", 40
        if budget <= self.reasoning_tokens:  # the reasoning phase spent the whole budget: nothing visible
            return "r" * budget, "", "length", budget
        return "r" * self.reasoning_tokens, content, "stop", self.reasoning_tokens + 40

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return None

            def _json(self, payload: Any, status: int = 200) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, chunks: list[dict[str, Any]], delay: float = 0.0) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for chunk in chunks:
                    if delay:
                        time.sleep(delay)
                    self.wfile.write(json.dumps(chunk, ensure_ascii=False).encode() + b"\n")
                    self.wfile.flush()

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/version":
                    self._json({"version": fake.version})
                elif self.path == "/api/tags":
                    models = [{"name": "qwen3:8b", "digest": fake.digest_hex, "size": 5225376147,
                               "details": {"family": "qwen3", "parameter_size": "8.2B", "quantization_level": "Q4_K_M",
                                           "format": "gguf"}}] if fake.present else []
                    self._json({"models": models})
                elif self.path == "/api/ps":
                    self._json({"models": [{"name": "qwen3:8b", "size": 6_400_000_000, "size_vram": 6_400_000_000,
                                            "context_length": 8192}] if fake.loaded else []})
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                fake.requests.append({"path": self.path, "body": body})
                if self.path == "/api/pull" and fake.pull_error:
                    self._stream([{"status": "pulling manifest"}, {"status": "pulling a3de86cd1c13", "completed": 1},
                                  {"error": fake.pull_error}])
                elif self.path == "/api/pull":
                    fake.present = True
                    self._stream([{"status": "pulling manifest"}, {"status": "verifying sha256 digest"}, {"status": "success"}])
                elif self.path == "/api/generate" and body.get("keep_alive") == 0:
                    fake.loaded = False
                    self._json({"done": True})
                elif self.path == "/api/chat":
                    load = 0 if fake.loaded else 1_500_000_000
                    fake.loaded = True
                    prompt = body["messages"][-1]["content"]
                    if "600-word essay" in prompt:
                        self._stream([{"message": {"content": "x"}, "done": False}] * 3, delay=1.0)
                        return
                    content, calls = fake.answer(body)
                    if body.get("stream") is False:  # non-streamed chat (the compute-node backend's call shape)
                        thinking, content, done_reason, used = fake.reasoning_phase(body, content)
                        self._json({"message": {"role": "assistant", "content": content, "thinking": thinking,
                                                "tool_calls": calls},
                                    "done": True, "done_reason": done_reason, "total_duration": 900_000_000 + load,
                                    "load_duration": load, "prompt_eval_count": len(prompt) // 3,
                                    "eval_count": used, "eval_duration": 800_000_000})
                        return
                    half = len(content) // 2
                    self._stream([
                        {"message": {"role": "assistant", "content": content[:half], "tool_calls": calls}, "done": False},
                        {"message": {"role": "assistant", "content": content[half:]}, "done": False},
                        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                         "total_duration": 900_000_000 + load, "load_duration": load, "prompt_eval_count": len(prompt) // 3,
                         "prompt_eval_duration": 100_000_000, "eval_count": 40, "eval_duration": 800_000_000}])
                else:
                    self._json({"error": "not found"}, 404)

        return Handler
