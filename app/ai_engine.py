"""Yapay zekâ motoru: Google Gemini veya Anthropic Claude.

Hangi sağlayıcının kullanılacağı .env dosyasındaki anahtara göre otomatik seçilir
(bkz. config.active_provider). Uygulamanın geri kalanı sağlayıcıdan bağımsızdır.

- analyze(): maddeleri okur, kullanıcının rolüne göre riskli maddeleri sade Türkçe ile açıklar.
- ask(): sözleşme hakkında serbest soru-cevap.
- transcribe_image(): fotoğraftaki sözleşme metnini okur (OCR).

Halüsinasyona karşı önlemler:
- Model yalnızca bizim verdiğimiz madde kimliklerine (m1, m2…) ve bilgi tabanındaki
  kanun referanslarına atıf yapabilir; JSON şeması bunları enum ile sınırlar.
- Modelin verdiği alıntı gerçekten metinde geçmiyorsa alıntı maddenin kendisiyle değiştirilir.
- Bilinmeyen madde kimliği veya kanun referansı içeren bulgular temizlenir.
"""
import base64
import json
import logging
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from . import config, knowledge
from .report import CONTRACT_TYPES, ROLES

log = logging.getLogger("sade.ai")
NO_REF = "YOK"  # "uygun kanun referansı yok" anlamındaki değer
_clients: dict = {}


class AIError(Exception):
    pass


def is_enabled() -> bool:
    return config.ai_available()


def provider_name() -> str | None:
    return {"gemini": "Gemini", "anthropic": "Claude"}.get(config.active_provider())


_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ai")


MAX_FALLBACK_STEPS = 4  # en fazla bu kadar istek (şemalı/şemasız, ana/yedek modeller)


def _deadline() -> float:
    """Bir yapay zekâ çağrısının en fazla süreceği toplam süre (tüm denemeler dahil)."""
    return config.AI_TIMEOUT_SECONDS * max(1, config.AI_MAX_ATTEMPTS) * MAX_FALLBACK_STEPS + 10


def _with_deadline(fn, *args):
    """fn'yi ayrı bir iş parçacığında çalıştırır; süre dolarsa beklemeyi bırakır.

    Ağ katmanı ne sebeple takılırsa takılsın sayfa sonsuza kadar beklemez.
    """
    future = _executor.submit(fn, *args)
    try:
        return future.result(timeout=_deadline())
    except FutureTimeout as exc:
        raise AIError(
            f"Yapay zekâ servisi {int(_deadline())} saniye içinde cevap vermedi. "
            "İnternet bağlantınızı kontrol edin; okul/yurt ağlarında Google servisleri engelli olabilir."
        ) from exc


