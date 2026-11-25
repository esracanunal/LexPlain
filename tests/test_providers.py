"""Sağlayıcı katmanı testleri: Gemini/Claude seçimi ve Gemini isteğinin doğru kurulması (ağ kullanılmaz)."""
from types import SimpleNamespace

import pytest

from app import ai_engine, config


@pytest.fixture(autouse=True)
def clean_keys(monkeypatch):
    for name in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "AI_PROVIDER"):
        monkeypatch.setattr(config, name, "")
    ai_engine._clients.clear()
    yield
    ai_engine._clients.clear()


def test_provider_selection(monkeypatch):
    assert config.active_provider() is None
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "a")
    assert config.active_provider() == "anthropic"
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g")
    assert config.active_provider() == "gemini"  # ikisi de varsa Gemini
    monkeypatch.setattr(config, "AI_PROVIDER", "anthropic")
    assert config.active_provider() == "anthropic"
    monkeypatch.setattr(config, "AI_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert config.active_provider() is None  # seçilen sağlayıcının anahtarı yok


class FakeModels:
    def __init__(self, text=None, exc=None):
        self.text, self.exc, self.calls = text, exc, []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return SimpleNamespace(text=self.text)


def _use_fake_gemini(monkeypatch, models):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g")
    ai_engine._clients["gemini"] = SimpleNamespace(models=models)


def test_gemini_json_request(monkeypatch):
    models = FakeModels(text='```json\n{"a": 1}\n```')
    _use_fake_gemini(monkeypatch, models)
    assert ai_engine._json_call("sistem", "kullanıcı", {"type": "object"}) == {"a": 1}
    cfg = models.calls[0]["config"]
    assert models.calls[0]["model"] == config.GEMINI_MODEL
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == {"type": "object"}
    assert cfg.system_instruction == "sistem"


def test_gemini_chat_roles(monkeypatch):
    models = FakeModels(text="cevap")
    _use_fake_gemini(monkeypatch, models)
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    assert ai_engine._text_call("s", msgs) == "cevap"
    assert [c["role"] for c in models.calls[0]["contents"]] == ["user", "model", "user"]


@pytest.mark.parametrize("code,needle", [(404, "GEMINI_MODEL"), (429, "sınır"), (400, "reddetti")])
def test_gemini_errors_are_friendly(monkeypatch, code, needle):
    from google.genai import errors

    exc = errors.ClientError(code, {"error": {"code": code, "message": "x", "status": "X"}})
    _use_fake_gemini(monkeypatch, FakeModels(exc=exc))
    with pytest.raises(ai_engine.AIError, match=needle):
        ai_engine._json_call("s", "u", {"type": "object"})


def test_gemini_invalid_key_message(monkeypatch):
    from google.genai import errors

    exc = errors.ClientError(400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}})
    _use_fake_gemini(monkeypatch, FakeModels(exc=exc))
    with pytest.raises(ai_engine.AIError, match="anahtarı geçersiz"):
        ai_engine._text_call("s", [{"role": "user", "content": "x"}])


def test_bad_json_raises(monkeypatch):
    _use_fake_gemini(monkeypatch, FakeModels(text="bu json değil"))
    with pytest.raises(ai_engine.AIError, match="formatta"):
        ai_engine._json_call("s", "u", {"type": "object"})


def test_gemini_timeout_message(monkeypatch):
    import httpx

    _use_fake_gemini(monkeypatch, FakeModels(exc=httpx.ReadTimeout("timed out")))
    with pytest.raises(ai_engine.AIError, match="cevap vermedi"):
        ai_engine._json_call("s", "u", {"type": "object"})


