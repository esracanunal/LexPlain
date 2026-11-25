"""Analiz akışını yönetir: maskeleme → maddelere ayırma → yapay zekâ + kural motoru → birleşik rapor."""
import logging
import re

from . import ai_engine, rule_engine
from .privacy import mask_personal_data
from .report import (
    CONTRACT_TYPES, ROLES, count_by_risk, detect_contract_type, normalize_role,
    risk_label, risk_score, sort_findings, suggested_questions,
)
from .splitter import split_clauses
from .textutil import normalize_space, tr_lower

log = logging.getLogger("sade.analyzer")

DISCLAIMER = (
    "LexPlain bilgilendirme amaçlıdır ve hukuki danışmanlık yerine geçmez. Önemli kararlar ve anlaşmazlıklar "
    "için bir avukata ya da baronuzun adli yardım bürosuna başvurun."
)
# Kullanıcıya gösterilen mesajlar teknik ayrıntı içermez; ayrıntılar sunucu kayıtlarına yazılır.
MSG_DETAILED_UNAVAILABLE = (
    "Ayrıntılı inceleme şu an tamamlanamadı. Aşağıda temel kontrol sonuçları yer alıyor; "
    "birkaç dakika sonra yeniden inceletebilirsiniz."
)
MSG_OTHER_BASIC_ONLY = (
    "Bu sözleşme türü için ayrıntılı inceleme şu an yapılamadı. Yalnızca her sözleşmede görülebilen "
    "genel riskli ifadeler kontrol edildi."
)


def prepare(text: str, contract_type: str = "otomatik", role: str = "otomatik"):
    clean = normalize_space(text)
    masked, mask_counts = mask_personal_data(clean)
    ctype = contract_type if contract_type in CONTRACT_TYPES else detect_contract_type(masked)
    role = normalize_role(ctype, role)
    clauses = split_clauses(masked)
    return masked, mask_counts, ctype, role, clauses


def _merge(ai_report: dict, rule_report: dict) -> list:
    """Yapay zekâ bulgularına, yalnızca yüksek güvenli kural bulgularını ekler.

    Kural bulgusu eklenir: yapay zekâ o maddeye hiç değinmemişse ve bulgu kanuna dayalı
    ("yasaya aykırı olabilir") ya da yüksek riskliyse. Böylece kurallar güvenlik ağı olur ama
    yaygın/standart maddeler sonuca gürültü olarak eklenmez.
    """
    findings = list(ai_report["bulgular"])
    ai_clauses = {f["madde_id"] for f in findings}
    for f in rule_report["bulgular"]:
        if f["madde_id"] in ai_clauses:
            continue
        if f["gecerlilik"] == "gecersiz_olabilir" or f["risk"] == "yuksek":
            findings.append(f)
    return sort_findings(findings)


def analyze(text: str, contract_type: str = "otomatik", role: str = "otomatik", mode: str = "otomatik",
            role_note: str = "") -> dict:
    role_note = (role_note or "").strip()[:120]
    masked, mask_counts, ctype, role, clauses = prepare(text, contract_type, role)
    rule_report = rule_engine.analyze(masked, clauses, ctype, role)

    use_ai = mode != "kural" and ai_engine.is_enabled()
    warning = None
    report = rule_report
    if mode == "ai" and not ai_engine.is_enabled():
        warning = MSG_DETAILED_UNAVAILABLE

    if use_ai:
        try:
            ai = ai_engine.analyze(clauses, ctype, role, role_note)
            findings = _merge(ai, rule_report)
            missing = ai["eksik_maddeler"] or rule_report["eksik_maddeler"]
            # Hesaplanan hatırlatma tarihleri (kural motoru) + yapay zekânın bulduğu tarihler
            dates = rule_report["onemli_tarihler"] + [
                d for d in ai["onemli_tarihler"]
                if d["tarih"] not in {x["tarih"] for x in rule_report["onemli_tarihler"] if x["tarih"]}
            ]
            score = risk_score(findings, missing)
            report = {
                **rule_report,
                "mod": "ai",
                "genel_ozet": ai["genel_ozet"] or rule_report["genel_ozet"],
                "risk_puani": score,
                "risk_seviyesi": risk_label(score),
                "sayilar": count_by_risk(findings),
                "bulgular": findings,
                "eksik_maddeler": missing,
                "onemli_tarihler": dates,
                "sorulacak_sorular": ai["sorulacak_sorular"] or suggested_questions(findings),
            }
        except ai_engine.AIError as exc:
            log.warning("Ayrıntılı inceleme yapılamadı, temel kontrol gösteriliyor: %s", exc)
            warning = MSG_DETAILED_UNAVAILABLE

    if report["mod"] == "kural" and ctype == "diger" and not warning:
        warning = MSG_OTHER_BASIC_ONLY
    if role_note:
        report["rol_ad"] = f"{report['rol_ad']}: {role_note}" if ctype != "diger" else role_note

    for f in report["bulgular"]:
        f.pop("_pos", None)

    return {
        **report,
        "uyari": warning,
        "maskelenen_veriler": mask_counts,
        "metin": masked,
        "maddeler": [c.to_dict() for c in clauses],
        "roller": ROLES[ctype],
        "yasal_uyari": DISCLAIMER,
    }


