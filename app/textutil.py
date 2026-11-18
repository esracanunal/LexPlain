"""Türkçe metin yardımcıları."""
import re

_TR_UPPER_MAP = str.maketrans({"I": "ı", "İ": "i"})


def tr_lower(text: str) -> str:
    """Türkçe kurallarına uygun küçük harfe çevirme (I→ı, İ→i)."""
    return text.translate(_TR_UPPER_MAP).lower()


def normalize_space(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_WORD_NUMBERS = {
    "bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6,
    "yedi": 7, "sekiz": 8, "dokuz": 9, "on": 10, "on iki": 12,
}


def parse_tr_number(token: str):
    """'3', 'üç', '(3)', 'üç (3)' gibi ifadeleri sayıya çevirir; çeviremezse None."""
    token = tr_lower(token).strip(" ()")
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)
