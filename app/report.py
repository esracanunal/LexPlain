"""Rapor yapısı için ortak yardımcılar: sözleşme türü/rol tespiti, risk puanı, tarih çıkarımı."""
import re
from datetime import date, timedelta

from .textutil import tr_lower

CONTRACT_TYPES = {
    "kira": "Kira sözleşmesi",
    "hizmet": "Hizmet / abonelik sözleşmesi",
    "is": "İş sözleşmesi",
    "diger": "Diğer sözleşme",
}
ROLES = {
    "kira": {"kiraci": "Kiracı", "kiraya_veren": "Kiraya veren (ev sahibi)"},
    "hizmet": {"musteri": "Müşteri / tüketici", "firma": "Hizmet veren firma"},
    "is": {"calisan": "Çalışan", "isveren": "İşveren"},
    "diger": {"taraf": "Sözleşmenin tarafı"},
}
# "Zayıf taraf" rolleri: kurallar bu rollerin aleyhine olan maddeleri arar.
WEAK_ROLES = {"kiraci", "musteri", "calisan", "taraf"}
STRONG_FOR = {"kira": "kiraya_veren", "hizmet": "firma", "is": "isveren", "diger": "taraf"}
WEAK_FOR = {"kira": "kiraci", "hizmet": "musteri", "is": "calisan", "diger": "taraf"}
RISK_ORDER = {"yuksek": 0, "orta": 1, "dusuk": 2}
RISK_WEIGHT = {"yuksek": 18, "orta": 7, "dusuk": 1}
MISSING_WEIGHT = 3
SCORE_SCALE = 60  # bu ham puanda risk puanı ~63 olur

_TYPE_WORDS = {
    "kira": re.compile(r"kiracı|kiraya veren|kiralanan|mecur|kira bedeli|kira sözleşmesi"),
    "hizmet": re.compile(r"hizmet|abone|üyelik|müşteri|paket|taahhüt|tüketici"),
    "is": re.compile(r"işçi|işveren|iş sözleşmesi|personel|çalışan|maaş|mesai|deneme süresi|kıdem"),
}


def detect_contract_type(text: str) -> str:
    low = tr_lower(text)
    counts = {t: len(rx.findall(low)) for t, rx in _TYPE_WORDS.items()}
    best = max(counts, key=lambda t: counts[t])
    return best if counts[best] >= 2 else "diger"


def normalize_role(contract_type: str, role: str) -> str:
    if role in ROLES[contract_type]:
        return role
    # Rol bu türe ait değilse karşılığını seç: zayıf taraf ↔ zayıf taraf
    weak = not role or role == "otomatik" or role in WEAK_ROLES
    return WEAK_FOR[contract_type] if weak else STRONG_FOR[contract_type]


def risk_score(findings: list, missing: list) -> int:
    """0–100 arası kaba risk göstergesi.

    - Aynı maddeye ait birden fazla bulgu tek sayılır (en yüksek riskli olanı).
    - Ham puan doğrusal toplanmaz; doygunluk eğrisiyle 100'e yaklaşır. Böylece birkaç
      yüksek riskli madde puanı hemen 100'e çıkarmaz, gerçekten çok sorunlu sözleşmeler ayrışır.
    """
    import math

    worst = {}
    for f in findings:
        key = f.get("madde_id")
        w = RISK_WEIGHT.get(f["risk"], 0)
        worst[key] = max(worst.get(key, 0), w)
    raw = sum(worst.values()) + MISSING_WEIGHT * len(missing)
    return round(100 * (1 - math.exp(-raw / SCORE_SCALE)))


def risk_label(score: int) -> str:
    if score >= 70:
        return "Yüksek"
    if score >= 35:
        return "Orta"
    return "Düşük"


def count_by_risk(findings: list) -> dict:
    counts = {"yuksek": 0, "orta": 0, "dusuk": 0}
    for f in findings:
        counts[f["risk"]] = counts.get(f["risk"], 0) + 1
    return counts


def sort_findings(findings: list) -> list:
    return sorted(findings, key=lambda f: (RISK_ORDER.get(f["risk"], 9), f.get("_pos", 0)))


# ---------- Tarih ve süre çıkarımı ----------
_DATE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)")
_DURATION = re.compile(r"(?<!\d)(\d{1,2})\s*(?:\(\s*[a-zçğıöşü ]+\s*\)\s*)?(yıl|sene|ay)(?:lık)?\b")
_START_CTX = re.compile(r"başla|itibaren|yürürlü")
_END_CTX = re.compile(r"bitiş|sona er|bitecek|son bulur|kadar")


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, 28)


