"""Fornitori OCR con i veri SDK contro un server finto (dal test dell'OCR del progetto rag).
Saltati se gli SDK non sono installati (extra `tutto`)."""

import asyncio
import os

import pytest

pytest.importorskip("openai")
pytest.importorskip("anthropic")
pytest.importorskip("google.genai")
pytest.importorskip("mistralai")
pytest.importorskip("ollama")

from server_finto import FakeLLM  # noqa: E402

from pa_common.errori import ErroreOcr, OcrRifiutato, OcrTransitorio  # noqa: E402
from pa_common.ocr import Ocr, OcrGemini, OcrMistral, crea_fornitore  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture(scope="module")
def finto():
    server = FakeLLM()
    vecchie = {k: os.environ.get(k) for k in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "GOOGLE_GEMINI_BASE_URL")}
    os.environ.update(
        OPENAI_BASE_URL=server.url + "/v1", ANTHROPIC_BASE_URL=server.url, GOOGLE_GEMINI_BASE_URL=server.url
    )
    yield server
    for k, v in vecchie.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _ocr(fornitore, tentativi=3):
    return Ocr(fornitore, al_minuto=100000, tentativi=tentativi, attesa_minima=0, attesa_massima=0)


def _errore(coro):
    try:
        asyncio.run(coro)
    except Exception as e:  # noqa: BLE001
        return type(e).__name__
    return None


def test_vllm(finto):
    finto.requests.clear()
    finto.replies = [(200, "# Titolo\ntesto")]
    ocr = _ocr(crea_fornitore("vllm", "qwen-vision", base_url=finto.url + "/v1"))
    assert asyncio.run(ocr.trascrivi(PNG)) == "# Titolo\ntesto"
    percorso, corpo = finto.requests[-1]
    assert (percorso, corpo["model"]) == ("/v1/chat/completions", "qwen-vision")
    contenuto = corpo["messages"][0]["content"]
    assert contenuto[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "Markdown" in contenuto[0]["text"]

    finto.replies = [(429, {"error": {"message": "lento"}})] * 2 + [(200, "dopo il rate limit")]
    assert asyncio.run(ocr.trascrivi(PNG)) == "dopo il rate limit"
    finto.replies = [(500, {"error": {"message": "giù"}})] * 30
    assert _errore(ocr.trascrivi(PNG)) == "OcrTransitorio"
    finto.requests.clear()
    finto.replies = [(400, {"error": {"message": "modello senza immagini"}})]
    assert _errore(ocr.trascrivi(PNG)) == "ErroreOcr"
    assert len(finto.requests) == 1  # nessun nuovo tentativo
    filtrata = (200, {"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": [
        {"index": 0, "finish_reason": "content_filter", "message": {"role": "assistant", "content": ""}}]})  # fmt: skip
    finto.replies = [filtrata]
    assert _errore(ocr.trascrivi(PNG)) == "OcrRifiutato"
    finto.replies = []


def test_gemini_un_rifiuto_non_e_una_trascrizione(finto):
    gemini = OcrGemini("gemini-prova", api_key="k")

    def risposta(motivo, parti):
        contenuto = {"role": "model", "parts": parti} if parti else {}
        finto.replies = [(200, {"candidates": [{"finishReason": motivo, "content": contenuto}]})]
        return _errore(gemini.trascrivi(PNG, "p"))

    assert risposta("RECITATION", None) == "OcrRifiutato"
    assert risposta("RECITATION", [{"text": "parziale"}]) == "OcrRifiutato"
    assert risposta("STOP", [{"text": "ok"}]) is None
    assert "gemini-prova" in finto.requests[-1][0]


def test_openai_e_anthropic(finto):
    finto.replies = [(200, "openai ok")]
    assert asyncio.run(crea_fornitore("openai", "gpt", api_key="k").trascrivi(PNG, "p")) == "openai ok"
    finto.replies = [(200, "claude ok")]
    assert asyncio.run(crea_fornitore("anthropic", "claude", api_key="k").trascrivi(PNG, "p")) == "claude ok"
    assert finto.requests[-1][1]["messages"][0]["content"][0]["source"]["media_type"] == "image/png"


def test_mistral_chat_e_api_ocr(finto):
    chat = OcrMistral("pixtral", api_key="k")
    chat._client.sdk_configuration.server_url = finto.url
    finto.replies = [(200, "pixtral ok")]
    assert asyncio.run(chat.trascrivi(PNG, "p")) == "pixtral ok"
    dedicato = OcrMistral("mistral-ocr-latest", api_key="k")
    dedicato._client.sdk_configuration.server_url = finto.url
    finto.replies = [(200, {"model": "m", "pages": [
        {"index": 0, "markdown": "pagina", "images": [], "dimensions": None}], "usage_info": {"pages_processed": 1}})]  # fmt: skip
    assert asyncio.run(dedicato.trascrivi(PNG, "p")) == "pagina"
    assert finto.requests[-1][0] == "/v1/ocr"


def test_ollama_si_crea():
    assert crea_fornitore("ollama", "llava", base_url="http://localhost:11434").nome == "ollama"


def test_errori_tipizzati():
    assert issubclass(OcrTransitorio, ErroreOcr) and issubclass(OcrRifiutato, ErroreOcr)
