"""Kural tabanlı analiz motoru.

İki görevi var:
1. API anahtarı yokken uygulamanın tek başına çalışmasını sağlamak (çevrimdışı mod).
2. Yapay zekâ modunda güvenlik ağı olmak: yapay zekânın kaçırdığı bilinen riskli kalıpları eklemek.
"""
import re

from . import knowledge
from .report import (
    CONTRACT_TYPES, ROLES, WEAK_ROLES, count_by_risk, extract_dates, risk_label,
    risk_score, sort_findings, suggested_questions,
)
from .splitter import Clause
from .textutil import parse_tr_number, tr_lower

_SENT_BOUNDARY = re.compile(r"[.;:!?\n]")
_NUM = r"(\d+|bir|iki|üç|dört|beş|altı|yedi|sekiz|dokuz|on)"
_DEPOSIT_MONTHS = [
    re.compile(r"(depozito|güvence bedeli|teminat)[^.;]{0,90}?" + _NUM + r"\s*(?:\(\s*[\wçğıöşü]+\s*\)\s*)?aylık"),
    re.compile(_NUM + r"\s*(?:\(\s*[\wçğıöşü]+\s*\)\s*)?aylık kira[^.;]{0,40}?(depozito|güvence|teminat)"),
]
_PERCENT_INCREASE = re.compile(r"(%\s?\d+|yüzde\s?\d+)[^.;]{0,60}(artış|artırıl|\bzam\b|zamm)|(artış|artırıl|\bzam\b|zamm)[^.;]{0,60}(%\s?\d+|yüzde\s?\d+)")


