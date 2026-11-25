"""LexPlain: FastAPI uygulaması.

Çalıştırma:  uvicorn app.main:app --reload
Arayüz:      http://127.0.0.1:8000
API belgesi: http://127.0.0.1:8000/docs
"""
import logging
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai_engine, analyzer, config
from .extract import ExtractionError, SUPPORTED_EXT, extract_text
from .report import CONTRACT_TYPES, ROLES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")

app = FastAPI(
    title="LexPlain API",
    description="Kira ve hizmet sözleşmelerindeki riskli maddeleri yapay zekâ ile sade Türkçeye çeviren servis.",
    version="1.0.0",
)

STATIC_DIR = config.BASE_DIR / "static"
SAMPLES_DIR = config.BASE_DIR / "samples"

ContractType = Literal["otomatik", "kira", "hizmet", "is", "diger"]
Role = Literal["otomatik", "kiraci", "kiraya_veren", "musteri", "firma", "calisan", "isveren", "taraf"]
Mode = Literal["otomatik", "ai", "kural"]


class AnalyzeRequest(BaseModel):
    metin: str = Field(..., description="Sözleşme metni")
    sozlesme_turu: ContractType = "otomatik"
    rol: Role = "otomatik"
    rol_aciklama: str = Field("", max_length=120, description="'Diğer' türde kullanıcının kendi rol tanımı")
    mod: Mode = "otomatik"


class ChatTurn(BaseModel):
    rol: Literal["user", "assistant"]
    metin: str


class AskRequest(BaseModel):
    metin: str
    soru: str = Field(..., min_length=2, max_length=1000)
    sozlesme_turu: ContractType = "otomatik"
    rol: Role = "otomatik"
    rol_aciklama: str = Field("", max_length=120)
    gecmis: list[ChatTurn] = []


def _check_text(text: str) -> str:
    text = (text or "").strip()
    if len(text) < config.MIN_TEXT_CHARS:
        raise HTTPException(422, "Metin çok kısa. Lütfen sözleşmenin tamamını yapıştırın.")
    if len(text) > config.MAX_TEXT_CHARS:
        raise HTTPException(413, f"Metin çok uzun (en fazla {config.MAX_TEXT_CHARS:,} karakter).".replace(",", "."))
    return text


@app.get("/api/health")
def health():
    return {"durum": "ok", "yapay_zeka": ai_engine.is_enabled(), "saglayici": ai_engine.provider_name(), "model": config.active_model()}


@app.get("/api/meta")
def meta():
    """Arayüzün ihtiyaç duyduğu seçenekler."""
    return {
        "sozlesme_turleri": CONTRACT_TYPES,
        "roller": ROLES,
        "dosya_turleri": sorted(SUPPORTED_EXT),
        "max_mb": config.MAX_UPLOAD_BYTES // (1024 * 1024),
        "yapay_zeka": ai_engine.is_enabled(),
        "saglayici": ai_engine.provider_name(),
    }


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    text = _check_text(req.metin)
    return analyzer.analyze(text, req.sozlesme_turu, req.rol, req.mod, req.rol_aciklama)


@app.post("/api/analyze-file")
async def analyze_file(
    dosya: UploadFile = File(...),
    sozlesme_turu: ContractType = Form("otomatik"),
    rol: Role = Form("otomatik"),
    mod: Mode = Form("otomatik"),
    rol_aciklama: str = Form("", max_length=120),
):
    data = await dosya.read(config.MAX_UPLOAD_BYTES + 1)
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Dosya çok büyük (en fazla {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    try:
        text = extract_text(dosya.filename or "", data)
    except ExtractionError as exc:
        raise HTTPException(422, str(exc)) from exc
    text = _check_text(text)
    result = analyzer.analyze(text, sozlesme_turu, rol, mod, rol_aciklama)
    result["dosya_adi"] = dosya.filename
    return result


@app.post("/api/ask")
def ask(req: AskRequest):
    text = _check_text(req.metin)
    history = [t.model_dump() for t in req.gecmis]
    return analyzer.answer_question(text, req.soru.strip(), req.sozlesme_turu, req.rol, history, req.rol_aciklama)


@app.get("/api/samples/{name}")
def sample(name: Literal["kira", "hizmet", "is"]):
    path = SAMPLES_DIR / f"ornek_{name}_sozlesmesi.txt"
    return {"metin": path.read_text(encoding="utf-8")}


# --- Ön yüz (saf HTML/CSS/JS) ---
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
