"""Doğrulanmış mevzuat referansları ve kural tabanı (JSON dosyalarından yüklenir).

Yapay zekâ kanun maddesi uyduramasın diye, raporlarda yalnızca buradaki referans
kimlikleri kullanılır. Bilgi tabanını güncellemek için app/data/*.json dosyalarını düzenleyin.
"""
import json
import re
from functools import lru_cache

from .config import BASE_DIR

DATA_DIR = BASE_DIR / "app" / "data"


@lru_cache(maxsize=1)
def legal_refs() -> dict:
    return json.loads((DATA_DIR / "legal_refs.json").read_text(encoding="utf-8"))


def get_ref(ref_id):
    if not ref_id:
        return None
    ref = legal_refs().get(ref_id)
    return {"id": ref_id, **ref} if ref else None


@lru_cache(maxsize=1)
def _raw_rules() -> dict:
    return json.loads((DATA_DIR / "rules.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def rules() -> list:
    """Kuralları düzenli ifadeleri derlenmiş hâlde döndürür."""
    compiled = []
    for rule in _raw_rules()["kurallar"]:
        r = dict(rule)
        r["_groups"] = [[re.compile(p) for p in group] for group in rule["desenler"]]
        r["_haric"] = re.compile(rule["haric"]) if rule.get("haric") else None
        compiled.append(r)
    return compiled


@lru_cache(maxsize=1)
def missing_checks() -> list:
    checks = []
    for item in _raw_rules()["eksik_kontrolleri"]:
        c = dict(item)
        c["_kosul"] = re.compile(item["kosul"]) if item.get("kosul") else None
        c["_aranan"] = re.compile(item["aranan"])
        checks.append(c)
    return checks
