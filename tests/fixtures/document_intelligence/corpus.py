# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic, public-safe Document Intelligence benchmark corpus (written for PADOS; no real company data).

Six invented nonconformance reports (NCRs) for invented parts. Each report has a heading, a summary, labelled 8D
facts (``Root cause: ...``), a whitespace-aligned measurement table, discussion paragraphs that PARAPHRASE the facts
and distractor paragraphs that SHARE vocabulary with other reports (so neither lexical nor dense retrieval is
favoured by construction). NCR-2091 has no root cause yet (abstention case); NCR-2095 states two different root
causes (conflict case).

Every passage has a stable key (``c1.root``); queries carry graded judgments over keys (2 = states the answer,
1 = related). The same content renders two ways:

* ``case_pdf`` / ``binder_pdf``: a text-layer PDF (one passage per paragraph, blank line between paragraphs), for
  the Linux OCR-pack path;
* ``binder_lines``: (page, key, text) rows, for building an ``ocr.document/v1`` through the engine-result path with
  line geometry (one passage per paragraph), which also runs where the OCR pack cannot (Windows).

The corpus proves nothing about production quality: it is small, synthetic and English only.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

CORPUS_VERSION = "pados.document-intelligence.synthetic-ncr.v1"


@dataclass(frozen=True)
class Case:
    case_id: str
    ncr: str
    title: str
    passages: tuple[tuple[str, str], ...]  # (key, text) in reading order
    table: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class Query:
    query_id: str
    text: str
    judgments: dict[str, int] = field(default_factory=dict)  # passage key -> grade
    style: str = "paraphrase"  # "keyword" | "paraphrase"


