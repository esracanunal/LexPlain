"""Uygulama ayarları. Değerler ortam değişkenlerinden ya da proje kökündeki .env dosyasından okunur."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Ek bağımlılık gerektirmeyen basit .env okuyucu (KEY=VALUE satırları)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

# --- Yapay zekâ sağlayıcısı ---
# AI_PROVIDER: "gemini", "anthropic" ya da boş (otomatik: hangi anahtar varsa o; ikisi de varsa Gemini).
AI_PROVIDER = os.getenv("AI_PROVIDER", "").strip().lower()

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
# Ücretsiz kullanımda en kararlı sonuç veren model (Kasım 2026 denemelerine göre).
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()
# Ana model yoğunsa/hata verirse sırayla denenecek modeller (virgülle ayrılmış; boş bırakılabilir)
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash,gemini-3.5-flash-lite").strip()


def gemini_models() -> list[str]:
    """Denenecek Gemini modelleri: önce ana model, sonra yedekler (tekrarsız)."""
    models = [GEMINI_MODEL] + [m.strip() for m in GEMINI_FALLBACK_MODEL.split(",")]
    return [m for i, m in enumerate(models) if m and m not in models[:i]]

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "claude-sonnet-5-5").strip()  # Anthropic modeli

AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "60"))  # tek istek için süre sınırı
# Kütüphanenin kendi tekrar denemesi (1 = tekrar yok). Yedek denemeleri uygulama kendisi yapar.
AI_MAX_ATTEMPTS = int(os.getenv("AI_MAX_ATTEMPTS", "1"))
# Bazı ev/okul ağlarında IPv6 bağlantısı cevap vermeden asılı kalır; Gemini'ye IPv4 ile bağlan.
AI_FORCE_IPV4 = os.getenv("AI_FORCE_IPV4", "1").strip().lower() not in ("0", "false", "hayir", "no")

# Yükleme sınırları
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "8")) * 1024 * 1024
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "60000"))
MIN_TEXT_CHARS = 40


def active_provider() -> str | None:
    """Kullanılacak yapay zekâ sağlayıcısı: 'gemini', 'anthropic' ya da None (kapalı)."""
    if AI_PROVIDER == "gemini":
        return "gemini" if GEMINI_API_KEY else None
    if AI_PROVIDER == "anthropic":
        return "anthropic" if ANTHROPIC_API_KEY else None
    if GEMINI_API_KEY:
        return "gemini"
    if ANTHROPIC_API_KEY:
        return "anthropic"
    return None


def active_model() -> str | None:
    provider = active_provider()
    if provider == "gemini":
        return GEMINI_MODEL
    if provider == "anthropic":
        return AI_MODEL
    return None


def ai_available() -> bool:
    return active_provider() is not None