def answer_question(text: str, question: str, contract_type: str, role: str, history: list,
                    role_note: str = "") -> dict:
    masked, _, ctype, role, clauses = prepare(text, contract_type, role)
    if ai_engine.is_enabled():
        try:
            answer = ai_engine.ask(question, clauses, ctype, role, history, (role_note or "").strip()[:120])
            return {"cevap": answer, "mod": "ai", "ilgili_maddeler": _cited_ids(answer, clauses)}
        except ai_engine.AIError as exc:
            log.warning("Soru yapay zekâ ile cevaplanamadı: %s", exc)
            return _keyword_answer(question, clauses, masked, ctype, role)
    return _keyword_answer(question, clauses, masked, ctype, role)


def _cited_ids(answer: str, clauses) -> list:
    valid = {c.id for c in clauses}
    return [i for i in dict.fromkeys(re.findall(r"\[(m\d+)\]", answer)) if i in valid]


def _stem(word: str) -> str:
    return word[:5]


def _keyword_answer(question: str, clauses, masked: str, ctype: str, role: str) -> dict:
    """Yapay zekâ yokken: soruyla en çok kelime örtüşen maddeleri bulur."""
    stop = {"nasıl", "neden", "nedir", "olur", "olursa", "benim", "bana", "acaba", "mıyım", "miyim", "zaman", "kadar", "hangi"}
    words = {_stem(w) for w in re.findall(r"[a-zçğıöşü]{4,}", tr_lower(question)) if w not in stop}
    lows = {c.id: tr_lower(c.text) for c in clauses}
    # Nadir kelimeler daha değerli (basit IDF ağırlığı): "kombi" tek maddede geçer, "öder" birçok maddede.
    df = {w: sum(1 for low in lows.values() if w in low) for w in words}
    scored = []
    for c in clauses:
        score = sum(1 / df[w] for w in words if df[w] and w in lows[c.id])
        if score:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    top = [c for s, c in scored[:2] if s >= scored[0][0] * 0.5] if scored else []
    if not top:
        return {
            "cevap": "Sorunuzla doğrudan ilgili bir madde bulunamadı. Sorunuzu sözleşmedeki kelimelerle "
                     "yeniden yazmayı deneyin.",
            "mod": "kural", "ilgili_maddeler": [],
        }
    ids = " ".join(f"[{c.id}]" for c in top)
    answer = f"Bu konu sözleşmede şu maddelerde geçiyor: {ids}"
    findings = rule_engine.analyze(masked, clauses, ctype, role)["bulgular"]
    if findings:
        notes = [f"{f['sade']} {f['oneri']}" for f in findings if f["madde_id"] == top[0].id][:1]
        if notes:
            answer += "\n\n" + notes[0]
    answer += "\n\nBu cevap, sorunuzdaki kelimelerin geçtiği maddelere göre hazırlandı."
    return {
        "cevap": answer,
        "mod": "kural",
        "ilgili_maddeler": [c.id for c in top],
    }