# ================================================================== sağlayıcı katmanı
_original_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Google API adresleri için yalnızca IPv4 adreslerini döndürür."""
    if family in (0, socket.AF_UNSPEC) and isinstance(host, str) and host.endswith("googleapis.com"):
        family = socket.AF_INET
    return _original_getaddrinfo(host, port, family, type, proto, flags)


def _prefer_ipv4_for_google():
    """IPv6'nın çalışmadığı ağlarda (bazı ev/okul ağları) bağlantı sonsuza kadar asılı kalabiliyor.

    Python önce IPv6 adresini dener ve cevap gelmezse beklemeye devam eder; tarayıcılar ve curl
    ise otomatik olarak IPv4'e geçer. Bu yüzden Google adresleri için doğrudan IPv4 kullanıyoruz.
    """
    socket.getaddrinfo = _ipv4_getaddrinfo


def _gemini_client():
    if "gemini" not in _clients:
        from google import genai
        from google.genai import types

        import httpx2

        if config.AI_FORCE_IPV4:
            _prefer_ipv4_for_google()
        # Kendi HTTP istemcimiz: bağlantı kurma süresine ayrı sınır.
        http_client = httpx2.Client(timeout=httpx2.Timeout(config.AI_TIMEOUT_SECONDS, connect=10.0))
        _clients["gemini"] = genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options=types.HttpOptions(
                httpx_client=http_client,
                timeout=int(config.AI_TIMEOUT_SECONDS * 1000),
                # Kütüphane varsayılan olarak 5 kez tekrar dener; bu, sayfanın dakikalarca beklemesine yol açar.
                retry_options=types.HttpRetryOptions(
                    attempts=config.AI_MAX_ATTEMPTS, initial_delay=1.0, max_delay=4.0,
                    http_status_codes=[500, 502, 503, 504],
                ),
            ),
        )
    return _clients["gemini"]


def _anthropic_client():
    if "anthropic" not in _clients:
        import anthropic

        _clients["anthropic"] = anthropic.Anthropic(
            api_key=config.ANTHROPIC_API_KEY, timeout=config.AI_TIMEOUT_SECONDS,
            max_retries=max(0, config.AI_MAX_ATTEMPTS - 1))
    return _clients["anthropic"]


def _error_detail(exc: Exception) -> str:
    """Terminal günlüğü için hatanın kısa, okunur özeti."""
    code = getattr(exc, "code", None)
    if code is not None:
        return f"kod {code} {getattr(exc, 'status', '') or ''}: {getattr(exc, 'message', '') or str(exc)}"[:300]
    return f"{exc.__class__.__name__}: {exc}"[:300]


def _is_schema_error(exc: Exception) -> bool:
    """Model, istenen JSON şemasını desteklemiyorsa (ör. 'too many states') True."""
    return getattr(exc, "code", None) == 400 and "schema" in str(exc).lower()


def _simplify_schema_for_gemini(schema: dict) -> dict:
    """Uzun enum listelerini ve sınırları kaldırır (hafif modeller karmaşık şemaları reddediyor).

    Madde kimlikleri ve kanun referansları zaten analyze() içinde doğrulanıyor.
    """
    import copy

    s = copy.deepcopy(schema)
    item = s.get("properties", {}).get("bulgular", {}).get("items", {}).get("properties", {})
    for key in ("madde_id", "yasal_ref"):
        if key in item:
            item[key].pop("enum", None)
    for prop in s.get("properties", {}).values():
        prop.pop("maxItems", None)
    return s


def _is_auth_or_network_error(exc: Exception) -> bool:
    """Yedek denemelerle düzelmeyecek hatalar: geçersiz anahtar, bağlantı yok, zaman aşımı."""
    text = str(exc)
    if getattr(exc, "code", None) in (401, 403) or "API_KEY_INVALID" in text or "API key not valid" in text:
        return True
    name = exc.__class__.__name__.lower()
    return "connect" in name or "timeout" in name


def _gemini_error(exc: Exception) -> AIError:
    from google.genai import errors

    if isinstance(exc, AIError):
        return exc

    if isinstance(exc, errors.APIError):
        code = getattr(exc, "code", None)
        text = str(exc)
        if code in (401, 403) or "API_KEY_INVALID" in text or "API key not valid" in text:
            return AIError("Gemini API anahtarı geçersiz. .env dosyasındaki GEMINI_API_KEY değerini kontrol edin.")
        if code == 404:
            return AIError(f"'{config.GEMINI_MODEL}' adlı Gemini modeli bulunamadı. .env dosyasında GEMINI_MODEL değerini değiştirin.")
        if code == 429:
            return AIError("Gemini ücretsiz kullanım sınırına ulaşıldı. Bir dakika bekleyip tekrar deneyin.")
        if isinstance(exc, errors.ServerError):
            return AIError(f"Gemini servisi şu an yanıt vermiyor (kod {code}). Biraz sonra tekrar deneyin.")
        return AIError(f"Gemini isteği reddetti (kod {code}).")
    name = exc.__class__.__name__.lower()
    if "connect" in name:
        return AIError("Gemini sunucusuna bağlanılamadı. İnternet bağlantınızı kontrol edin; okul/yurt ağlarında Google servisleri engelli olabilir.")
    if "timeout" in name or "timed out" in str(exc).lower():
        return AIError(f"Gemini {int(config.AI_TIMEOUT_SECONDS)} saniye içinde cevap vermedi.")
    return AIError("Gemini servisine ulaşılamadı. İnternet bağlantınızı kontrol edin.")


def _anthropic_error(exc: Exception) -> AIError:
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return AIError("Claude API anahtarı geçersiz. .env dosyasındaki ANTHROPIC_API_KEY değerini kontrol edin.")
    if isinstance(exc, anthropic.RateLimitError):
        return AIError("Claude servisi şu an yoğun (istek sınırı). Biraz sonra tekrar deneyin.")
    if isinstance(exc, anthropic.APITimeoutError):
        return AIError("Claude servisi zamanında yanıt vermedi.")
    return AIError(f"Claude servisine ulaşılamadı: {exc.__class__.__name__}")


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)  # olası ``` işaretlerini atla
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise AIError("Yapay zekâ beklenen formatta yanıt vermedi. Tekrar deneyin.")


def _json_call(system: str, user: str, schema: dict) -> dict:
    """Şemaya uygun JSON cevabı döndürür (süreyi ve hataları terminale yazar)."""
    start = time.monotonic()
    log.info("Yapay zekâ analizi başladı (%s, %s)", config.active_provider(), config.active_model())
    try:
        result = _with_deadline(_json_call_inner, system, user, schema)
    except AIError as exc:
        log.warning("Yapay zekâ analizi başarısız (%.1f sn): %s", time.monotonic() - start, exc)
        raise
    log.info("Yapay zekâ analizi bitti (%.1f sn)", time.monotonic() - start)
    return result


def _json_call_inner(system: str, user: str, schema: dict) -> dict:
    provider = config.active_provider()
    if provider == "gemini":
        # Her model için önce sade şemalı istek; model şemayı reddederse (400) aynı modelle şemasız istek.
        # Yoğunluk (503), kota (429) veya model yok (404) gibi hatalarda sıradaki modele geçilir.
        gemini_schema = _simplify_schema_for_gemini(schema)
        last_exc, steps = None, 0
        for model in config.gemini_models():
            for with_schema in (True, False):
                if steps >= MAX_FALLBACK_STEPS:
                    break
                steps += 1
                try:
                    return _gemini_json(model, system, user, gemini_schema, with_schema)
                except Exception as exc:  # noqa: BLE001 - tüm SDK/ağ hataları kullanıcı mesajına çevrilir
                    last_exc = exc
                    log.warning("Gemini denemesi başarısız (model=%s, şema=%s): %s",
                                model, "var" if with_schema else "yok", _error_detail(exc))
                    if _is_auth_or_network_error(exc):
                        raise _gemini_error(exc) from exc
                    if not (with_schema and _is_schema_error(exc)):
                        break  # şema sorunu değilse aynı modeli şemasız denemenin anlamı yok
        raise _gemini_error(last_exc) from last_exc

    if provider == "anthropic":
        try:
            resp = _anthropic_client().messages.create(
                model=config.AI_MODEL, max_tokens=8000, system=system,
                tools=[{"name": "rapor_ver", "description": "Analiz raporunu kaydeder.", "input_schema": schema}],
                tool_choice={"type": "tool", "name": "rapor_ver"},
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001
            raise _anthropic_error(exc) from exc
        data = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        if not isinstance(data, dict):
            raise AIError("Yapay zekâ beklenen formatta yanıt vermedi. Tekrar deneyin.")
        return data

    raise AIError("Yapay zekâ anahtarı tanımlı değil.")


def _gemini_json(model: str, system: str, user: str, schema: dict, with_schema: bool) -> dict:
    from google.genai import types

    if with_schema:
        cfg = types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json",
            response_json_schema=schema, temperature=0.2, max_output_tokens=8192)
    else:
        cfg = types.GenerateContentConfig(
            system_instruction=system + "\n\nCevabını YALNIZCA şu JSON şemasına uyan tek bir JSON nesnesi olarak ver:\n"
            + json.dumps(schema, ensure_ascii=False),
            response_mime_type="application/json", temperature=0.2, max_output_tokens=8192)
    resp = _gemini_client().models.generate_content(model=model, contents=user, config=cfg)
    return _parse_json(resp.text)


def _text_call(system: str, messages: list) -> str:
    """messages: [{"role": "user"|"assistant", "content": str}, ...] (user ile başlar, sırayla)."""
    return _with_deadline(_text_call_inner, system, messages)


def _text_call_inner(system: str, messages: list) -> str:
    provider = config.active_provider()
    if provider == "gemini":
        from google.genai import types

        contents = [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages
        ]
        last_exc = None
        for model in config.gemini_models()[:3]:
            try:
                resp = _gemini_client().models.generate_content(
                    model=model, contents=contents,
                    config=types.GenerateContentConfig(system_instruction=system, temperature=0.3, max_output_tokens=4096),
                )
                return (resp.text or "").strip()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning("Gemini sohbet denemesi başarısız (model=%s): %s", model, _error_detail(exc))
                if _is_auth_or_network_error(exc):
                    break
        raise _gemini_error(last_exc) from last_exc

    if provider == "anthropic":
        try:
            resp = _anthropic_client().messages.create(
                model=config.AI_MODEL, max_tokens=1200, system=system, messages=messages)
        except Exception as exc:  # noqa: BLE001
            raise _anthropic_error(exc) from exc
        return "".join(getattr(b, "text", "") for b in resp.content).strip()

    raise AIError("Yapay zekâ anahtarı tanımlı değil.")


def _image_call(data: bytes, media_type: str, prompt: str) -> str:
    return _with_deadline(_image_call_inner, data, media_type, prompt)


def _image_call_inner(data: bytes, media_type: str, prompt: str) -> str:
    provider = config.active_provider()
    if provider == "gemini":
        from google.genai import types

        try:
            resp = _gemini_client().models.generate_content(
                model=config.GEMINI_MODEL,
                contents=[types.Part.from_bytes(data=data, mime_type=media_type), prompt],
                config=types.GenerateContentConfig(temperature=0, max_output_tokens=16384),
            )
        except Exception as exc:  # noqa: BLE001
            raise _gemini_error(exc) from exc
        return (resp.text or "").strip()

    if provider == "anthropic":
        try:
            resp = _anthropic_client().messages.create(
                model=config.AI_MODEL, max_tokens=8000,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                 "data": base64.standard_b64encode(data).decode()}},
                    {"type": "text", "text": prompt},
                ]}],
            )
        except Exception as exc:  # noqa: BLE001
            raise _anthropic_error(exc) from exc
        return "".join(getattr(b, "text", "") for b in resp.content).strip()

    raise AIError("Yapay zekâ anahtarı tanımlı değil.")


# ================================================================== analiz
def report_schema(clause_ids: list[str]) -> dict:
    """Her iki sağlayıcının da desteklediği sade JSON şeması."""
    ref_ids = list(knowledge.legal_refs().keys())
    return {
        "type": "object",
        "properties": {
            "sozlesme_turu": {"type": "string", "enum": list(CONTRACT_TYPES.keys())},
            "genel_ozet": {"type": "string", "description": "2-4 cümlelik, sade Türkçe genel değerlendirme."},
            "bulgular": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "madde_id": {"type": "string", "enum": clause_ids},
                        "risk": {"type": "string", "enum": ["yuksek", "orta", "dusuk"]},
                        "kategori": {"type": "string", "description": "Kısa kategori adı, ör. Depozito, Kira artışı, Fesih"},
                        "baslik": {"type": "string", "description": "En fazla 10 kelimelik başlık"},
                        "alinti": {"type": "string", "description": "Maddeden KELİMESİ KELİMESİNE kopyalanmış en kritik kısa ifade (en fazla 40 kelime)"},
                        "sade": {"type": "string", "description": "Bu madde ne diyor? Lise öğrencisinin anlayacağı dille 1-2 cümle"},
                        "neden": {"type": "string", "description": "Kullanıcıya etkisi ve neden riskli olduğu, 1-3 cümle"},
                        "oneri": {"type": "string", "description": "Kullanıcının ne yapması gerektiği, 1-2 cümle"},
                        "alternatif": {"type": "string", "description": "Karşı tarafa önerilebilecek dengeli madde metni; yoksa boş metin"},
                        "yasal_ref": {"type": "string", "enum": ref_ids + [NO_REF], "description": f"Listeden kanun kimliği; uygun yoksa {NO_REF}"},
                        "gecerlilik": {"type": "string", "enum": ["gecersiz_olabilir", "gecerli_ama_riskli", "belirsiz"]},
                    },
                    "required": ["madde_id", "risk", "kategori", "baslik", "alinti", "sade", "neden", "oneri", "yasal_ref", "gecerlilik"],
                },
            },
            "eksik_maddeler": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"baslik": {"type": "string"}, "aciklama": {"type": "string"}},
                    "required": ["baslik", "aciklama"],
                },
            },
            "onemli_tarihler": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "aciklama": {"type": "string"},
                        "tarih": {"type": "string", "description": "YYYY-AA-GG biçiminde; belirli bir tarih yoksa boş metin"},
                    },
                    "required": ["aciklama"],
                },
            },
            "sorulacak_sorular": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        },
        "required": ["sozlesme_turu", "genel_ozet", "bulgular", "eksik_maddeler", "sorulacak_sorular"],
    }


def _refs_for_prompt() -> str:
    return "\n".join(
        f"- {rid}: {r['kanun']} {r['madde']} ({r['baslik']}): {r['ozet']}"
        for rid, r in knowledge.legal_refs().items()
    )


_SYSTEM_ANALYZE = """Sen Türk hukukuna hâkim, sözleşmeleri sıradan insanlara anlatan bir asistansın.
Görevin: verilen sözleşmeyi KULLANICININ ROLÜNDEN bakarak incelemek ve onun aleyhine olabilecek maddeleri bulmak.

