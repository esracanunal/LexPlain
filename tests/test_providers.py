"""Sağlayıcı seçimi, hata yönetimi, yedek model ve analiz akışı testleri.

Yapay zekâ çağrıları mock'lanır: internet ve API anahtarı gerekmez, testler saniyeler içinde biter.
"""
import json

import pytest

from app import ai_engine, analyzer, config
from app.splitter import split_clauses

TC = "10000000146"  # algoritma kontrolünden geçen örnek numara
CONTRACT = f"""KONUT KİRA SÖZLEŞMESİ

Kiraya Veren: Ahmet Yılmaz, T.C. Kimlik No: {TC}, Tel: 0532 000 00 00
Kiracı: Elif Demir, e-posta: elif.demir@example.com
Aylık Kira Bedeli: 25.000 TL

MADDE 1 - Kiracı, aylık kira bedelini her ayın 5'ine kadar kiraya verene elden ödeyecektir.

MADDE 2 - Kiracı, sözleşme imzası sırasında 4 (dört) aylık kira bedeli tutarında depozito öder. Depozito, kiralananın tahliyesinde kiraya verenin takdirine göre iade edilir.

MADDE 3 - Kiraya veren, kiralananı haber vermeksizin dilediği zaman kontrol etmek amacıyla girme hakkına sahiptir.
"""
QUOTE = "Depozito, kiralananın tahliyesinde kiraya verenin takdirine göre iade edilir."
# Başlık/taraf bölümü m1 olur; MADDE 1 = m2, MADDE 2 (depozito) = m3, MADDE 3 (girme hakkı) = m4.
DEPOSIT, ENTRY = "m3", "m4"


class FakeAPIError(Exception):
    def __init__(self, status_code, message=""):
        super().__init__(message or f"hata {status_code}")
        self.status_code = status_code


def ai_payload(**override):
    """Modelin döndürebileceği, şemaya uygun örnek çıktı."""
    finding = {
        "madde_id": DEPOSIT, "risk": "yuksek", "kategori": "Depozito",
        "baslik": "Depozito iadesi ev sahibinin takdirine bırakılmış",
        "alinti": QUOTE, "sade": "Depozito iadesine ev sahibi tek başına karar veriyor.",
        "neden": "Paranın tamamı tutulabilir.", "oneri": "İade süresini yazdırın.",
        "alternatif": "Depozito 30 gün içinde iade edilir.",
        "gecerlilik": "gecersiz_olabilir", "yasal_dayanak_id": "TBK_342",
    }
    data = {"genel_ozet": "Depozito maddesi sorunlu.", "bulgular": [finding],
            "eksik_maddeler": [], "sorulacak_sorular": ["Depozito ne zaman iade edilir?"]}
    data.update(override)
    return data


@pytest.fixture
def keys(monkeypatch):
    """Anahtarları ve model listesini bilinen bir duruma getirir."""
    def setup(gemini="", anthropic="", provider=""):
        monkeypatch.setattr(config, "GEMINI_API_KEY", gemini)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", anthropic)
        monkeypatch.setattr(config, "AI_PROVIDER", provider)
        monkeypatch.setattr(config, "GEMINI_MODEL", "ana")
        monkeypatch.setattr(config, "GEMINI_FALLBACK_MODEL", "yedek1,yedek2")
    return setup


def fake_gemini(monkeypatch, behaviors):
    """behaviors: model -> dönecek metin ya da fırlatılacak hata. Çağrılan modelleri kaydeder."""
    calls = []

    def fake(model, system, user, schema, image=None):
        calls.append({"model": model, "user": user, "system": system, "image": image})
        result = behaviors[model]
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, str) else json.dumps(result)

    monkeypatch.setattr(ai_engine, "_gemini_call", fake)
    return calls


# ------------------------------------------------------------------ sağlayıcı seçimi
def test_anahtar_yoksa_yapay_zeka_kapali(keys):
    keys()
    assert config.active_provider() is None
    assert not config.ai_available()


def test_iki_anahtar_varsa_varsayilan_gemini(keys):
    keys(gemini="g", anthropic="a")
    assert config.active_provider() == "gemini"


def test_ai_provider_ile_claude_secilir(keys):
    keys(gemini="g", anthropic="a", provider="anthropic")
    assert config.active_provider() == "anthropic"


def test_secilen_saglayicinin_anahtari_yoksa_kapali(keys):
    keys(gemini="g", provider="anthropic")
    assert config.active_provider() is None


