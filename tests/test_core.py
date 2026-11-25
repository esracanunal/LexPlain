"""Çekirdek modüllerin birim testleri: maskeleme, maddelere ayırma, kural motoru, tarih çıkarımı."""
from pathlib import Path

import pytest

from app import analyzer, rule_engine
from app.privacy import is_valid_tckn, mask_personal_data
from app.report import detect_contract_type, extract_dates
from app.splitter import split_clauses
from app.textutil import tr_lower

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
KIRA = (SAMPLES / "ornek_kira_sozlesmesi.txt").read_text(encoding="utf-8")
HIZMET = (SAMPLES / "ornek_hizmet_sozlesmesi.txt").read_text(encoding="utf-8")


# ---------------- metin yardımcıları ----------------
def test_tr_lower_keeps_turkish_letters_and_length():
    s = "KİRACI İŞBU MADDEYİ IŞIK"
    assert tr_lower(s) == "kiracı işbu maddeyi ışık"
    assert len(tr_lower(s)) == len(s)  # offset'ler bozulmamalı


# ---------------- KVKK maskeleme ----------------
def test_tckn_validation():
    assert is_valid_tckn("10000000146")
    assert not is_valid_tckn("12345678901")


def test_masking_replaces_personal_data():
    text = "TC: 10000000146 tel 0532 000 00 00 mail a.b@example.com IBAN TR33 0006 1005 1978 6457 8413 26"
    masked, counts = mask_personal_data(text)
    assert "10000000146" not in masked and "[TC_KIMLIK]" in masked
    assert "[TELEFON]" in masked and "[E-POSTA]" in masked and "[IBAN]" in masked
    assert counts == {"TC_KIMLIK": 1, "TELEFON": 1, "E-POSTA": 1, "IBAN": 1}


def test_masking_ignores_invalid_tckn_and_money():
    masked, counts = mask_personal_data("Bedel 25.000 TL, numara 12345678901")
    assert "12345678901" in masked
    assert counts == {}


# ---------------- maddelere ayırma ----------------
def test_split_by_madde_headings_keeps_offsets():
    clauses = split_clauses(KIRA)
    assert len(clauses) >= 15
    for c in clauses:
        assert KIRA[c.start:c.end] == c.text
    assert clauses[1].text.startswith("MADDE 1")


def test_split_numbered_list():
    clauses = split_clauses(HIZMET)
    assert any(c.text.startswith("4.") for c in clauses)


def test_split_falls_back_to_sentences():
    text = "Bu bir cümle. Bu ikinci cümle. Üçüncü cümle burada. Dördüncü cümle de var."
    clauses = split_clauses(text)
    assert len(clauses) >= 2
    assert "".join(c.text for c in clauses).replace(" ", "") == text.replace(" ", "")


# ---------------- tür tespiti ----------------
def test_detect_contract_type():
    assert detect_contract_type(KIRA) == "kira"
    assert detect_contract_type(HIZMET) == "hizmet"


# ---------------- kural motoru ----------------
@pytest.fixture(scope="module")
def kira_report():
    return analyzer.analyze(KIRA, "otomatik", "otomatik", "kural")


def _titles(report):
    return {f["baslik"] for f in report["bulgular"]}


def test_kira_detects_key_risks(kira_report):
    ids = {f["id"].split("-")[0] for f in kira_report["bulgular"]}
    for expected in ["depozito_fazla", "depozito_takdir", "kira_artis_tek_tarafli", "cezai_sart_gec_odeme",
                     "muacceliyet", "tum_tamirat_kiraci", "erken_tahliye_ceza", "tahliye_taahhudu",
                     "habersiz_giris", "boya_badana", "sorumluluk_reddi", "genel_feragat"]:
        assert expected in ids, expected


def test_findings_are_sorted_by_risk(kira_report):
    order = {"yuksek": 0, "orta": 1, "dusuk": 2}
    ranks = [order[f["risk"]] for f in kira_report["bulgular"]]
    assert ranks == sorted(ranks)


def test_findings_reference_known_laws_only(kira_report):
    from app.knowledge import legal_refs

    for f in kira_report["bulgular"]:
        if f["yasal_dayanak"]:
            assert f["yasal_dayanak"]["id"] in legal_refs()


def test_quote_offsets_point_to_exact_text(kira_report):
    """Arayüz yalnızca alıntının kendisini işaretler; konum metinle birebir uyuşmalı."""
    clauses = {c["id"]: c for c in kira_report["maddeler"]}
    for f in kira_report["bulgular"]:
        k = f["alinti_konum"]
        assert k is not None
        assert kira_report["metin"][k["start"]:k["end"]] == f["alinti"]
        c = clauses[f["madde_id"]]
        assert c["start"] <= k["start"] < k["end"] <= c["end"]  # alıntı kendi maddesinin içinde