Kurallar:
1. Dil: Sade, günlük Türkçe. Hukuk jargonu kullanma; zorunlu kalırsan parantez içinde açıkla ("muacceliyet (kalan tüm borcun hemen istenmesi)").
2. Her bulgu bir madde_id'ye bağlı olmalı ve "alinti" alanı o maddeden kelimesi kelimesine kopyalanmalı.
3. Kanun atfını YALNIZCA aşağıdaki listeden yap (yasal_ref). Listede uygun madde yoksa "{no_ref}" yaz; asla kanun maddesi uydurma.
4. gecerlilik: Kanuna aykırı olduğu için geçersiz sayılabilecekse "gecersiz_olabilir"; geçerli ama kullanıcıyı zorlayacaksa "gecerli_ama_riskli"; emin değilsen "belirsiz".
5. Kalibrasyon çok önemli. Sıradan, yaygın ve kanuna uygun maddeleri (ör. kiracının özenle kullanma
   borcu, alt kiralama yasağı, kefil istenmesi, olağan aidat/fatura giderleri, yetkili mahkeme) ya hiç
   raporlama ya da "dusuk" ver. Kullanıcının LEHİNE olan maddeleri (ör. "kiracı olağan yıpranmadan sorumlu
   değildir") asla risk olarak raporlama. "yuksek" yalnızca kullanıcıya ciddi maddi kayıp ya da hak kaybı
   doğurabilecek maddeler içindir; internetten bulunan standart bir sözleşmede genellikle 0-3 yüksek riskli
   madde olur. Başlıktaki form alanlarını, taraf bilgilerini ve imza satırlarını madde olarak değerlendirme.
6. "eksik_maddeler": Kullanıcıyı korumak için olması gerekirken sözleşmede OLMAYAN hükümler.
7. "alternatif": Kullanıcının karşı tarafa önerebileceği, dengeli ve kısa bir madde metni (yoksa boş bırak).
8. "sorulacak_sorular": İmzadan önce karşı tarafa sorulması gereken somut sorular.
9. Metindeki [TC_KIMLIK], [IBAN] gibi etiketler gizlenmiş kişisel verilerdir; bunları sorun olarak raporlama.
10. Kesin hukuki hüküm verme; "olabilir", "sayılabilir" gibi ihtiyatlı ifadeler kullan.
11. Cevabını yalnızca istenen JSON yapısında ver.

Kullanılabilecek kanun referansları:
{refs}
"""


def _clauses_block(clauses) -> str:
    return "\n\n".join(
        f"[{c.id}]{' (başlık, taraf bilgileri ve form alanları; madde değildir)' if getattr(c, 'preamble', False) else ''}\n{c.text}"
        for c in clauses)


def _locate_quote(text: str, quote: str):
    """Alıntının metindeki (başlangıç, bitiş) konumunu döndürür; bulunamazsa None.

    Satır sonu/boşluk farkları ve büyük-küçük harf önemsenmez; kelimeler aynı sırada olmalıdır.
    Çok kısa alıntılar (3 kelimeden az) güvenilir eşleşme sayılmaz.
    """
    words = re.findall(r"\S+", quote.replace("…", " ").strip(" .;,"))
    if len(words) < 3:
        return None
    pattern = r"\s+".join(re.escape(w) for w in words)
    m = re.search(pattern, text, re.IGNORECASE)
    return (m.start(), m.end()) if m else None


def role_label(contract_type: str, role: str, role_note: str = "") -> str:
    base = ROLES[contract_type][role]
    return f"{base} ({role_note})" if role_note else base


def analyze(clauses, contract_type: str, role: str, role_note: str = "") -> dict:
    """Yapay zekâ raporunu döndürür (doğrulanmış ve temizlenmiş)."""
    clause_map = {c.id: c for c in clauses}
    user_msg = (
        f"Sözleşme türü (kullanıcının seçimi/tahmin): {CONTRACT_TYPES[contract_type]}\n"
        f"Kullanıcının rolü: {role_label(contract_type, role, role_note)}\n\n"
        f"Maddeler:\n\n{_clauses_block(clauses)}"
    )
    data = _json_call(
        _SYSTEM_ANALYZE.format(refs=_refs_for_prompt(), no_ref=NO_REF),
        user_msg,
        report_schema(list(clause_map.keys())),
    )
    if not isinstance(data, dict):
        raise AIError("Yapay zekâ beklenen formatta yanıt vermedi. Tekrar deneyin.")

    findings = []
    for i, f in enumerate(data.get("bulgular") or []):
        if not isinstance(f, dict):
            continue
        clause = clause_map.get(f.get("madde_id"))
        if not clause or f.get("risk") not in ("yuksek", "orta", "dusuk"):
            continue
        if getattr(clause, "preamble", False) and f.get("risk") != "yuksek":
            continue  # form alanları/taraf bilgileri gürültü üretmesin
        # Alıntıyı maddede birebir bul (boşluk farkları önemsiz). Bulunamazsa kaynak tahmin edilmez.
        located = _locate_quote(clause.text, (f.get("alinti") or "").strip().strip('"“”«»'))
        if located:
            q_start, q_end = located
            quote = clause.text[q_start:q_end]
            quote_pos = {"start": clause.start + q_start, "end": clause.start + q_end}
        else:
            quote, quote_pos, q_start = "", None, 0
        pos = q_start
        ref = f.get("yasal_ref")
        findings.append({
            "id": f"ai{i + 1}-{clause.id}",
            "madde_id": clause.id,
            "madde_baslik": clause.title,
            "risk": f["risk"],
            "kategori": (f.get("kategori") or "Genel").strip(),
            "baslik": (f.get("baslik") or "").strip(),
            "alinti": quote,
            "alinti_konum": quote_pos,
            "sade": f.get("sade", ""),
            "neden": f.get("neden", ""),
            "oneri": f.get("oneri", ""),
            "alternatif": (f.get("alternatif") or "").strip() or None,
            "yasal_dayanak": None if ref in (None, "", NO_REF) else knowledge.get_ref(ref),
            "gecerlilik": f.get("gecerlilik") if f.get("gecerlilik") in ("gecersiz_olabilir", "gecerli_ama_riskli", "belirsiz") else "belirsiz",
            "kaynak": "ai",
            "_pos": clause.start + max(pos, 0),
        })

    dates = []
    for d in data.get("onemli_tarihler") or []:
        if not isinstance(d, dict):
            continue
        t = (d.get("tarih") or "").strip()
        dates.append({"aciklama": d.get("aciklama", ""),
                      "tarih": t if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t) else None, "kaynak": "ai"})

    return {
        "sozlesme_turu": data.get("sozlesme_turu") if data.get("sozlesme_turu") in CONTRACT_TYPES else contract_type,
        "genel_ozet": data.get("genel_ozet", ""),
        "bulgular": findings,
        "eksik_maddeler": [m for m in (data.get("eksik_maddeler") or []) if isinstance(m, dict) and m.get("baslik")],
        "onemli_tarihler": dates,
        "sorulacak_sorular": [q for q in (data.get("sorulacak_sorular") or []) if isinstance(q, str) and q.strip()],
    }


# ================================================================== soru-cevap
_SYSTEM_ASK = """Sen bir sözleşme açıklama asistanısın. Kullanıcı {rol} rolünde.
Soruları YALNIZCA aşağıdaki sözleşme metnine ve verilen kanun özetlerine dayanarak cevapla.
- Cevabın dayandığı maddeleri [m3] biçiminde belirt.
- Sözleşmede cevap yoksa bunu açıkça söyle ve genel kuralı (varsa kanun özetinden) belirt.
- Sade Türkçe, en fazla 6-7 cümle. Kanun maddesi uydurma. Markdown biçimlendirmesi kullanma.
- Ciddi bir uyuşmazlık varsa avukata veya baro adli yardım bürosuna danışmayı öner.

Kanun özetleri:
{refs}

Sözleşme:
{contract}
"""


def _clean_history(history: list, question: str) -> list:
    """Sohbet geçmişini 'user' ile başlayan, rolleri sırayla değişen bir listeye çevirir."""
    messages = [
        {"role": h["rol"], "content": h["metin"][:2000]}
        for h in history[-8:]
        if h.get("rol") in ("user", "assistant") and h.get("metin")
    ]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    cleaned = []
    for m in messages:
        if cleaned and cleaned[-1]["role"] == m["role"]:
            cleaned[-1] = m
        else:
            cleaned.append(m)
    if cleaned and cleaned[-1]["role"] == "user":
        cleaned.pop()
    cleaned.append({"role": "user", "content": question})
    return cleaned


def ask(question: str, clauses, contract_type: str, role: str, history: list, role_note: str = "") -> str:
    system = _SYSTEM_ASK.format(rol=role_label(contract_type, role, role_note), refs=_refs_for_prompt(),
                                contract=_clauses_block(clauses))
    answer = _text_call(system, _clean_history(history, question))
    answer = re.sub(r"\*\*(.+?)\*\*", r"\1", answer)  # olası kalın yazı işaretlerini temizle
    if not answer:
        raise AIError("Yapay zekâ boş bir cevap döndürdü. Soruyu farklı sormayı deneyin.")
    return answer


# ================================================================== OCR
_OCR_PROMPT = (
    "Bu görsel bir sözleşme sayfası. İçindeki metni Türkçe karakterleri koruyarak, "
    "madde numaralarıyla birlikte eksiksiz yaz. El yazısıyla doldurulmuş alanları da ekle; "
    "okunamayan yerlere [okunamadı] yaz. Yalnızca metni döndür, yorum ekleme."
)


def transcribe_image(data: bytes, media_type: str) -> str:
    text = _image_call(data, media_type, _OCR_PROMPT)
    if len(text) < 20:
        raise AIError("Fotoğrafta okunabilir bir metin bulunamadı.")
    return text
