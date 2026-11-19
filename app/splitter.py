"""Sözleşme metnini maddelere ayırır ve her maddenin metindeki konumunu (offset) tutar."""
import re
from dataclasses import dataclass, asdict

# "MADDE 5", "Madde 5 -", "5.", "5)", "5-" ile başlayan satırlar madde başlangıcı sayılır.
_HEADING = re.compile(
    r"^[ \t]*(?:(?:MADDE|Madde|madde)[ \t]*\d+(?:\.\d+)?|\d{1,2}(?:\.\d{1,2})?[\.\)\-](?=[ \t]))",
    re.MULTILINE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+(?=[A-ZÇĞİÖŞÜ0-9])")


@dataclass
class Clause:
    id: str
    title: str
    text: str
    start: int
    end: int
    preamble: bool = False  # ilk numaralı maddeden önceki başlık/taraf bilgileri

    def to_dict(self):
        return asdict(self)


def _title(text: str) -> str:
    first = text.strip().split("\n", 1)[0].strip()
    return (first[:77] + "…") if len(first) > 80 else first


def _spans_by_heading(text: str):
    starts = [m.start() for m in _HEADING.finditer(text)]
    if len(starts) < 3:
        return None
    spans = []
    if text[: starts[0]].strip():
        spans.append((0, starts[0], True))  # başlık/taraflar bölümü
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        spans.append((s, e, False))
    return spans


def _spans_by_paragraph(text: str):
    spans, pos = [], 0
    for block in re.split(r"\n\s*\n", text):
        idx = text.find(block, pos)
        if idx == -1:
            continue
        spans.append((idx, idx + len(block)))
        pos = idx + len(block)
    return spans if len(spans) >= 3 else None


def _spans_by_sentence(text: str, per_chunk: int = 3):
    bounds = [0] + [m.end() for m in _SENTENCE_END.finditer(text)] + [len(text)]
    spans = []
    for i in range(0, len(bounds) - 1, per_chunk):
        spans.append((bounds[i], bounds[min(i + per_chunk, len(bounds) - 1)]))
    return spans


def split_clauses(text: str) -> list[Clause]:
    spans = _spans_by_heading(text) or _spans_by_paragraph(text) or _spans_by_sentence(text)
    clauses = []
    for span in spans:
        start, end = span[0], span[1]
        preamble = len(span) > 2 and span[2]
        chunk = text[start:end]
        # Baştaki/sondaki boşlukları offset'i bozmadan kırp
        lstrip = len(chunk) - len(chunk.lstrip())
        rstrip = len(chunk) - len(chunk.rstrip())
        start, end = start + lstrip, end - rstrip
        body = text[start:end]
        if len(body) < 3:
            continue
        clauses.append(Clause(id=f"m{len(clauses) + 1}", title=_title(body), text=body, start=start, end=end,
                              preamble=preamble))
    return clauses