def test_yedek_model_listesi_tekrarsiz(keys, monkeypatch):
    keys(gemini="g")
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODEL", "ana, yedek1,yedek1,")
    assert config.gemini_models() == ["ana", "yedek1"]


def test_anahtar_yoksa_cagri_AIUnavailable_verir(keys):
    keys()
    with pytest.raises(ai_engine.AIUnavailable):
        ai_engine.analyze_clauses(split_clauses(CONTRACT), "kira", "kiraci")


def test_claude_secilince_gemini_cagrilmaz(keys, monkeypatch):
    keys(gemini="g", anthropic="a", provider="anthropic")
    gemini_calls = fake_gemini(monkeypatch, {})
    seen = []

    def fake_claude(model, system, user, schema, image=None):
        seen.append(model)
        return json.dumps(ai_payload())

    monkeypatch.setattr(ai_engine, "_anthropic_call", fake_claude)
    result = ai_engine.analyze_clauses(split_clauses(CONTRACT), "kira", "kiraci")
    assert seen == [config.AI_MODEL] and gemini_calls == []
    assert result["saglayici"] == "anthropic"


# ------------------------------------------------------------------ hata sınıflandırma
def test_hata_siniflandirma():
    assert ai_engine.classify_error(FakeAPIError(429)) == "quota"
    assert ai_engine.classify_error(FakeAPIError(503)) == "busy"
    assert ai_engine.classify_error(FakeAPIError(529, "overloaded")) == "busy"
    assert ai_engine.classify_error(FakeAPIError(401)) == "auth"
    assert ai_engine.classify_error(TimeoutError()) == "timeout"
    assert ai_engine.classify_error(Exception("Request timed out.")) == "timeout"
    assert ai_engine.classify_error(Exception("RESOURCE_EXHAUSTED")) == "quota"
    assert ai_engine.classify_error(ValueError("bilinmeyen")) == "other"


# ------------------------------------------------------------------ yedek model ve hata yönetimi
def test_ana_model_yogunsa_yedege_gecer(keys, monkeypatch):
    keys(gemini="g")
    calls = fake_gemini(monkeypatch, {"ana": FakeAPIError(503), "yedek1": ai_payload()})
    result = ai_engine.analyze_clauses(split_clauses(CONTRACT), "kira", "kiraci")
    assert [c["model"] for c in calls] == ["ana", "yedek1"]
    assert result["model"] == "yedek1"


def test_kota_ve_zaman_asimi_yedege_gecer(keys, monkeypatch):
    keys(gemini="g")
    calls = fake_gemini(monkeypatch, {"ana": FakeAPIError(429), "yedek1": TimeoutError(),
                                      "yedek2": ai_payload()})
    result = ai_engine.analyze_clauses(split_clauses(CONTRACT), "kira", "kiraci")
    assert [c["model"] for c in calls] == ["ana", "yedek1", "yedek2"]
    assert result["model"] == "yedek2"


def test_bozuk_json_yedege_gecer(keys, monkeypatch):
    keys(gemini="g")
    fake_gemini(monkeypatch, {"ana": "bu json değil", "yedek1": ai_payload()})
    assert ai_engine.analyze_clauses(split_clauses(CONTRACT), "kira", "kiraci")["model"] == "yedek1"


def test_anahtar_hatasinda_yedek_denenmez(keys, monkeypatch):
    keys(gemini="g")
    calls = fake_gemini(monkeypatch, {"ana": FakeAPIError(401), "yedek1": ai_payload()})
    with pytest.raises(ai_engine.AIError) as err:
        ai_engine.analyze_clauses(split_clauses(CONTRACT), "kira", "kiraci")
    assert err.value.kind == "auth" and len(calls) == 1


def test_tum_modeller_basarisizsa_son_hata_doner(keys, monkeypatch):
    keys(gemini="g")
    fake_gemini(monkeypatch, {"ana": FakeAPIError(503), "yedek1": FakeAPIError(503),
                              "yedek2": TimeoutError()})
    with pytest.raises(ai_engine.AIError) as err:
        ai_engine.analyze_clauses(split_clauses(CONTRACT), "kira", "kiraci")
    assert err.value.kind == "timeout"


# ------------------------------------------------------------------ çıktı doğrulama
def test_alinti_metinde_bulunur_ve_konum_dogrudur():
    clauses = split_clauses(CONTRACT)
    result = ai_engine.validate_analysis(ai_payload(), clauses)
    f = result["bulgular"][0]
    pos = f["alinti_konum"]
    assert CONTRACT[pos["start"]:pos["end"]] == f["alinti"] == QUOTE
    assert f["kaynak"] == "ai"


