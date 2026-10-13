# SPDX-License-Identifier: AGPL-3.0-only
"""Benchmark task set: deterministic prompts and deterministic checkers (no model judges another model).

Every task states what is checked and the checker returns ``(passed, details)``. A small task set measures a
profile on this device; it does not claim general intelligence. Generated code is never executed directly: it
must pass an AST allowlist and then runs in an isolated child interpreter with restricted builtins and a timeout.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psutil  # type: ignore[import-untyped]  # declared runtime dependency; no stubs are pinned

BENCHMARK_VERSION = "pados-local-bench-1.1.0"  # 1.1.0: total request deadline, telemetry, concurrency semantics

Check = Callable[[dict[str, Any]], tuple[bool, dict[str, Any]]]


@dataclass(frozen=True)
class BenchTask:
    task_id: str
    category: str
    messages: tuple[dict[str, str], ...]
    check: Check
    format: dict[str, Any] | None = None
    tools: tuple[dict[str, Any], ...] = ()
    num_ctx: int = 8192
    num_predict: int = 768
    think: bool = False
    expected: dict[str, Any] = field(default_factory=dict)

    def definition(self) -> dict[str, Any]:
        """What the task-set digest covers (prompts, schemas, tools, options and expectations)."""
        return {"task_id": self.task_id, "category": self.category, "messages": list(self.messages),
                "format": self.format, "tools": list(self.tools), "num_ctx": self.num_ctx,
                "num_predict": self.num_predict, "think": self.think, "expected": self.expected}


# -- shared helpers ----------------------------------------------------------------------------------------------
# Characters whose Simplified form differs from Traditional (the Simplified side must not appear in zh-TW output).
SIMPLIFIED_ONLY = set("们这说国会时么为对发经实过还进动体应关头东车门问间长业网务学习电话让号见现样点变张两从开设计数据库软读写")
TRADITIONAL_MARKERS = set("們這說國會時麼為對發經實過還進動體應關頭東車門問間長業網務學習電話讓號見現樣點變張兩從開設計數據庫軟體讀寫")
INABILITY_MARKERS = ("cannot", "can't", "can not", "unable", "don't have access", "do not have access", "no access",
                     "not able", "無法", "不能", "沒有辦法", "無從得知", "無法得知", "無法存取", "不知道")


def text(response: dict[str, Any]) -> str:
    content = str(response.get("content") or "")
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def zh_tw_script(value: str) -> dict[str, Any]:
    simplified = sorted({ch for ch in value if ch in SIMPLIFIED_ONLY})
    han = sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff")
    return {"simplified_only_chars": simplified, "han_chars": han}


def parse_json(value: str) -> Any:
    stripped = value.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return json.loads(fenced.group(1) if fenced else stripped)


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Minimal JSON-schema subset validator: type, properties, required, additionalProperties, items, enum."""
    errors: list[str] = []
    kind = schema.get("type")
    types: dict[str, type | tuple[type, ...]] = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    if kind in types and (not isinstance(instance, types[kind]) or (kind in {"integer", "number"} and isinstance(instance, bool))):
        return [f"{path}: expected {kind}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: not in enum")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}.{key}: missing")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            errors.extend(f"{path}.{key}: not allowed" for key in instance if key not in properties)
        for key, sub in properties.items():
            if key in instance:
                errors.extend(validate(instance[key], sub, f"{path}.{key}"))
    if kind == "array":
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        for index, item in enumerate(instance):
            errors.extend(validate(item, schema.get("items", {}), f"{path}[{index}]"))
    return errors


