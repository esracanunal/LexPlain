"""KVKK: Metindeki kişisel verileri analizden ve yapay zekâya gönderilmeden önce maskeler."""
import re

# Sıra önemli: IBAN içindeki rakamlar telefon/TC olarak yakalanmasın diye önce IBAN.
_PATTERNS = [
    ("IBAN", re.compile(r"\bTR\s?\d{2}(?:\s?\d{4}){5}\s?\d{2}\b", re.IGNORECASE)),
    ("E-POSTA", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("TC_KIMLIK", re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")),
    ("TELEFON", re.compile(r"(?<!\d)(?:\+?90[\s-]?)?0?\(?5\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)")),
]


def is_valid_tckn(value: str) -> bool:
    """T.C. kimlik numarası algoritma kontrolü."""
    if len(value) != 11 or not value.isdigit() or value[0] == "0":
        return False
    d = [int(c) for c in value]
    if ((sum(d[0:9:2]) * 7) - sum(d[1:8:2])) % 10 != d[9]:
        return False
    return sum(d[:10]) % 10 == d[10]


def mask_personal_data(text: str):
    """Kişisel verileri [ETİKET] ile değiştirir. (maskeli_metin, {etiket: adet}) döner."""
    counts: dict[str, int] = {}

    for label, pattern in _PATTERNS:
        def _repl(m, label=label):
            if label == "TC_KIMLIK" and not is_valid_tckn(m.group(0)):
                return m.group(0)
            counts[label] = counts.get(label, 0) + 1
            return f"[{label}]"

        text = pattern.sub(_repl, text)
    return text, counts