def test_alinti_bosluk_farkina_toleransli():
    clauses = split_clauses(CONTRACT)
    item = ai_payload()["bulgular"][0] | {"alinti": QUOTE.replace(" ", "\n", 1)}
    f = ai_engine.validate_analysis(ai_payload(bulgular=[item]), clauses)["bulgular"][0]
    assert QUOTE.startswith(f["alinti"]) and f["alinti_konum"] is not None  # metindeki birebir karşılığı


def test_bulunamayan_alinti_tahmin_edilmez():
    clauses = split_clauses(CONTRACT)
    item = ai_payload()["bulgular"][0] | {"alinti": "Bu cümle sözleşmede hiç geçmiyor."}
    f = ai_engine.validate_analysis(ai_payload(bulgular=[item]), clauses)["bulgular"][0]
    assert f["alinti"] == "" and f["alinti_konum"] is None


def test_uydurma_kanun_atfi_silinir_ve_gecersizlik_iddiasi_dusurulur():
    clauses = split_clauses(CONTRACT)
    item = ai_payload()["bulgular"][0] | {"yasal_dayanak_id": "TBK_9999"}
    f = ai_engine.validate_analysis(ai_payload(bulgular=[item]), clauses)["bulgular"][0]
    assert f["yasal_dayanak"] is None
    assert f["gecerlilik"] == "belirsiz"


def test_dogrulanmis_kanun_atfi_bilgi_tabanindan_gelir():
    f = ai_engine.validate_analysis(ai_payload(), split_clauses(CONTRACT))["bulgular"][0]
    assert f["yasal_dayanak"]["id"] == "TBK_342" and "Borçlar Kanunu" in f["yasal_dayanak"]["kanun"]


def test_gecersiz_madde_ve_bozuk_kayitlar_atilir():
    clauses = split_clauses(CONTRACT)
    good = ai_payload()["bulgular"][0]
    data = ai_payload(bulgular=[good | {"madde_id": "m99"}, good | {"madde_id": ENTRY},
                                "metin", {"madde_id": DEPOSIT}, good | {"baslik": ""}])
    ids = [f["madde_id"] for f in ai_engine.validate_analysis(data, clauses)["bulgular"]]
    assert ids == [ENTRY]  # m99 yok, "metin" bozuk, eksik alanlı ve başlıksız kayıtlar atılır


def test_baslik_bolumu_madde_sayilmaz():
    clauses = split_clauses(CONTRACT)
    assert clauses[0].preamble
    item = ai_payload()["bulgular"][0] | {"madde_id": clauses[0].id}
    assert ai_engine.validate_analysis(ai_payload(bulgular=[item]), clauses)["bulgular"] == []


def test_bilinmeyen_risk_ortaya_cekilir():
    item = ai_payload()["bulgular"][0] | {"risk": "felaket"}
    f = ai_engine.validate_analysis(ai_payload(bulgular=[item]), split_clauses(CONTRACT))["bulgular"][0]
    assert f["risk"] == "orta"


# ------------------------------------------------------------------ soru-cevap ve fotoğraf
def test_soru_cevap_gecerli_maddeleri_dondurur(keys, monkeypatch):
    keys(gemini="g")
    fake_gemini(monkeypatch, {"ana": {"cevap": "Depozito [m3] maddesinde.", "ilgili_maddeler": ["m3", "m77"]}})
    out = ai_engine.ask(CONTRACT, "Depozito ne olur?", "kira", "kiraci")
    assert out["ilgili_maddeler"] == [DEPOSIT] and "[m3]" in out["cevap"]


def test_soru_cevap_kisa_soruyu_reddeder(keys):
    keys(gemini="g")
    with pytest.raises(ValueError):
        ai_engine.ask(CONTRACT, " ", "kira", "kiraci")


def test_fotograftan_metin_okuma(keys, monkeypatch):
    keys(gemini="g")
    text = "MADDE 1 - Kiracı aylık kirayı ödeyecektir. " * 3
    calls = fake_gemini(monkeypatch, {"ana": text})
    assert ai_engine.read_image(b"\xff\xd8fake", "image/jpeg").startswith("MADDE 1")
    assert calls[0]["image"] == (b"\xff\xd8fake", "image/jpeg")


def test_fotograf_turu_ve_bos_metin_kontrolu(keys, monkeypatch):
    keys(gemini="g")
    with pytest.raises(ValueError):
        ai_engine.read_image(b"x", "application/pdf")
    fake_gemini(monkeypatch, {"ana": "kısa"})
    with pytest.raises(ai_engine.AIError):
        ai_engine.read_image(b"x", "image/png")