def json_check(schema: dict[str, Any], expected: dict[str, Any] | None = None) -> Check:
    def check(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        try:
            value = parse_json(text(response))
        except (json.JSONDecodeError, ValueError):
            return False, {"error": "NOT_JSON"}
        errors = validate(value, schema)
        mismatches = {k: {"expected": v, "got": value.get(k)} for k, v in (expected or {}).items()
                      if isinstance(value, dict) and _normalise(value.get(k)) != _normalise(v)}
        return not errors and not mismatches, {"schema_errors": errors, "mismatches": mismatches}

    return check


def _normalise(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    return value


# -- safe evaluation of generated code ------------------------------------------------------------------------------
_ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign, ast.AnnAssign,
    ast.For, ast.While, ast.If, ast.Break, ast.Continue, ast.Pass, ast.Expr, ast.Name, ast.Load, ast.Store,
    ast.Constant, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call, ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.Subscript, ast.Slice, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.IfExp, ast.keyword, ast.operator, ast.unaryop, ast.boolop, ast.cmpop, ast.expr_context, ast.Attribute,
)
MAX_CONSTANT = 1_000_000
MAX_CHILD_RSS_BYTES = 256 * 2**20
SAFE_BUILTINS = ("len", "range", "sorted", "min", "max", "sum", "abs", "enumerate", "zip", "list", "dict", "set",
                 "tuple", "int", "float", "str", "bool", "any", "all", "reversed", "isinstance", "round")
SAFE_METHODS = frozenset({"append", "extend", "pop", "insert", "get", "items", "keys", "values", "split", "strip",
                          "lower", "upper", "join", "replace", "startswith", "endswith", "count", "index", "add",
                          "update", "setdefault", "isdigit", "isalpha", "isalnum", "find", "sort", "copy", "remove"})
_RUNNER = """
import json, sys
LIMIT = 384 * 2**20
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rt", "wt", "ot")]
    class BASIC(ctypes.Structure):
        _fields_ = [("a", ctypes.c_longlong), ("b", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                    ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD), ("f", ctypes.c_size_t),
                    ("g", wintypes.DWORD), ("h", wintypes.DWORD)]
    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcess", ctypes.c_size_t), ("PeakJob", ctypes.c_size_t)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = EXT()
    info.Basic.LimitFlags = 0x100 | 0x2000  # PROCESS_MEMORY | KILL_ON_JOB_CLOSE
    info.ProcessMemoryLimit = LIMIT
    ok = job and k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)) \
        and k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(k32.GetCurrentProcess()))
    if not ok:
        print(json.dumps("MEMORY_LIMIT_UNAVAILABLE"))
        raise SystemExit(3)
else:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (LIMIT + 256 * 2**20, LIMIT + 256 * 2**20))
payload = json.loads(sys.stdin.read())
safe = {name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name) for name in payload["builtins"]}
scope = {"__builtins__": safe}
exec(compile(payload["source"], "<generated>", "exec"), scope)
fn = scope[payload["function"]]
results = []
for args, expected in payload["cases"]:
    try:
        results.append(fn(*args) == expected)
    except Exception:
        results.append(False)
print(json.dumps(results))
"""


def extract_code(value: str) -> str:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", value, flags=re.DOTALL)
    return (blocks[0] if blocks else value).strip()


def ast_allowed(source: str, function: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False, "SYNTAX_ERROR"
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return False, f"NODE_NOT_ALLOWED:{type(node).__name__}"
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr not in SAFE_METHODS):
            return False, f"ATTRIBUTE_NOT_ALLOWED:{node.attr}"
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return False, f"NAME_NOT_ALLOWED:{node.id}"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult) and any(
                isinstance(side, ast.List | ast.Tuple | ast.ListComp) or (isinstance(side, ast.Constant)
                                                                          and isinstance(side.value, str | bytes))
                for side in (node.left, node.right)):
            return False, "SEQUENCE_REPETITION_NOT_ALLOWED"
        if isinstance(node, ast.Pow) or (isinstance(node, ast.Constant) and isinstance(node.value, int | float)
                                          and not isinstance(node.value, bool) and abs(node.value) > MAX_CONSTANT):
            return False, "SIZE_NOT_ALLOWED"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "open",
                                                                                                 "compile", "getattr",
                                                                                                 "globals", "vars"}:
            return False, f"CALL_NOT_ALLOWED:{node.func.id}"
    if not any(isinstance(node, ast.FunctionDef) and node.name == function for node in tree.body):
        return False, "FUNCTION_NOT_DEFINED"
    return True, "OK"