CASES: tuple[Case, ...] = (
    Case("case.ncr-2041", "NCR-2041", "Nonconformance report NCR-2041: PH-300 pump housing", (
        ("c1.summary", "Summary: Field returns showed hairline cracks starting at the mounting boss of the PH-300 "
                       "pump housing within the first month of service."),
        ("c1.team", "Team: molding engineering, supplier quality and dryer maintenance"),
        ("c1.problem", "Problem: cracked mounting boss on PH-300 housings"),
        ("c1.containment", "Containment: quarantine molding lots 2208 to 2212 and inspect every housing"),
        ("c1.root", "Root cause: resin moisture above specification after the hopper dryer failed"),
        ("c1.action", "Corrective action: dew point interlock on the hopper dryer"),
        ("c1.validation", "Validation: three consecutive lots molded with zero cracked bosses"),
        ("c1.prevention", "Prevention: resin moisture check added to the molding control plan"),
        ("c1.closure", "Closure: report closed after the supplier audit"),
        ("c1.discussion", "Discussion: the plastic absorbed water because the drying equipment stopped working, "
                          "which made the molded parts brittle so they split under bolt torque."),
        ("c1.distractor", "Warehouse note: humidity in the finished goods warehouse stayed within limits and the "
                          "cardboard cartons showed no moisture damage during the audit week."),
    ), (("Lot", "Boss cracks", "Moisture pct"), ("2208", "14", "0.31"), ("2213", "0", "0.08"))),
    Case("case.ncr-2057", "NCR-2057", "Nonconformance report NCR-2057: GB-12 conveyor gearbox", (
        ("c2.summary", "Summary: Customers reported a grinding noise from the GB-12 conveyor gearbox and early "
                       "output bearing failures after roughly two hundred running hours."),
        ("c2.team", "Team: assembly, test laboratory and design engineering"),
        ("c2.problem", "Problem: noisy GB-12 gearbox with output bearing failure"),
        ("c2.containment", "Containment: hold all GB-12 shipments and run an end of line noise test"),
        ("c2.root", "Root cause: wrong shim thickness gave insufficient bearing preload"),
        ("c2.action", "Corrective action: shim selection gauge at the assembly station"),
        ("c2.validation", "Validation: endurance test of twenty gearboxes passed without noise"),
        ("c2.prevention", "Prevention: shim thickness recorded in the traveler for every gearbox"),
        ("c2.closure", "Closure: report closed after the endurance test review"),
        ("c2.discussion", "Discussion: because the spacer under the bearing was too thin, the bearing sat loose, "
                          "rattled under load and wore out long before its rated life."),
        ("c2.distractor", "Maintenance note: the paint line conveyor received new bearings and fresh grease as "
                          "part of the scheduled preventive maintenance."),
    ), (("Unit", "Preload N", "Noise dB"), ("G1", "120", "71"), ("G2", "410", "58"))),
    Case("case.ncr-2063", "NCR-2063", "Nonconformance report NCR-2063: CP-7 control panel", (
        ("c3.summary", "Summary: CP-7 control panels restarted by themselves several times per shift at two "
                       "customer sites."),
        ("c3.team", "Team: electronics manufacturing, process engineering and field service"),
        ("c3.problem", "Problem: intermittent reset of the CP-7 control panel"),
        ("c3.containment", "Containment: x-ray inspection of power connectors on stocked panels"),
        ("c3.root", "Root cause: solder voids in the power connector after reflow profile drift"),
        ("c3.action", "Corrective action: reflow oven profile verification at every shift start"),
        ("c3.validation", "Validation: x-ray of five hundred joints found no voids above limit"),
        ("c3.prevention", "Prevention: oven thermocouple calibration added to the maintenance calendar"),
        ("c3.closure", "Closure: report closed after field units were replaced"),
        ("c3.discussion", "Discussion: the oven slowly ran cooler than programmed, so the joints on the supply "
                          "plug trapped gas pockets and the panel lost power when it vibrated."),
        ("c3.distractor", "Training note: operators completed the soldering refresher course and the hand "
                          "soldering stations were cleaned."),
    ), (("Board", "Void pct", "Resets per shift"), ("B17", "38", "4"), ("B52", "6", "0"))),
    Case("case.ncr-2079", "NCR-2079", "Nonconformance report NCR-2079: HH-5 hydraulic hose", (
        ("c4.summary", "Summary: HH-5 hydraulic hose assemblies leaked at the crimped fitting during the "
                       "customer pressure test."),
        ("c4.team", "Team: hose assembly, tooling and quality laboratory"),
        ("c4.problem", "Problem: leak at the crimp of HH-5 hose assemblies"),
        ("c4.containment", "Containment: pressure test every HH-5 assembly in stock before release"),
        ("c4.root", "Root cause: crimp die wear beyond the tooling limit"),
        ("c4.action", "Corrective action: die replacement interval based on crimp count"),
        ("c4.validation", "Validation: burst test of thirty assemblies met the pressure rating"),
        ("c4.prevention", "Prevention: crimp diameter measured on the first piece of every batch"),
        ("c4.closure", "Closure: report closed after the customer approved the corrective action"),
        ("c4.discussion", "Discussion: the tool that squeezes the fitting had worn down, so the sleeve was not "
                          "compressed enough and oil escaped under pressure."),
        ("c4.distractor", "Facilities note: the compressed air leak near the loading dock was repaired and the "
                          "pressure regulator was replaced."),
    ), (("Batch", "Crimp mm", "Leaks"), ("H4", "21.9", "3"), ("H9", "21.4", "0"))),
    Case("case.ncr-2091", "NCR-2091", "Nonconformance report NCR-2091: BR-22 painted bracket", (
        ("c5.summary", "Summary: Paint peeled from BR-22 brackets after the salt spray test; the investigation "
                       "is still open."),
        ("c5.team", "Team: finishing, chemistry laboratory and supplier quality"),
        ("c5.problem", "Problem: paint peeling on BR-22 brackets"),
        ("c5.containment", "Containment: hold painted BR-22 stock pending adhesion tests"),
        ("c5.discussion", "Discussion: pretreatment bath samples were sent to the laboratory; no cause has been "
                          "confirmed yet and no corrective action has been chosen."),
        ("c5.distractor", "Supplier note: the bracket steel supplier delivered on time and the coil certificates "
                          "matched the purchase order."),
    )),
    Case("case.ncr-2095", "NCR-2095", "Nonconformance report NCR-2095: FV-9 flow valve", (
        ("c6.summary", "Summary: FV-9 flow valves stuck partly open during the customer line trial."),
        ("c6.team", "Team: valve assembly and design engineering"),
        ("c6.problem", "Problem: sticking FV-9 flow valve"),
        ("c6.root_a", "Root cause: burr on the valve seat from the machining operation"),
        ("c6.root_b", "Root cause: spring force below drawing tolerance"),
        ("c6.discussion", "Discussion: the machining review and the spring test each claim to explain the "
                          "sticking; the team has not agreed which one is the cause."),
    )),
)

QUERIES: tuple[Query, ...] = (
    Query("q01", "NCR-2041 root cause", {"c1.root": 2, "c1.discussion": 1}, "keyword"),
    Query("q02", "why did the pump housings crack", {"c1.root": 2, "c1.discussion": 2, "c1.summary": 1}),
    Query("q03", "which molding lots were quarantined", {"c1.containment": 2}, "keyword"),
    Query("q04", "what stops wet plastic from being molded again", {"c1.action": 2, "c1.prevention": 2}),
    Query("q05", "GB-12 shim preload", {"c2.root": 2, "c2.action": 1, "c2.prevention": 1}, "keyword"),
    Query("q06", "why did the gearbox make a grinding noise", {"c2.root": 2, "c2.discussion": 2,
                                                                "c2.summary": 1}),
    Query("q07", "how was the gearbox fix proven", {"c2.validation": 2}),
    Query("q08", "CP-7 intermittent reset", {"c3.problem": 2, "c3.summary": 1, "c3.root": 1}, "keyword"),
    Query("q09", "why did the control panels restart by themselves", {"c3.root": 2, "c3.discussion": 2,
                                                                     "c3.summary": 1}),
    Query("q10", "reflow oven profile check", {"c3.action": 2, "c3.root": 1, "c3.prevention": 1}, "keyword"),
    Query("q11", "what was done with panels already in stock", {"c3.containment": 2}),
    Query("q12", "HH-5 crimp die wear", {"c4.root": 2, "c4.action": 1}, "keyword"),
    Query("q13", "why did oil escape from the hose fitting", {"c4.root": 2, "c4.discussion": 2,
                                                             "c4.summary": 1}),
    Query("q14", "how often is the crimping tool replaced", {"c4.action": 2}),
    Query("q15", "BR-22 paint peeling cause", {"c5.discussion": 2, "c5.problem": 1, "c5.summary": 1}, "keyword"),
    Query("q16", "FV-9 valve sticking root cause", {"c6.root_a": 2, "c6.root_b": 2, "c6.discussion": 1},
          "keyword"),
    Query("q17", "who worked on the cracked housing investigation", {"c1.team": 2}),
    Query("q18", "bearing grease maintenance", {"c2.distractor": 2}, "keyword"),
    Query("q19", "salt spray adhesion failure", {"c5.summary": 2, "c5.problem": 1, "c5.containment": 1}),
    Query("q20", "how was the hose fix validated", {"c4.validation": 2}),
)