def test_locate_quote_ignores_whitespace_and_rejects_short():
    from app.ai_engine import _locate_quote

    text = "Kiracı, sözleşme imzası sırasında\n4 (dört) aylık kira bedeli öder."
    assert _locate_quote(text, "4 (dört) aylık kira") is not None
    assert _locate_quote(text, "imzası sırasında 4 (dört)") is not None  # satır sonu farkı
    assert _locate_quote(text, "depozito iade edilmez") is None
    assert _locate_quote(text, "kira") is None


def test_personal_data_never_in_report(kira_report):
    assert "10000000146" not in kira_report["metin"]
    assert kira_report["maskelenen_veriler"]["TC_KIMLIK"] == 1


def test_deposit_within_limit_not_flagged():
    text = ("KİRA SÖZLEŞMESİ\nMADDE 1 - Kiracı kiraya verene aylık kira öder.\n"
            "MADDE 2 - Kiracı 2 (iki) aylık kira bedeli tutarında depozito verir.\n"
            "MADDE 3 - Depozito tahliyeden sonra 30 gün içinde iade edilir.")
    report = analyzer.analyze(text, "kira", "kiraci", "kural")
    assert not any(f["id"].startswith("depozito_fazla") for f in report["bulgular"])


def test_clean_contract_has_low_score():
    text = ("KONUT KİRA SÖZLEŞMESİ\nBaşlangıç tarihi: 01.01.2027, süresi 1 yıl.\n"
            "MADDE 1 - Kira bedeli her ayın 5'ine kadar kiraya verenin banka hesabına havale edilir.\n"
            "MADDE 2 - Yenilenen dönemlerde kira, TÜFE on iki aylık ortalama oranında artırılır.\n"
            "MADDE 3 - Kiracı 2 aylık kira tutarında güvence bedelini vadeli hesaba yatırır; bedel tahliyeden sonra 30 gün içinde iade edilir.\n"
            "MADDE 4 - Teslim sırasında demirbaş listesi ve fotoğraflı teslim tutanağı düzenlenir.")
    report = analyzer.analyze(text, "kira", "kiraci", "kural")
    assert report["sayilar"]["yuksek"] == 0
    assert report["risk_puani"] < 25


def test_landlord_role_gets_different_framing():
    report = analyzer.analyze(KIRA, "kira", "kiraya_veren", "kural")
    assert report["rol"] == "kiraya_veren"
    assert all(f["risk"] == "orta" for f in report["bulgular"])
    assert all(f["gecerlilik"] == "gecersiz_olabilir" for f in report["bulgular"])


def test_hizmet_detects_consumer_risks():
    report = analyzer.analyze(HIZMET, "otomatik", "otomatik", "kural")
    ids = {f["id"].split("-")[0] for f in report["bulgular"]}
    assert {"otomatik_yenileme", "tek_tarafli_degisiklik", "cayma_hakki_yok", "fesih_zor", "veri_paylasim"} <= ids
    assert report["rol"] == "musteri"


# ---------------- tarih çıkarımı ----------------
def test_dates_compute_notice_deadline():
    dates = extract_dates("Başlangıç Tarihi: 01.09.2026\nKira Süresi: 1 (bir) yıl", "kira")
    by_desc = {d["aciklama"]: d["tarih"] for d in dates}
    assert by_desc["Başlangıç tarihi"] == "2026-09-01"
    assert by_desc["Hesaplanan bitiş tarihi"] == "2027-08-31"
    reminder = [d for d in dates if d.get("hatirlatma")][0]
    assert reminder["tarih"] == "2027-08-16"


def test_invalid_date_is_ignored():
    assert extract_dates("Başlangıç: 31.02.2026", "kira") == []


# ---------------- iş sözleşmesi ve diğer ----------------
IS = (SAMPLES / "ornek_is_sozlesmesi.txt").read_text(encoding="utf-8")


def test_detects_employment_contract():
    assert detect_contract_type(IS) == "is"


def test_employment_rules_for_employee():
    report = analyzer.analyze(IS, "otomatik", "calisan", "kural")
    assert report["sozlesme_turu"] == "is" and report["rol"] == "calisan"
    ids = {f["id"].split("-")[0] for f in report["bulgular"]}
    for expected in ["deneme_suresi_uzun", "haftalik_saat", "fazla_mesai_dahil", "tek_tarafli_gorev_yer",
                     "rekabet_suresi_uzun", "cezai_sart_isci", "egitim_gideri", "ucret_kesinti",
                     "izin_feragat", "kidem_feragat"]:
        assert expected in ids, expected
    # özel bulgu varsa aynı maddedeki genel rekabet bulgusu tekrarlanmaz
    assert "rekabet_yasagi" not in ids