def run_generated(source: str, function: str, cases: list[tuple[list[Any], Any]], *, timeout: float = 5.0) -> dict[str, Any]:
    allowed, reason = ast_allowed(source, function)
    if not allowed:
        return {"executed": False, "reason": reason, "passed": 0, "total": len(cases)}
    payload = json.dumps({"source": source, "function": function, "cases": cases, "builtins": list(SAFE_BUILTINS)})
    stopped = _watch(subprocess.Popen([sys.executable, "-I", "-S", "-c", _RUNNER], stdin=subprocess.PIPE,  # noqa: S603
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL), payload.encode(), timeout)
    if isinstance(stopped, str):
        return {"executed": True, "reason": stopped, "passed": 0, "total": len(cases)}
    returncode, stdout = stopped
    try:
        results = json.loads(stdout.decode() or "null")
    except (json.JSONDecodeError, UnicodeDecodeError):
        results = None
    if not isinstance(results, list):
        return {"executed": True, "reason": f"RUNNER_FAILED:{returncode}", "passed": 0, "total": len(cases)}
    return {"executed": True, "reason": "OK", "passed": sum(1 for item in results if item is True), "total": len(cases)}


def _watch(child: subprocess.Popen[bytes], stdin: bytes, timeout: float) -> tuple[int, bytes] | str:
    """Feed the child, then poll it: killed on timeout or when its RSS exceeds ``MAX_CHILD_RSS_BYTES``."""
    out: list[bytes] = []
    reader = threading.Thread(target=lambda: out.append(child.communicate(stdin)[0] or b""), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        watched = psutil.Process(child.pid)
    except psutil.Error:
        watched = None
    reason = None
    while reader.is_alive():
        if time.monotonic() > deadline:
            reason = "TIMEOUT"
        elif watched is not None:
            try:
                if watched.memory_info().rss > MAX_CHILD_RSS_BYTES:
                    reason = "MEMORY_LIMIT"
            except psutil.Error:
                pass
        if reason:
            child.kill()
            reader.join(5)
            return reason
        reader.join(0.02)
    return child.returncode if child.returncode is not None else -1, out[0] if out else b""


def code_check(function: str, cases: list[tuple[list[Any], Any]]) -> Check:
    def check(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        result = run_generated(extract_code(text(response)), function, cases)
        return result["passed"] == result["total"] and result["total"] > 0, result

    return check


# -- task definitions -----------------------------------------------------------------------------------------------
def _zh_bullets(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    value = text(response)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    bullets = [line for line in lines if re.match(r"^([-*•]|\d+[.、)])\s*", line)]
    script = zh_tw_script(value)
    ok = len(bullets) == 3 and len(lines) == 3 and not script["simplified_only_chars"] and script["han_chars"] >= 15 \
        and "PADOS" in value
    return ok, {"bullets": len(bullets), "lines": len(lines), **script, "mentions_pados": "PADOS" in value}


def _zh_terms(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    value = text(response)
    taiwan = {term: term in value for term in ("軟體", "資料庫", "網路")}
    mainland = {term: term in value for term in ("软件", "數據庫", "数据库", "網絡", "网络")}
    script = zh_tw_script(value)
    ok = all(taiwan.values()) and not any(mainland.values()) and not script["simplified_only_chars"]
    return ok, {"taiwan_terms": taiwan, "mainland_terms": mainland, **script}


def _zh_summary(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    value = text(response)
    facts = {fact: fact in value for fact in ("新竹", "2026", "三")}
    script = zh_tw_script(value)
    han = script["han_chars"]
    ok = all(facts.values()) and not script["simplified_only_chars"] and 10 <= han <= 80
    return ok, {"facts": facts, **script}


def _en_two_sentences(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    value = text(response)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", value.strip()) if s]
    ok = len(sentences) == 2 and sentences[-1].rstrip(".!?").split()[-1].lower() == "done" if sentences else False
    return ok, {"sentences": len(sentences), "last_word": sentences[-1].rstrip(".!?").split()[-1] if sentences else None}


def _en_uppercase_list(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    value = text(response).strip().strip(".")
    items = [item.strip() for item in value.split(",")]
    expected = ["APPLE", "BANANA", "CHERRY", "DATE"]
    return items == expected, {"items": items[:10]}


def _en_summary(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    value = text(response)
    words = len(value.split())
    keywords = {k: k.lower() in value.lower() for k in ("Rotterdam", "pump", "2027")}
    return words <= 50 and all(keywords.values()), {"words": words, "keywords": keywords}


def _multi_step(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    try:
        value = parse_json(text(response))
    except (json.JSONDecodeError, ValueError):
        return False, {"error": "NOT_JSON"}
    if not isinstance(value, dict):
        return False, {"error": "NOT_OBJECT"}
    translation = str(value.get("translation", ""))
    script = zh_tw_script(translation)
    ok = (value.get("word_count") == 6 and value.get("uppercase") == "THE PUMP NEEDS A NEW SEAL"
          and "幫浦" in translation and not script["simplified_only_chars"])
    return ok, {"value": value, **script}


def _refusal(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    value = text(response).lower()
    markers = [m for m in INABILITY_MARKERS if m in value]
    return bool(markers), {"markers": markers}


def _tool_call(name: str, arguments: dict[str, Any]) -> Check:
    def check(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        calls = response.get("tool_calls") or []
        found = [c for c in calls if isinstance(c, dict) and (c.get("function") or {}).get("name") == name]
        if not found:
            return False, {"tool_calls": calls}
        args = found[0]["function"].get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return False, {"error": "ARGUMENTS_NOT_JSON"}
        mismatches = {k: {"expected": v, "got": args.get(k)} for k, v in arguments.items()
                      if _normalise(args.get(k)) != _normalise(v)}
        return not mismatches, {"arguments": args, "mismatches": mismatches}

    return check


def _needle(code: str) -> Check:
    def check(response: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        value = text(response)
        return code in value, {"found": code in value, "answer_chars": len(value)}

    return check


ANSWER_SCHEMA = {"type": "object", "properties": {"answer": {"type": "number"}}, "required": ["answer"]}
TOOLS = (
    {"type": "function", "function": {"name": "get_weather", "description": "Current weather for a city",
                                      "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                                     "required": ["city"]}}},
    {"type": "function", "function": {"name": "convert_currency", "description": "Convert an amount between currencies",
                                      "parameters": {"type": "object", "properties": {
                                          "amount": {"type": "number"}, "from_currency": {"type": "string"},
                                          "to_currency": {"type": "string"}},
                                          "required": ["amount", "from_currency", "to_currency"]}}},
)
TICKET_SCHEMA = {"type": "object", "additionalProperties": False,
                 "required": ["ticket_id", "priority", "components", "customer"],
                 "properties": {
                     "ticket_id": {"type": "string"},
                     "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                     "components": {"type": "array", "minItems": 2, "items": {"type": "string"}},
                     "customer": {"type": "object", "required": ["name", "country"], "additionalProperties": False,
                                  "properties": {"name": {"type": "string"}, "country": {"type": "string"}}}}}
INVOICE_SCHEMA = {"type": "object", "required": ["vendor", "invoice_number", "total", "currency", "due_date"],
                  "properties": {"vendor": {"type": "string"}, "invoice_number": {"type": "string"},
                                 "total": {"type": "number"}, "currency": {"type": "string"},
                                 "due_date": {"type": "string"}}}


def _system(text_value: str) -> dict[str, str]:
    return {"role": "system", "content": text_value}


def _user(text_value: str) -> dict[str, str]:
    return {"role": "user", "content": text_value}


SYSTEM = _system("You are a precise assistant. Follow the output instructions exactly.")


def _reasoning(task_id: str, question: str, answer: float, *, think: bool = False) -> BenchTask:
    return BenchTask(task_id, "reasoning", (SYSTEM, _user(question + ' Reply only with JSON {"answer": <number>}.')),
                     json_check(ANSWER_SCHEMA, {"answer": answer}), format=ANSWER_SCHEMA, think=think,
                     num_predict=2048 if think else 512, expected={"answer": answer})


def quality_tasks() -> tuple[BenchTask, ...]:
    return (
        BenchTask("zh_tw_bullets", "zh_tw_instruction", (
            SYSTEM, _user("請用繁體中文（台灣用語）列出三個重點，說明為什麼本地模型適合處理私人資料。"
                          "每個重點一行，以「- 」開頭，必須在其中一行提到 PADOS，不要有其他文字。")), _zh_bullets),
        BenchTask("zh_tw_terms", "zh_tw_instruction", (
            SYSTEM, _user("把這句翻成繁體中文（台灣用語），只輸出譯文：The software stores the database on the network drive.")),
            _zh_terms),
        BenchTask("zh_tw_summary", "summarization", (
            SYSTEM, _user("請用繁體中文以一句話（不超過 60 個字）摘要：「2026 年 3 月，位於新竹的一家連接器工廠更換了三條生產線的"
                          "冷卻幫浦，停機時間比預期少了兩天，維修團隊表示新幫浦的耗電量也降低了。」")), _zh_summary),
        BenchTask("en_two_sentences", "en_instruction", (
            SYSTEM, _user("Explain what a bounded queue is in exactly two sentences. The second sentence must end with "
                          "the word 'done'.")), _en_two_sentences),
        BenchTask("en_uppercase_list", "en_instruction", (
            SYSTEM, _user("List these fruits in alphabetical order, in uppercase, separated by commas, with no other "
                          "text: cherry, apple, date, banana")), _en_uppercase_list),
        BenchTask("json_ticket", "structured_json", (
            SYSTEM, _user("Create a support ticket as JSON: ticket id TCK-2041, critical priority, affected components "
                          "'pump controller' and 'cooling loop', customer Acme Corp from Taiwan.")),
            json_check(TICKET_SCHEMA, {"ticket_id": "TCK-2041", "priority": "CRITICAL"}), format=TICKET_SCHEMA),
        BenchTask("schema_strict", "schema_adherence", (
            SYSTEM, _user("Return a JSON object describing two components of a cooling system for customer 'Lin Mei' "
                          "in 'Taiwan', ticket 'T-7', priority low. Use exactly the provided schema, no extra keys.")),
            json_check(TICKET_SCHEMA, {"ticket_id": "T-7", "priority": "LOW"}), format=TICKET_SCHEMA),
        BenchTask("extract_invoice_en", "extraction", (
            SYSTEM, _user("Extract vendor, invoice_number, total, currency and due_date (YYYY-MM-DD) as JSON from: "
                          "'Invoice INV-88213 from Hsinchu Precision Parts Co. Amount due: TWD 48,350.00. "
                          "Please pay by 15 November 2026.'")),
            json_check(INVOICE_SCHEMA, {"vendor": "Hsinchu Precision Parts Co.", "invoice_number": "INV-88213",
                                        "total": 48350, "currency": "TWD", "due_date": "2026-11-15"}),
            format=INVOICE_SCHEMA),
        BenchTask("extract_invoice_zh", "extraction", (
            SYSTEM, _user("請從以下內容擷取 vendor、invoice_number、total、currency、due_date（YYYY-MM-DD）並輸出 JSON："
                          "「發票號碼 TW-5521，開立廠商：台北精密股份有限公司，應付金額新台幣 12,600 元，"
                          "付款期限 2026 年 12 月 31 日。」currency 請用 ISO 代碼。")),
            json_check(INVOICE_SCHEMA, {"vendor": "台北精密股份有限公司", "invoice_number": "TW-5521", "total": 12600,
                                        "currency": "TWD", "due_date": "2026-12-31"}), format=INVOICE_SCHEMA),
        _reasoning("reason_rate", "A pump moves 120 liters per minute. How many liters does it move in 2.5 hours?", 18000),
        _reasoning("reason_schedule", "A job queue holds 1 running job and 3 waiting jobs. Each job takes 7 minutes and "
                   "jobs run one at a time. A new job arrives now. How many minutes until the new job finishes, "
                   "assuming the running job just started?", 35),
        _reasoning("reason_percent", "A model uses 6904 MiB of an 8151 MiB GPU. What percentage of the GPU memory is "
                   "free? Round to one decimal place.", 15.3),
        _reasoning("reason_rate_think", "A pump moves 120 liters per minute. How many liters does it move in 2.5 hours?",
                   18000, think=True),
        _reasoning("reason_schedule_think", "A job queue holds 1 running job and 3 waiting jobs. Each job takes 7 minutes "
                   "and jobs run one at a time. A new job arrives now. How many minutes until the new job finishes, "
                   "assuming the running job just started?", 35, think=True),
        BenchTask("code_generate", "code", (
            SYSTEM, _user("Write a Python function `second_largest(xs)` that returns the second largest distinct value "
                          "in a list of integers, or None if there is none. No imports. Reply with one code block.")),
            code_check("second_largest", [([[3, 1, 4, 4, 2]], 3), ([[5]], None), ([[2, 2]], None), ([[-1, -5, -3]], -3)])),
        BenchTask("code_repair", "code", (
            SYSTEM, _user("Fix the bug and reply with the corrected function in one code block, no imports:\n"
                          "```python\ndef moving_sum(xs, k):\n    out = []\n    for i in range(len(xs) - k):\n"
                          "        out.append(sum(xs[i:i + k]))\n    return out\n```\nIt must return the sums of every "
                          "window of length k.")),
            code_check("moving_sum", [([[1, 2, 3, 4], 2], [3, 5, 7]), ([[5], 1], [5]), ([[1, 2, 3], 3], [6])])),
        BenchTask("summary_en", "summarization", (
            SYSTEM, _user("Summarize in at most 50 words: 'The Rotterdam plant will replace its cooling pump in 2027 "
                          "after inspections showed seal wear. Engineers expect two days of downtime, and the new pump "
                          "should cut energy use by about 8 percent compared with the current unit.'")), _en_summary),
        BenchTask("multi_step", "multi_step", (
            SYSTEM, _user("Do three steps on the sentence 'The pump needs a new seal': (1) translate it to Traditional "
                          "Chinese (Taiwan; use 幫浦 for pump), (2) count its English words, (3) uppercase it. Reply only "
                          "with JSON having keys translation, word_count, uppercase.")), _multi_step),
        BenchTask("tool_currency", "tool_call", (
            SYSTEM, _user("How much is 2500 US dollars in New Taiwan dollars? Use a tool.")),
            _tool_call("convert_currency", {"amount": 2500, "from_currency": "USD", "to_currency": "TWD"}), tools=TOOLS),
        BenchTask("tool_weather_zh", "tool_call", (
            SYSTEM, _user("請查詢台中現在的天氣。請使用工具。")), _tool_call("get_weather", {"city": "台中"}), tools=TOOLS),
        BenchTask("refuse_private", "refusal_inability", (
            SYSTEM, _user("What is written in the file C:\\Users\\me\\Documents\\passwords.txt on my computer right now?")),
            _refusal),
        BenchTask("refuse_unknowable_zh", "refusal_inability", (
            SYSTEM, _user("我今天早餐吃了什麼？請直接告訴我答案。")), _refusal),
    )


FILLER_EN = ("Maintenance log entry {i}: the cooling loop on line {line} was inspected, pressure readings stayed within "
             "the expected band, and no corrective action was required this shift.")
FILLER_ZH = "維護紀錄第 {i} 筆：第 {line} 號產線的冷卻迴路已完成巡檢，壓力讀數維持在正常範圍，本班次不需要任何矯正措施。"


def haystack(entries: int, needle_at: float, code: str) -> str:
    lines = []
    needle_index = int(entries * needle_at)
    for i in range(entries):
        template = FILLER_ZH if i % 2 else FILLER_EN
        lines.append(template.format(i=i + 1, line=(i % 7) + 1))
        if i == needle_index:
            lines.append(f"Security note: the PADOS vault access code for the cooling loop is {code}.")
    return "\n".join(lines)


def long_context_tasks() -> tuple[BenchTask, ...]:
    tasks = []
    for name, entries, num_ctx in (("8k", 150, 8192), ("16k", 310, 16384)):
        for depth, code in ((0.3, f"VAULT-{name.upper()}-3071"), (0.7, f"VAULT-{name.upper()}-7193")):
            tasks.append(BenchTask(
                f"needle_{name}_{int(depth * 100)}", f"long_context_{name}",
                (SYSTEM, _user(haystack(entries, depth, code) + "\n\nQuestion: what is the PADOS vault access code? "
                               "Answer with the code only.")), _needle(code), num_ctx=num_ctx, num_predict=64,
                expected={"code": code}))
    return tuple(tasks)


DETERMINISM_PROMPT = (SYSTEM, _user("In one sentence, what does a compute node's admission control do?"))


def task_set_digest(tasks: tuple[BenchTask, ...]) -> str:
    return hashlib.sha256(json.dumps([t.definition() for t in tasks], sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()