def test_gemini_client_limits_retries(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g")
    opts = ai_engine._gemini_client()._api_client._http_options
    assert opts.retry_options.attempts == config.AI_MAX_ATTEMPTS <= 3
    assert opts.timeout == int(config.AI_TIMEOUT_SECONDS * 1000)


def test_hard_deadline_stops_hanging_call(monkeypatch):
    import time as _t

    monkeypatch.setattr(config, "GEMINI_API_KEY", "g")
    monkeypatch.setattr(config, "AI_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(config, "AI_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(ai_engine, "_deadline", lambda: 0.5)
    monkeypatch.setattr(ai_engine, "_json_call_inner", lambda *a: _t.sleep(3))
    start = _t.monotonic()
    with pytest.raises(ai_engine.AIError, match="cevap vermedi"):
        ai_engine._json_call("s", "u", {"type": "object"})
    assert _t.monotonic() - start < 2


def test_gemini_client_forces_ipv4(monkeypatch):
    import socket

    monkeypatch.setattr(config, "GEMINI_API_KEY", "g")
    monkeypatch.setattr(config, "AI_FORCE_IPV4", True)
    monkeypatch.setattr(socket, "getaddrinfo", ai_engine._original_getaddrinfo)
    seen = []
    monkeypatch.setattr(ai_engine, "_original_getaddrinfo", lambda h, p, f=0, *a: seen.append((h, f)) or [])
    http = ai_engine._gemini_client()._api_client._http_options.httpx_client
    assert http.timeout.connect == 10.0
    socket.getaddrinfo("generativelanguage.googleapis.com", 443)
    socket.getaddrinfo("example.com", 443)
    assert seen == [("generativelanguage.googleapis.com", socket.AF_INET), ("example.com", 0)]


class SequenceModels:
    """Sırayla hata/cevap döndüren sahte Gemini."""

    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(text=out)


def _server_error(code=500):
    from google.genai import errors

    return errors.ServerError(code, {"error": {"code": code, "message": "Internal error", "status": "INTERNAL"}})


def _schema_error():
    from google.genai import errors

    return errors.ClientError(400, {"error": {"code": 400, "message": "The specified schema produces a constraint that has too many states", "status": "INVALID_ARGUMENT"}})


def test_gemini_falls_back_to_schemaless_request(monkeypatch):
    models = SequenceModels([_schema_error(), '{"ok": true}'])
    _use_fake_gemini(monkeypatch, models)
    assert ai_engine._json_call("sistem", "u", {"type": "object"}) == {"ok": True}
    assert models.calls[0]["config"].response_json_schema == {"type": "object"}
    assert models.calls[0]["model"] == models.calls[1]["model"]  # aynı model, şemasız
    second = models.calls[1]["config"]
    assert second.response_json_schema is None and "JSON şemasına" in second.system_instruction


def test_gemini_busy_model_skips_to_next_model(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_MODEL", "ana-model")
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODEL", "yedek-1,yedek-2")
    models = SequenceModels([_server_error(503), _server_error(503), '{"ok": 1}'])
    _use_fake_gemini(monkeypatch, models)
    assert ai_engine._json_call("s", "u", {"type": "object"}) == {"ok": 1}
    assert [c["model"] for c in models.calls] == ["ana-model", "yedek-1", "yedek-2"]


def test_schema_is_simplified_for_gemini():
    full = ai_engine.report_schema(["m1", "m2"])
    simple = ai_engine._simplify_schema_for_gemini(full)
    item = simple["properties"]["bulgular"]["items"]["properties"]
    assert "enum" not in item["madde_id"] and "enum" not in item["yasal_ref"]
    assert "maxItems" not in simple["properties"]["bulgular"]
    assert "enum" in full["properties"]["bulgular"]["items"]["properties"]["madde_id"]  # orijinal değişmedi


def test_gemini_all_attempts_fail_reports_code(monkeypatch):
    models = SequenceModels([_server_error(503)] * 4)
    _use_fake_gemini(monkeypatch, models)
    with pytest.raises(ai_engine.AIError, match="kod 503"):
        ai_engine._json_call("s", "u", {"type": "object"})


def test_invalid_key_is_not_retried(monkeypatch):
    from google.genai import errors

    bad = errors.ClientError(400, {"error": {"code": 400, "message": "API key not valid.", "status": "INVALID_ARGUMENT"}})
    models = SequenceModels([bad, '{"x": 1}'])
    _use_fake_gemini(monkeypatch, models)
    with pytest.raises(ai_engine.AIError, match="anahtarı geçersiz"):
        ai_engine._json_call("s", "u", {"type": "object"})
    assert len(models.calls) == 1
