# SPDX-License-Identifier: AGPL-3.0-only
"""Clean-room similarity audit (evidence, not legal proof).

For each replacement file, compare against (1) the quarantined old PADOS
implementation (git history at BASE) and (2) the donor source files named by
the consolidation manifest (read-only, GIT_OPTIONAL_LOCKS=0). Flags:
identical non-trivial line blocks, distinctive comments, identifier-sequence
overlap, and shared literals not mandated by the clean-room contracts.
Usage: similarity_audit.py BASE DONORS_DIR OUT.json
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = ["src/pados/execution/context.py", "src/pados/execution/handoff.py", "src/pados/execution/receipts.py",
         "src/pados/product_loop/state.py", "src/pados/knowledge/identity.py", "src/pados/knowledge/adapters.py",
         "web/src/resourceCache.ts", "web/src/useResource.ts"]
LEXEME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|==|!=|<=|>=|->|=>|\*\*|[^\sA-Za-z0-9_]")
IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
STRING = re.compile(r"(['\"])((?:\\.|(?!\1).){8,}?)\1")
COMMENT = re.compile(r"(?:#|//)\s?(.*)$")
TRIVIAL = re.compile(r"^[\s\)\]\}\(\[\{,;:]*$|^(from|import|export|return|pass|else:|try:|finally:|\}|\);)\b")
KEYWORDS = {"False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield", "self", "str", "int", "bool", "dict", "list", "tuple", "set", "Any", "Mapping", "Sequence", "const", "let", "function", "export", "type", "Promise", "new", "null", "true", "false", "string", "unknown"}


def git(args: list[str], cwd: Path) -> str:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env).stdout


def contract_terms() -> set[str]:
    text = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "docs/clean-room").glob("*_contract.md"))
    terms = set(re.findall(r"`([^`]+)`", text)) | set(IDENT.findall(text)) | set(re.findall(r"[A-Z][A-Z0-9_]{5,}", text))
    # Every whitespace/punctuation-separated token of the contracts is contract-mandated vocabulary.
    return terms | set(re.findall(r"[^\s`,;()|]+", text))


def ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i:i + n]) for i in range(max(0, len(tokens) - n + 1))}


def strip_comments(text: str) -> str:
    return "\n".join(COMMENT.sub("", line) if not line.lstrip().startswith(('"', "'")) else line
                     for line in text.splitlines())


def compare(new: str, old: str, allowed: set[str]) -> dict[str, object]:
    new_tokens, old_tokens = LEXEME.findall(strip_comments(new)), LEXEME.findall(strip_comments(old))
    grams_new, grams_old = ngrams(new_tokens, 8), ngrams(old_tokens, 8)
    containment = len(grams_new & grams_old) / len(grams_new) if grams_new else 0.0
    norm = lambda s: [re.sub(r"\s+", " ", line.strip()) for line in s.splitlines()]
    new_lines, old_lines = norm(new), norm(old)
    matcher = difflib.SequenceMatcher(None, new_lines, old_lines, autojunk=False)
    blocks = []
    for block in matcher.get_matching_blocks():
        lines = [line for line in new_lines[block.a:block.a + block.size] if line and not TRIVIAL.match(line)]
        if len(lines) >= 3:
            blocks.append({"lines": len(lines), "sample": lines[0][:80]})
    comments_new = {m.group(1).strip() for line in new.splitlines() if (m := COMMENT.search(line)) and len(m.group(1).strip()) > 20}
    comments_old = {m.group(1).strip() for line in old.splitlines() if (m := COMMENT.search(line)) and len(m.group(1).strip()) > 20}
    ident_new = [i for i in IDENT.findall(strip_comments(new)) if i not in KEYWORDS and i not in allowed]
    ident_old = [i for i in IDENT.findall(strip_comments(old)) if i not in KEYWORDS and i not in allowed]
    ident_seq = len(ngrams(ident_new, 4) & ngrams(ident_old, 4)) / max(1, len(ngrams(ident_new, 4)))
    literals_new = {m.group(2) for m in STRING.finditer(new)}
    literals_old = {m.group(2) for m in STRING.finditer(old)}
    # Drop fragments produced by the simple literal regex spanning code between two quotes.
    shared_literals = sorted(s for s in literals_new & literals_old if not s[:1] in {",", ")", " ", "(", "]"})
    unexplained = [s for s in shared_literals if s not in allowed and not any(s in term or term in s for term in allowed if len(term) > 7)]
    flags = []
    if containment > 0.35:
        flags.append("HIGH_TOKEN_OVERLAP")
    if any(b["lines"] >= 5 for b in blocks):
        flags.append("IDENTICAL_BLOCK_5+_LINES")
    if comments_new & comments_old:
        flags.append("SHARED_COMMENTS")
    if ident_seq > 0.25:
        flags.append("IDENTIFIER_SEQUENCE_OVERLAP")
    if len(unexplained) >= 3:
        flags.append("UNEXPLAINED_SHARED_LITERALS")
    return {"token_8gram_containment": round(containment, 3), "identifier_4gram_overlap": round(ident_seq, 3),
            "common_blocks_3plus": blocks, "shared_comments": sorted(comments_new & comments_old),
            "shared_literals": shared_literals, "unexplained_shared_literals": unexplained, "flags": flags}


def main() -> int:
    base, donors, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    allowed = contract_terms()
    manifest = json.loads(git(["show", f"{base}:docs/migration/PADOS_ASSET_MIGRATION_MANIFEST.json"], ROOT))
    report = {"base": base, "files": {}}
    for path in FILES:
        new = (ROOT / path).read_text(encoding="utf-8")
        old = git(["show", f"{base}:{path}"], ROOT)
        entry: dict[str, object] = {"new_lines": len(new.splitlines()), "old_lines": len(old.splitlines()),
                                    "vs_old_pados": compare(new, old, allowed), "vs_donors": []}
        for component in manifest["components"]:
            if component["destination"] != path or not component["migration_type"].startswith("DIRECT"):
                continue
            repo = donors / component["source_repo"]
            for source in str(component["source_path"]).split(","):
                source = source.strip()
                try:
                    donor = git(["show", f"HEAD:{source}"], repo)
                except subprocess.CalledProcessError:
                    entry["vs_donors"].append({"donor": f"{component['source_repo']}:{source}", "error": "NOT_FOUND"})
                    continue
                entry["vs_donors"].append({"donor": f"{component['source_repo']}:{source}", **compare(new, donor, allowed)})
        report["files"][path] = entry
    flagged = {p: sorted({*e["vs_old_pados"]["flags"], *[f for d in e["vs_donors"] for f in d.get("flags", [])]})
               for p, e in report["files"].items()}
    report["flagged"] = {p: f for p, f in flagged.items() if f}
    donor_flags = {p: sorted({f for d in e["vs_donors"] for f in d.get("flags", [])}) for p, e in report["files"].items()}
    review = json.loads((ROOT / "docs/clean-room/SIMILARITY_REVIEW.json").read_text(encoding="utf-8"))
    unreviewed = {p: f for p, f in ((p, e["vs_old_pados"]["flags"]) for p, e in report["files"].items())
                  if f and not set(f) <= set(review["classifications"].get(p, {}).get("flags", []))}
    report["donor_flags"] = {p: f for p, f in donor_flags.items() if f}
    report["old_flags_unreviewed"] = unreviewed
    report["suspicious"] = review.get("suspicious", [])
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    for p, e in report["files"].items():
        old = e["vs_old_pados"]
        donor_max = max([d.get("token_8gram_containment", 0) for d in e["vs_donors"]] or [0])
        print(f"{p}: vs_old token8={old['token_8gram_containment']} ident4={old['identifier_4gram_overlap']} "
              f"blocks={[b['lines'] for b in old['common_blocks_3plus']]} comments={len(old['shared_comments'])} "
              f"| vs_donor max token8={donor_max} | flags={flagged[p]}")
    ok = not report["donor_flags"] and not report["old_flags_unreviewed"] and not report["suspicious"]
    print(f"donor_flags={report['donor_flags']} old_flags_unreviewed={report['old_flags_unreviewed']} "
          f"suspicious={report['suspicious']}")
    print("SIMILARITY_AUDIT_OK" if ok else "SIMILARITY_AUDIT_FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
