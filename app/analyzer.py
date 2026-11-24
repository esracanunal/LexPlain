"""Analiz akışı: maskeleme → maddelere ayırma → kural motoru → yapay zekâ → birleştirme.

Yapay zekâ çalışmazsa (anahtar yok, kota, zaman aşımı…) kural tabanlı rapor döner ve
kullanıcıya nedeni bildirilir; böylece site hiçbir durumda boş kalmaz.
"""
from . import ai_engine, config, rule_engine
from .privacy import mask_personal_data
from .report import (
    CONTRACT_TYPES, ROLES, WEAK_ROLES, count_by_risk, normalize_role, risk_label, risk_score,
    sort_findings, suggested_questions,
)
from .splitter import split_clauses
from .textutil import normalize_space, tr_lower

LEGAL_NOTICE = ("LexPlain bilgilendirme amaçlıdır, hukuki danışmanlık değildir. "
                "Önemli sözleşmelerde imzadan önce bir avukata danışın.")

_FALLBACK_NOTICES = {
    "unavailable": "Yapay zekâ anahtarı tanımlı olmadığı için temel inceleme (kural tabanlı) gösteriliyor.",
    "timeout": "Yapay zekâ zamanında yanıt vermedi; temel inceleme (kural tabanlı) gösteriliyor. Daha sonra tekrar deneyebilirsiniz.",
    "quota": "Yapay zekâ kullanım sınırına ulaşıldı; temel inceleme (kural tabanlı) gösteriliyor. Daha sonra tekrar deneyebilirsiniz.",
    "busy": "Yapay zekâ şu anda yoğun; temel inceleme (kural tabanlı) gösteriliyor. Biraz sonra tekrar deneyebilirsiniz.",
    "auth": "Yapay zekâ anahtarı geçersiz görünüyor; temel inceleme (kural tabanlı) gösteriliyor.",
    "invalid": "Yapay zekâ anlaşılır bir sonuç üretemedi; temel inceleme (kural tabanlı) gösteriliyor.",
}
_FALLBACK_DEFAULT = "Yapay zekâ incelemesi tamamlanamadı; temel inceleme (kural tabanlı) gösteriliyor."


def _is_strong(finding: dict) -> bool:
    """Yapay zekâ kaçırdığında bile gösterilmeye değer, güçlü kural bulgusu mu?"""
    return finding["risk"] == "yuksek" or finding["gecerlilik"] == "gecersiz_olabilir"


def _same(a: str, b: str) -> bool:
    return tr_lower(a).strip(" .") == tr_lower(b).strip(" .")


def merge(rule_report: dict, ai_result: dict, role: str) -> dict:
    """Yapay zekâ ve kural bulgularını birleştirir.

    Yapay zekâ bulguları esastır. Kural bulgularından yalnızca güçlü olanlar, yapay zekânın hiç
    değinmediği maddelerde (ya da aynı kanun maddesine dayanan bulgusu olmayan maddelerde) eklenir.
    """
    ai_findings = list(ai_result["bulgular"])
    covered = {}  # madde_id -> o maddede yapay zekânın dayandığı kanun kimlikleri
    for f in ai_findings:
        covered.setdefault(f["madde_id"], set())
        if f["yasal_dayanak"]:
            covered[f["madde_id"]].add(f["yasal_dayanak"]["id"])

    findings = ai_findings
    for f in rule_report["bulgular"]:
        if not _is_strong(f):
            continue
        ref_id = f["yasal_dayanak"]["id"] if f["yasal_dayanak"] else None
        if f["madde_id"] not in covered or (ref_id and ref_id not in covered[f["madde_id"]]):
            findings.append(f)
    findings = sort_findings(findings)

    missing = list(rule_report["eksik_maddeler"])
    missing += [m for m in ai_result["eksik_maddeler"] if not any(_same(m["baslik"], x["baslik"]) for x in missing)]

    questions = []
    if role in WEAK_ROLES:
        for q in ai_result["sorulacak_sorular"] + suggested_questions(findings):
            if not any(_same(q, x) for x in questions):
                questions.append(q)
        questions = questions[:5]

    score = risk_score(findings, missing)
    merged = dict(rule_report)
    merged.update({
        "mod": "ai",
        "genel_ozet": ai_result["genel_ozet"] or rule_report["genel_ozet"],
        "risk_puani": score,
        "risk_seviyesi": risk_label(score),
        "sayilar": count_by_risk(findings),
        "bulgular": findings,
        "eksik_maddeler": missing,
        "sorulacak_sorular": questions,
    })
    return merged


def analyze_text(text: str, contract_type: str, role: str, role_note: str = "",
                 mode: str = "otomatik", file_name: str | None = None) -> dict:
    """Sözleşme metnini analiz eder ve arayüzün beklediği raporu döndürür.

    Geçersiz girdide ValueError fırlatır. Yapay zekâ hataları fırlatılmaz; kural raporuna dönülür.
    mode: "otomatik" (yapay zekâ varsa kullan) ya da "kural" (yalnızca kural motoru).
    """
    if contract_type not in CONTRACT_TYPES:
        raise ValueError("Geçersiz sözleşme türü.")
    text = normalize_space(text or "")
    if len(text) < config.MIN_TEXT_CHARS:
        raise ValueError("Metin çok kısa; sözleşmenin tamamını yapıştırın.")
    if len(text) > config.MAX_TEXT_CHARS:
        raise ValueError(f"Metin çok uzun (en fazla {config.MAX_TEXT_CHARS:,} karakter).".replace(",", "."))

    # KVKK: bundan sonraki hiçbir adım (kural motoru, yapay zekâ, rapor) ham metni görmez.
    masked, pii_counts = mask_personal_data(text)
    role = normalize_role(contract_type, role)
    role_note = (role_note or "").strip()[:120]
    clauses = split_clauses(masked)

    report = rule_engine.analyze(masked, clauses, contract_type, role)
    report["uyari"] = None

    if mode == "kural":
        pass
    else:
        try:
            ai_result = ai_engine.analyze_clauses(clauses, contract_type, role, role_note)
            report = merge(report, ai_result, role)
            report["ai_model"] = ai_result["model"]
            report["ai_saglayici"] = ai_result["saglayici"]
            report["uyari"] = None
        except ai_engine.AIError as err:
            report["uyari"] = _FALLBACK_NOTICES.get(err.kind, _FALLBACK_DEFAULT)
        except Exception:  # beklenmeyen hata da kullanıcıyı raporsuz bırakmamalı
            report["uyari"] = _FALLBACK_DEFAULT

    if contract_type == "diger" and role_note:
        report["rol_ad"] = role_note
    else:
        report["rol_ad"] = ROLES[contract_type][role]
    for f in report["bulgular"]:
        f.pop("_pos", None)
    report.update({
        "metin": masked,
        "maddeler": [c.to_dict() for c in clauses],
        "kvkk": pii_counts,
        "dosya_adi": file_name,
        "yasal_uyari": LEGAL_NOTICE,
    })
    return report