def test_short_probation_not_flagged():
    text = ("İŞ SÖZLEŞMESİ\nMADDE 1 - İşçi ile işveren arasında iş sözleşmesi yapılmıştır.\n"
            "MADDE 2 - Deneme süresi 1 (bir) aydır.\nMADDE 3 - Haftalık çalışma süresi 45 saattir.")
    report = analyzer.analyze(text, "is", "calisan", "kural")
    assert not any(f["id"].startswith(("deneme_suresi_uzun", "haftalik_saat")) for f in report["bulgular"])


def test_employer_role_gets_no_employee_questions():
    report = analyzer.analyze(IS, "is", "isveren", "kural")
    assert report["sorulacak_sorular"] == []
    assert all(f["risk"] == "orta" for f in report["bulgular"])


def test_other_contract_uses_role_note_and_warns_without_ai():
    report = analyzer.analyze(HIZMET, "diger", "taraf", "kural", "Kursa kaydolan öğrenci")
    assert report["sozlesme_turu"] == "diger" and report["rol_ad"] == "Kursa kaydolan öğrenci"
    assert report["uyari"] and "genel riskli ifadeler" in report["uyari"]
    assert report["sayilar"]["yuksek"] > 0  # genel kurallar yine çalışır


# ---------------- kalibrasyon: standart sözleşmelerde yanlış alarm olmamalı ----------------
STANDARD_LEASE = """KİRA SÖZLEŞMESİ
Kiracının Adı Soyadı
Garantör ve Müşterek Müteselsil Kefilin Adı Soyadı
GENEL KOŞULLAR
1- Kiracı, kiralananı özenle kullanmak zorundadır.
2- Kiracı, kiralananı kısmen veya tamamen üçüncü kişilere kiralayamaz.
3- Kiralananın normal kullanımından dolayı ortaya çıkacak yıpranma ve eksikliklerden dolayı kiracı sorumlu değildir.
4- Kiracı elektrik, su ve doğal gaz ücretlerini süresinde ödemediği takdirde bunlara ait bütün gecikme zammı ve cezaları da kendisi ödeyecektir.
5- Kira artışı, bir önceki kira yılının TÜFE on iki aylık ortalaması oranında yapılır.
6- Kiracı depozito olarak 2 aylık kira bedelini verir; depozito tahliyeden sonra 30 gün içinde iade edilir.
7- Sözleşmeden doğacak uyuşmazlıklarda Ankara mahkemeleri yetkilidir.
Kiracı Müteselsil Borçlu ve Kefil Kiraya Veren"""


def test_standard_lease_is_not_alarming():
    report = analyzer.analyze(STANDARD_LEASE, "kira", "kiraci", "kural")
    ids = {f["id"].split("-")[0] for f in report["bulgular"]}
    assert "sorumluluk_reddi" not in ids      # kiracı lehine madde risk sayılmaz
    assert "cezai_sart_gec_odeme" not in ids  # fatura gecikme cezası kira cezası değildir
    assert report["sayilar"]["yuksek"] == 0
    assert report["risk_puani"] < 35
    # başlıktaki form alanları (kefil etiketi) madde gibi işaretlenmez; kefil en fazla bir kez görünür
    kefil = [f for f in report["bulgular"] if f["id"].startswith("kefil")]
    assert len(kefil) <= 1
    assert all(not report["maddeler"][int(f["madde_id"][1:]) - 1]["preamble"] for f in report["bulgular"])


def test_landlord_disclaimer_still_flagged():
    text = STANDARD_LEASE.replace("7- Sözleşmeden", "8- Kiraya veren, kiracının uğrayacağı zararlardan hiçbir şekilde sorumlu tutulamaz.\n7- Sözleşmeden")
    ids = {f["id"].split("-")[0] for f in analyzer.analyze(text, "kira", "kiraci", "kural")["bulgular"]}
    assert "sorumluluk_reddi" in ids


def test_risk_score_saturates_and_counts_clause_once():
    from app.report import risk_score

    five_high = [{"madde_id": f"m{i}", "risk": "yuksek"} for i in range(5)]
    assert 70 <= risk_score(five_high, []) < 100
    same_clause = [{"madde_id": "m1", "risk": "yuksek"}, {"madde_id": "m1", "risk": "orta"}]
    assert risk_score(same_clause, []) == risk_score([{"madde_id": "m1", "risk": "yuksek"}], [])
    assert risk_score([], []) == 0