def _sentence_around(text: str, pos: int, max_len: int = 260):
    """pos konumundaki karakteri içeren cümleyi ve metindeki (başlangıç, bitiş) konumunu döndürür.

    Dönen konum, gösterilen alıntının metinde birebir karşılığıdır; böylece arayüz yalnızca
    doğru bölümü işaretleyebilir.
    """
    left = max((m.end() for m in _SENT_BOUNDARY.finditer(text, 0, pos)), default=0)
    m = _SENT_BOUNDARY.search(text, pos)
    right = m.end() if m else len(text)
    # baştaki/sondaki boşlukları konumu bozmadan kırp
    while left < right and text[left].isspace():
        left += 1
    while right > left and text[right - 1].isspace():
        right -= 1
    if right - left > max_len:
        left = max(left, pos - max_len // 2)
        right = min(right, left + max_len)
        # kelime ortasından başlama/bitme
        while left > 0 and not text[left - 1].isspace() and left < pos:
            left += 1
        while right < len(text) and not text[right].isspace() and right > pos:
            right -= 1
    return text[left:right], left, right


_SENTENCE_SPLIT = re.compile(r"[.;!?](?=\s|$)|\n\s*\n")


def _sentence_spans(text: str):
    """Metni cümlelere böler; (başlangıç, bitiş) konumlarını döndürür."""
    spans, start = [], 0
    for m in _SENTENCE_SPLIT.finditer(text):
        spans.append((start, m.end()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _match_rule(rule, low: str):
    """Kuralın bir desen grubundaki TÜM ifadeler AYNI cümlede geçiyorsa o cümledeki konumu döndürür.

    Ayrı cümlelerdeki kelimeleri birleştirmek (ör. elektrik faturası cezası + kira) yanlış alarma
    yol açtığı için eşleşme cümle düzeyinde aranır. "haric" ifadesi cümlede geçiyorsa (ör. maddenin
    aslında kullanıcı lehine olduğu "kiracı sorumlu değildir") kural uygulanmaz.
    """
    for s_start, s_end in _sentence_spans(low):
        sentence = low[s_start:s_end]
        if rule.get("_haric") and rule["_haric"].search(sentence):
            continue
        for group in rule["_groups"]:
            first = None
            for rx in group:
                m = rx.search(sentence)
                if not m:
                    break
                first = m.start() if first is None else first
            else:
                return s_start + first
    return None


def _finding(rule_like: dict, clause: Clause, pos: int, role: str) -> dict:
    ref = knowledge.get_ref(rule_like.get("ref"))
    f = {
        "id": f"{rule_like['id']}-{clause.id}",
        "madde_id": clause.id,
        "madde_baslik": clause.title,
        "risk": rule_like["risk"],
        "kategori": rule_like["kategori"],
        "baslik": rule_like["baslik"],
        "alinti": "",
        "alinti_konum": None,
        "sade": rule_like["sade"],
        "neden": rule_like["neden"],
        "oneri": rule_like["oneri"],
        "alternatif": rule_like.get("alternatif"),
        "yasal_dayanak": ref,
        "gecerlilik": rule_like.get("gecerlilik", "belirsiz"),
        "kaynak": "kural",
        "_pos": clause.start + pos,
    }
    quote, q_start, q_end = _sentence_around(clause.text, pos)
    f["alinti"] = quote
    f["alinti_konum"] = {"start": clause.start + q_start, "end": clause.start + q_end}
    if role not in WEAK_ROLES:
        # Güçlü taraf (ev sahibi / firma) için: kanuna aykırı madde koruma sağlamaz.
        f["risk"] = "orta"
        f["sade"] = "Bu madde lehinize yazılmış görünüyor, ancak kanuna aykırı olabilir. " + f["sade"]
        f["neden"] = (
            "Uyuşmazlıkta mahkeme bu maddeyi geçersiz sayarsa ona dayanarak hak talep edemezsiniz. "
            + (ref["ozet"] if ref else "")
        ).strip()
        f["oneri"] = "Bu maddeye güvenmeyin; yasal sınırlar içinde yeniden yazın."
    return f


_PAREN = r"\s*(?:\(\s*[\wçğıöşü]+\s*\)\s*)?"
# Sayı, anahtar kelimeden önce de sonra da gelebilir ("4 aylık deneme süresi" / "deneme süresi 4 aydır")
_PROBATION = [
    re.compile(r"deneme (?:süresi|kaydı|dönemi)[^.;]{0,60}?" + _NUM + _PAREN + r"(ay|hafta)"),
    re.compile(_NUM + _PAREN + r"(ay|hafta)\w*[^.;]{0,25}?deneme"),
]
_NONCOMPETE_YEARS = [
    re.compile(r"(?:rekabet|rakip)[^.;]{0,120}?" + _NUM + _PAREN + r"(yıl|sene)"),
    re.compile(_NUM + _PAREN + r"(yıl|sene)\w*[^.;]{0,140}?(?:rekabet|rakip)"),
]
_WEEKLY_HOURS = re.compile(r"haftalık[^.;]{0,30}?(\d{2})\s*saat|haftada[^.;]{0,20}?(\d{2})\s*saat")


def _first_number(m):
    return next((parse_tr_number(g) for g in m.groups() if g and parse_tr_number(g) is not None), None)


def _employment_checks(clause: Clause, low: str, role: str) -> list:
    out = []
    m = next((x for x in (rx.search(low) for rx in _PROBATION) if x), None)
    if m:
        n = _first_number(m)
        months = n if m.group(m.lastindex) == "ay" else (n or 0) / 4.3
        if n and months > 2:
            out.append(_finding({
                "id": "deneme_suresi_uzun", "risk": "yuksek", "kategori": "Deneme süresi",
                "baslik": f"Deneme süresi yasal sınırın üstünde ({n} {m.group(m.lastindex)})",
                "sade": f"Deneme süreniz {n} {m.group(m.lastindex)} olarak yazılmış. Bu sürede işveren sizi tazminatsız ve haber vermeden çıkarabilir.",
                "neden": "Deneme süresi en çok iki ay olabilir (yalnızca toplu iş sözleşmesiyle dört aya kadar). Fazlası geçersizdir; iki ay dolunca deneme süresi biter.",
                "oneri": "Deneme süresinin en fazla 2 ay olarak düzeltilmesini isteyin.",
                "alternatif": "İş sözleşmesinde 2 (iki) aylık deneme süresi uygulanır.",
                "ref": "IK_15", "gecerlilik": "gecersiz_olabilir",
            }, clause, m.start(), role))
    m = next((x for x in (rx.search(low) for rx in _NONCOMPETE_YEARS) if x), None)
    if m:
        n = _first_number(m)
        if n and n > 2:
            out.append(_finding({
                "id": "rekabet_suresi_uzun", "risk": "yuksek", "kategori": "Rekabet yasağı",
                "baslik": f"Rekabet yasağı {n} yıl: kural olarak sınır 2 yıl",
                "sade": f"İşten ayrıldıktan sonra {n} yıl boyunca rakip işlerde çalışamayacağınız yazıyor.",
                "neden": "Rekabet yasağının süresi özel durumlar dışında iki yılı aşamaz; hâkim aşırı yasağı daraltabilir.",
                "oneri": "Süreyi en fazla 1-2 yıla indirin; şehir ve iş alanını da sınırlayın.",
                "alternatif": None, "ref": "TBK_445", "gecerlilik": "gecersiz_olabilir",
            }, clause, m.start(), role))
    m = _WEEKLY_HOURS.search(low)
    if m:
        n = _first_number(m)
        if n and n > 45:
            out.append(_finding({
                "id": "haftalik_saat", "risk": "orta", "kategori": "Fazla mesai",
                "baslik": f"Haftalık {n} saat çalışma: sınır 45 saat",
                "sade": f"Haftada {n} saat çalışmanız bekleniyor.",
                "neden": "Haftalık çalışma süresi en çok 45 saattir; aşan her saat fazla çalışmadır ve en az yüzde elli zamlı ödenmelidir.",
                "oneri": "Fazla saatlerin fazla mesai olarak ayrıca ödeneceğini yazdırın.",
                "alternatif": None, "ref": "IK_41", "gecerlilik": "belirsiz",
            }, clause, m.start(), role))
    return out


def _custom_checks(contract_type: str, clause: Clause, low: str, role: str) -> list:
    out = []
    if contract_type == "is":
        return _employment_checks(clause, low, role)
    if contract_type != "kira":
        return out
    for rx in _DEPOSIT_MONTHS:
        m = rx.search(low)
        if not m:
            continue
        months = next((parse_tr_number(g) for g in m.groups() if parse_tr_number(g or "") is not None), None)
        if months and months > 3:
            out.append(_finding({
                "id": "depozito_fazla", "risk": "yuksek", "kategori": "Depozito",
                "baslik": f"Depozito yasal sınırın üstünde ({months} aylık kira)",
                "sade": f"Sizden {months} aylık kira tutarında depozito isteniyor.",
                "neden": "Konut kiralarında güvence bedeli üç aylık kira bedelini aşamaz; aşan kısım için talep geçersizdir.",
                "oneri": "Depozitoyu en fazla 3 aylık kiraya düşürün ve vadeli banka hesabına yatırılmasını isteyin.",
                "alternatif": "Kiracı, üç aylık kira bedelini aşmamak üzere ……… TL güvence bedelini, kiracı adına açılacak vadeli tasarruf hesabına yatırır.",
                "ref": "TBK_342", "gecerlilik": "gecersiz_olabilir",
            }, clause, m.start(), role))
            break
    m = _PERCENT_INCREASE.search(low)
    if m and "tüfe" not in low and "üfe" not in low:
        out.append(_finding({
            "id": "kira_artis_yuzde", "risk": "orta", "kategori": "Kira artışı",
            "baslik": "Sabit yüzdeyle kira artışı",
            "sade": "Kiranın her yıl sabit bir yüzdeyle artırılacağı yazıyor.",
            "neden": "Konutta yenilenen dönem artışı, son 12 aylık TÜFE ortalamasını geçemez. Yazılan oran bu sınırı aşarsa aşan kısım geçersiz olur.",
            "oneri": "Oranı TÜFE ile sınırlayın: 'yazılı oran ile TÜFE 12 aylık ortalamasından düşük olanı'. Kira hesaplayıcıyla kontrol edin.",
            "alternatif": "Yenilenen kira dönemlerinde kira bedeli, %… oranını ve bir önceki kira yılının TÜFE on iki aylık ortalamalara göre değişim oranını geçmemek üzere artırılır.",
            "ref": "TBK_344", "gecerlilik": "belirsiz",
        }, clause, m.start(), role))
    return out


_STRONG_PARTY_MISSING = {
    "kiraya_veren": [
        ("depozito|güvence bedeli|teminat", "Güvence bedeli (depozito) alınmamış",
         "Sözleşmede depozito yok. Kanun 3 aylık kiraya kadar güvence bedeli almanıza izin veriyor."),
        ("kefil", "Kefil bulunmuyor",
         "Kira ödenmezse başvurabileceğiniz bir kefil yok. Kefalet için kefilin azami tutarı el yazısıyla yazması gerekir."),
    ],
    "firma": [
        ("kişisel veri|kvkk|aydınlatma", "KVKK aydınlatma metni yok",
         "Müşteri verilerini işliyorsanız aydınlatma metni ve gerekiyorsa ayrı açık rıza almalısınız."),
    ],
    "isveren": [
        ("gizli|sır", "Gizlilik maddesi yok",
         "Çalışanın ticari sırları ve müşteri bilgilerini koruma yükümlülüğü yazılmamış."),
        ("kişisel veri|kvkk|aydınlatma", "KVKK aydınlatma metni yok",
         "Çalışan verilerini işlediğiniz için aydınlatma metni vermeniz gerekir."),
    ],
}


def _missing(contract_type: str, low_full: str, role: str) -> list:
    out = []
    if role in WEAK_ROLES:
        for c in knowledge.missing_checks():
            if contract_type not in c["turler"]:
                continue
            if c["_kosul"] and not c["_kosul"].search(low_full):
                continue
            if c["id"] == "eksik_depozito_iade":
                # depozito geçiyor ama 'iade' kelimesi depozito ile aynı bağlamda yoksa
                hits = [m.start() for m in c["_kosul"].finditer(low_full)]
                if any(c["_aranan"].search(low_full[max(0, h - 200): h + 250]) for h in hits):
                    continue
            elif c["_aranan"].search(low_full):
                continue
            out.append({"baslik": c["baslik"], "aciklama": c["aciklama"]})
    for pattern, title, desc in _STRONG_PARTY_MISSING.get(role, []):
        if not re.search(pattern, low_full):
            out.append({"baslik": title, "aciklama": desc})
    return out


def analyze(text: str, clauses: list, contract_type: str, role: str) -> dict:
    findings = []
    fired_once = set()
    for clause in clauses:
        if clause.preamble:
            continue  # başlık, taraf ve form alanları madde değildir
        low = tr_lower(clause.text)
        findings.extend(_custom_checks(contract_type, clause, low, role))
        for rule in knowledge.rules():
            if contract_type not in rule["turler"]:
                continue
            # Güçlü taraf için yalnızca "kanuna aykırı olabilir" kuralları anlamlıdır
            if role not in WEAK_ROLES and rule.get("gecerlilik") != "gecersiz_olabilir":
                continue
            if rule.get("tek_sefer") and rule["id"] in fired_once:
                continue  # yaygın maddeler sözleşme başına bir kez gösterilir
            pos = _match_rule(rule, low)
            if pos is not None:
                findings.append(_finding(rule, clause, pos, role))
                fired_once.add(rule["id"])
    # Daha özel bir bulgu aynı maddede genel olanı gereksiz kılar
    superseded = {"rekabet_yasagi": "rekabet_suresi_uzun"}
    present = {(f["madde_id"], f["id"].split("-")[0]) for f in findings}
    findings = [f for f in findings
                if (f["madde_id"], superseded.get(f["id"].split("-")[0], "")) not in present]
    findings = sort_findings(findings)

    low_full = tr_lower(text)
    missing = _missing(contract_type, low_full, role)
    score = risk_score(findings, missing)
    counts = count_by_risk(findings)
    return {
        "mod": "kural",
        "sozlesme_turu": contract_type,
        "sozlesme_turu_ad": CONTRACT_TYPES[contract_type],
        "rol": role,
        "rol_ad": ROLES[contract_type][role],
        "genel_ozet": _summary(contract_type, role, len(clauses), counts, missing, findings),
        "risk_puani": score,
        "risk_seviyesi": risk_label(score),
        "sayilar": counts,
        "bulgular": findings,
        "eksik_maddeler": missing,
        "onemli_tarihler": extract_dates(text, contract_type),
        "sorulacak_sorular": suggested_questions(findings) if role in WEAK_ROLES else [],
    }


def _summary(contract_type, role, n_clauses, counts, missing, findings) -> str:
    """Karar kartındaki kısa, sade özet. Sayılar arayüzde ayrıca gösterildiği için tekrar edilmez."""
    if not findings:
        return ("Kontrolde dikkat gerektiren bir ifade bulunamadı. İmzalamadan önce metni yine de "
                "dikkatle okuyun.")
    invalid = sum(1 for f in findings if f["gecerlilik"] == "gecersiz_olabilir")
    if role not in WEAK_ROLES:
        return (f"{invalid} madde yasaya aykırı olabilir. Bir anlaşmazlıkta bu maddelere dayanamayabilirsiniz; "
                "yasal sınırlar içinde yeniden yazmanız önerilir.")
    top = findings[0]["baslik"].rstrip(".")
    parts = [f"En önemli sorun: {tr_lower(top[0]) + top[1:]}."]
    if invalid:
        parts.append(f"{invalid} madde yasaya aykırı olabilir; imzalasanız bile sizi bağlamayabilir.")
    if missing:
        parts.append(f"Ayrıca sizi koruyan {len(missing)} hüküm sözleşmede yok.")
    return " ".join(parts)