def extract_dates(text: str, contract_type: str) -> list:
    """Sözleşmedeki tarihleri ve süreyi bulup kritik hatırlatma tarihlerini hesaplar."""
    low = tr_lower(text)
    items, start, end = [], None, None

    for m in _DATE.finditer(low):
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            continue
        ctx = low[max(0, m.start() - 70): m.start()]
        if _START_CTX.search(ctx) or "başlangıç" in ctx:
            start = start or d
            kind = "Başlangıç tarihi"
        elif _END_CTX.search(ctx) or "bitiş" in ctx:
            end = end or d
            kind = "Bitiş tarihi"
        else:
            kind = "Sözleşmede geçen tarih"
        items.append({"aciklama": kind, "tarih": d.isoformat(), "kaynak": "metin"})

    duration_months = None
    for m in _DURATION.finditer(low):
        ctx = low[max(0, m.start() - 60): m.end() + 30]
        if "süre" in ctx or "müddet" in ctx or "taahhüt" in ctx:
            n = int(m.group(1))
            duration_months = n * 12 if m.group(2) in ("yıl", "sene") else n
            items.append({"aciklama": f"Sözleşme süresi: {m.group(1)} {m.group(2)}", "tarih": None, "kaynak": "metin"})
            break

    if start and not end and duration_months:
        end = _add_months(start, duration_months) - timedelta(days=1)
        items.append({"aciklama": "Hesaplanan bitiş tarihi", "tarih": end.isoformat(), "kaynak": "hesap"})

    if end and contract_type in ("kira", "hizmet"):
        if contract_type == "kira":
            notice = end - timedelta(days=15)
            items.append({
                "aciklama": "Kiracı için son bildirim günü: çıkmak istiyorsanız bu tarihe kadar yazılı bildirim yapın, aksi hâlde sözleşme 1 yıl uzar (TBK m. 347).",
                "tarih": notice.isoformat(), "kaynak": "hesap", "hatirlatma": True,
            })
        else:
            notice = end - timedelta(days=30)
            items.append({
                "aciklama": "Yenileme öncesi kontrol: otomatik yenilemeyi istemiyorsanız iptal talebini bu tarih civarında iletin.",
                "tarih": notice.isoformat(), "kaynak": "hesap", "hatirlatma": True,
            })
    # aynı tarih+açıklama tekrarlarını kaldır
    seen, unique = set(), []
    for it in items:
        key = (it["aciklama"], it["tarih"])
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique


_QUESTIONS = {
    "Depozito": "Depozito hangi hesaba yatırılacak ve çıkışta kaç gün içinde iade edilecek?",
    "Kira artışı": "Kira artışı nasıl hesaplanacak? TÜFE 12 aylık ortalamasını geçmeyeceğini yazılı teyit edebilir misiniz?",
    "Bakım ve onarım": "Kombi, tesisat veya çatı gibi büyük arızalarda masrafı kim karşılayacak?",
    "Erken çıkış": "Sözleşme bitmeden çıkmam gerekirse kaç gün önce haber vermem yeterli olur?",
    "Çıkış masrafı": "Evin mevcut durumunu fotoğraflı bir teslim tutanağıyla kayda geçirebilir miyiz?",
    "Ek ödeme": "Aidat dışında ödemem beklenen başka bir gider var mı? Hepsini yazılı olarak sayabilir misiniz?",
    "Tek taraflı değişiklik": "Fiyat değişikliği olursa ne kadar önceden haber verilecek ve cezasız çıkabilecek miyim?",
    "Taahhüt": "Erken iptal edersem ödeyeceğim tutarı ay ay gösteren bir tablo verebilir misiniz?",
    "Fesih": "Sözleşmeyi hangi kanaldan (e-posta, uygulama, telefon) iptal edebilirim?",
    "Kişisel veri": "Verilerim hangi şirketlerle paylaşılacak? Pazarlama iznini vermemem mümkün mü?",
    "Kefalet": "Kefilin sorumlu olacağı azami tutar ne olacak?",
    "Tahliye": "Tahliye taahhüdü imzalamadan sözleşmeyi yapabilir miyiz?",
    "Fazla mesai": "Fazla mesai nasıl ödenecek? Yüzde elli zamlı mı, serbest zaman olarak mı?",
    "Rekabet yasağı": "Rekabet yasağının süresi, şehri ve kapsadığı işler tam olarak ne? Bu süre için bir ödeme yapılacak mı?",
    "Eğitim gideri": "Eğitim masrafının tutarı ne kadar ve çalıştığım süreye göre azalıyor mu?",
    "Ücret kesintisi": "Maaştan hangi durumlarda ve en fazla ne kadar kesinti yapılabilir?",
    "Yıllık izin": "Yıllık izin hakkım kaç gün ve nasıl kullanacağım?",
    "Görev ve yer değişikliği": "Başka bir şehre veya göreve gönderilirsem ne kadar önceden haber verilecek, masrafları kim karşılayacak?",
    "Cezai şart": "Bu ceza karşılıklı mı? İşveren sözleşmeyi bozarsa o da aynı tutarı öder mi?",
    "Deneme süresi": "Deneme süresi tam olarak kaç ay? İki ayı aşan kısmı sözleşmeden çıkarabilir miyiz?",
}


def suggested_questions(findings: list, limit: int = 5) -> list:
    out = []
    for f in findings:
        q = _QUESTIONS.get(f.get("kategori"))
        if q and q not in out:
            out.append(q)
        if len(out) >= limit:
            break
    return out