def _escape(text: str) -> bytes:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")


def _wrap(text: str, width: int = 92) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return [*lines, line] if line else lines


def case_blocks(case: Case) -> Iterator[tuple[str, list[str]]]:
    """(key, lines) per passage: heading, passages, and the table as one passage."""
    yield f"{case.case_id}.heading", [case.title]
    for key, text in case.passages:
        if key.endswith(".discussion") or key.endswith(".summary") or key.endswith(".distractor"):
            yield key, _wrap(text)
        else:
            yield key, [text]
    if case.table:
        yield f"{case.case_id}.table", ["    ".join(f"{cell:<12}" for cell in row).rstrip() for row in case.table]


def binder_lines(cases: tuple[Case, ...] = CASES) -> list[tuple[int, str, str]]:
    """(page, key, paragraph text with newlines) for every passage; one case per page."""
    rows = []
    for page, case in enumerate(cases, start=1):
        for key, lines in case_blocks(case):
            rows.append((page, key, "\n".join(lines)))
    return rows


def _content(case: Case) -> bytes:
    out = [b"BT /F1 10 Tf 12 TL 54 750 Td"]
    for _key, lines in case_blocks(case):
        for line in lines:
            out.append(b"(" + _escape(line) + b") Tj T*")
        out.append(b"T*")  # a blank line between paragraphs
    out.append(b"ET")
    return b"\n".join(out)


def _pdf(contents: list[bytes]) -> bytes:
    objects: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
                                 3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"}
    kids, next_id = [], 4
    for content in contents:
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        kids.append(page_id)
        objects[page_id] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R"
                            b" >> >> /Contents %d 0 R >>" % content_id)
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
    objects[2] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % kid for kid in kids) + b"] /Count %d >>" % len(kids)
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


def case_pdf(case: Case) -> bytes:
    return _pdf([_content(case)])


def binder_pdf(cases: tuple[Case, ...] = CASES) -> bytes:
    return _pdf([_content(case) for case in cases])


def case(case_id: str) -> Case:
    return next(item for item in CASES if item.case_id == case_id)


def engine_result(cases: tuple[Case, ...] = CASES) -> dict[str, object]:
    """``ocr.engine-result/v1`` for the binder: one line per passage, stacked with geometry (as an OCR engine
    reports it). Paragraph newlines become spaces, exactly as the OCR normalizer treats engine lines."""
    pages: dict[int, list[dict[str, object]]] = {}
    for page, _key, text in binder_lines(cases):
        lines = pages.setdefault(page, [])
        top = 40.0 + 24.0 * len(lines)
        lines.append({"text": text, "bbox": [54.0, top, 558.0, top + 12.0], "confidence": 1.0})
    return {"schema_version": "ocr.engine-result/v1",
            "pages": [{"page_number": page, "width": 612.0, "height": 792.0, "unit": "pt", "lines": lines}
                      for page, lines in sorted(pages.items())]}


def passage_keys(cases: tuple[Case, ...] = CASES) -> dict[str, str]:
    """Passage id (``p<page>.l<n>``) -> corpus key, for the engine-result document."""
    keys: dict[str, str] = {}
    counters: dict[int, int] = {}
    for page, key, _text in binder_lines(cases):
        counters[page] = counters.get(page, 0) + 1
        keys[f"p{page}.l{counters[page]}"] = key
    return keys


def ocr_document(source: object, cases: tuple[Case, ...] = CASES) -> object:
    """The binder as an ``ocr.document/v1`` built through the existing normalization (FIXTURE provenance: this is
    synthetic recognition, never a claim about OCR quality). ``source`` is the binder PDF's ``ArtifactRef``."""
    from pados.packs.ocr._stage import build_document, engine_record, stage_mark
    from pados.packs.ocr.fixture import fixture_descriptor
    from pados.packs.ocr.normalize import recognized_pages

    descriptor = fixture_descriptor()
    numbers = list(range(1, len(cases) + 1))
    pages = recognized_pages(engine_result(cases), selected=numbers, producer=descriptor.adapter_id)
    return build_document(source, [pages[number] for number in numbers],  # type: ignore[arg-type]
                          engines=(engine_record(descriptor, attempted=numbers, pages=numbers),),
                          stages=(stage_mark(descriptor, "recognize"),), fixture=True)