# ------------------------------------------------------------------ analiz akışı
def test_anahtar_yoksa_kural_raporu_ve_uyari(keys):
    keys()
    r = analyzer.analyze_text(CONTRACT, "kira", "kiraci")
    assert r["mod"] == "kural" and "kural tabanlı" in r["uyari"]
    assert r["bulgular"], "örnek sözleşmede kural bulgusu beklenir"


def test_yapay_zeka_hata_verirse_kural_raporuna_doner(keys, monkeypatch):
    keys(gemini="g")
    fake_gemini(monkeypatch, {"ana": FakeAPIError(503), "yedek1": FakeAPIError(429), "yedek2": TimeoutError()})
    r = analyzer.analyze_text(CONTRACT, "kira", "kiraci")
    assert r["mod"] == "kural" and r["uyari"]
    assert r["risk_puani"] > 0


def test_kisisel_veri_yapay_zekaya_gitmeden_maskelenir(keys, monkeypatch):
    keys(gemini="g")
    calls = fake_gemini(monkeypatch, {"ana": ai_payload()})
    r = analyzer.analyze_text(CONTRACT, "kira", "kiraci")
    sent = calls[0]["user"]
    for secret in (TC, "0532 000 00 00", "elif.demir@example.com"):
        assert secret not in sent and secret not in r["metin"]
    assert r["kvkk"] == {"E-POSTA": 1, "TC_KIMLIK": 1, "TELEFON": 1}


def test_yapay_zeka_bulgulari_birlestirilir(keys, monkeypatch):
    keys(gemini="g")
    fake_gemini(monkeypatch, {"ana": ai_payload()})
    rule_only = analyzer.analyze_text(CONTRACT, "kira", "kiraci", mode="kural")
    r = analyzer.analyze_text(CONTRACT, "kira", "kiraci")
    assert r["mod"] == "ai" and r["uyari"] is None and r["ai_model"] == "ana"
    ai_f = [f for f in r["bulgular"] if f["kaynak"] == "ai"]
    assert len(ai_f) == 1 and ai_f[0]["madde_id"] == DEPOSIT
    # Yapay zekânın değindiği maddede, aynı kanuna dayanan kural bulgusu tekrar eklenmez
    assert [f["madde_id"] for f in r["bulgular"]].count(DEPOSIT) == 1
    # Yapay zekânın değinmediği maddedeki güçlü kural bulguları güvenlik ağı olarak korunur
    strong_entry = [f for f in rule_only["bulgular"]
                    if f["madde_id"] == ENTRY and (f["risk"] == "yuksek" or f["gecerlilik"] == "gecersiz_olabilir")]
    assert strong_entry, "örnek sözleşmede 4. maddede güçlü kural bulgusu beklenir"
    assert {f["id"] for f in strong_entry} <= {f["id"] for f in r["bulgular"]}
    assert r["sayilar"]["yuksek"] == sum(f["risk"] == "yuksek" for f in r["bulgular"])
    assert all("_pos" not in f for f in r["bulgular"])


def test_kural_modu_yapay_zekayi_cagirmaz(keys, monkeypatch):
    keys(gemini="g")
    calls = fake_gemini(monkeypatch, {})
    r = analyzer.analyze_text(CONTRACT, "kira", "kiraci", mode="kural")
    assert r["mod"] == "kural" and calls == []


def test_rapor_arayuzun_bekledigi_alanlari_icerir(keys):
    keys()
    r = analyzer.analyze_text(CONTRACT, "kira", "kiraci", file_name="kira.pdf")
    for key in ("metin", "maddeler", "bulgular", "eksik_maddeler", "sayilar", "risk_puani", "risk_seviyesi",
                "onemli_tarihler", "sorulacak_sorular", "genel_ozet", "rol_ad", "sozlesme_turu",
                "sozlesme_turu_ad", "yasal_uyari", "dosya_adi"):
        assert key in r, key
    assert r["dosya_adi"] == "kira.pdf" and r["maddeler"][0]["id"] == "m1"


def test_diger_turde_serbest_rol_gosterilir(keys):
    keys()
    r = analyzer.analyze_text(CONTRACT, "diger", "taraf", role_note="Alt kiracı")
    assert r["rol_ad"] == "Alt kiracı"


def test_gecersiz_girdiler_reddedilir(keys):
    keys()
    with pytest.raises(ValueError):
        analyzer.analyze_text("kısa", "kira", "kiraci")
    with pytest.raises(ValueError):
        analyzer.analyze_text(CONTRACT, "bilinmeyen", "kiraci")