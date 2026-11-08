"""Trascrizione di immagini (pagine scansionate) con un modello multimodale.

Fornitori: gemini, openai, anthropic, mistral (API OCR dedicata per i modelli "mistral-ocr-*",
altrimenti chat con immagine), vllm (protocollo OpenAI su un indirizzo proprio), ollama.
Il modello deve leggere le immagini. Gli SDK si importano solo per il fornitore scelto
(extra `ocr-<fornitore>` di pa-common).

    ocr = crea_ocr("gemini", "gemini-3-flash-preview", api_key="...")
    testo = await ocr.trascrivi(png)

`Ocr` aggiunge il limite di richieste al minuto e i nuovi tentativi sugli errori transitori
(limite di richieste, sovraccarico, rete); un rifiuto del modello (`OcrRifiutato`) e gli
errori definitivi non si ritentano.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from abc import ABC, abstractmethod

from .errori import ErroreOcr, OcrRifiutato, OcrTransitorio
from .testo import pulisci_trascrizione

log = logging.getLogger("pa_common.ocr")

PROMPT_MARKDOWN = "Transcribe the text in this image precisely. Format the output strictly as Markdown."
PROMPT_TESTO = "Transcribe the text in this image precisely as raw text."


def in_png(immagine: bytes) -> bytes:
    """L'immagine in PNG (formato accettato da tutti i fornitori); PNG e JPEG restano come sono."""
    if immagine.startswith((b"\x89PNG", b"\xff\xd8\xff")):
        return immagine
    from PIL import Image

    with Image.open(io.BytesIO(immagine)) as img:
        uscita = io.BytesIO()
        img.convert("RGB").save(uscita, format="PNG")
        return uscita.getvalue()


def _tipo(immagine: bytes) -> str:
    return "image/jpeg" if immagine.startswith(b"\xff\xd8\xff") else "image/png"


def _data_url(immagine: bytes) -> str:
    return f"data:{_tipo(immagine)};base64," + base64.b64encode(immagine).decode()


class FornitoreOcr(ABC):
    """Un modello che trascrive un'immagine (PNG o JPEG)."""

    nome: str = ""
    modello: str = ""

    @abstractmethod
    async def trascrivi(self, immagine: bytes, prompt: str) -> str:
        """Testo grezzo della pagina. Solleva `OcrTransitorio` o `OcrRifiutato`."""


class OcrGemini(FornitoreOcr):
    nome = "gemini"

    def __init__(self, modello: str, api_key: str):
        from google import genai
        from google.genai import errors, types

        self.modello = modello
        self._client = genai.Client(api_key=api_key)
        self._types = types
        self._errors = errors
        categorie = (
            types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        )
        # atti e documenti amministrativi possono citare reati e violenze: non vanno bloccati
        self._sicurezza = [
            types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_NONE) for c in categorie
        ]

    async def trascrivi(self, immagine, prompt):
        t = self._types
        try:
            risposta = await self._client.aio.models.generate_content(
                model=self.modello,
                contents=[prompt, t.Part.from_bytes(data=immagine, mime_type=_tipo(immagine))],
                config=t.GenerateContentConfig(safety_settings=self._sicurezza),
            )
        except self._errors.ServerError as e:
            raise OcrTransitorio(str(e)) from e
        except self._errors.APIError as e:
            if getattr(e, "code", None) == 429:
                raise OcrTransitorio(str(e)) from e
            raise ErroreOcr(str(e)) from e
        candidato = risposta.candidates[0] if risposta.candidates else None
        motivo = candidato.finish_reason.name if candidato and candidato.finish_reason else "sconosciuto"
        # un rifiuto può arrivare con un testo parziale: non è una trascrizione
        if motivo in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"):
            raise OcrRifiutato(f"Gemini: {motivo}")
        if not candidato or not candidato.content or not candidato.content.parts:
            raise ErroreOcr(f"Gemini: nessun testo (motivo: {motivo})")
        return risposta.text


class OcrOpenAI(FornitoreOcr):
    """OpenAI e server compatibili (vLLM): cambia solo l'indirizzo."""

    nome = "openai"

    def __init__(self, modello: str, api_key: str, base_url: str | None = None):
        from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError

        self.modello = modello
        self._client = AsyncOpenAI(api_key=api_key or "non-serve", base_url=base_url)
        self._transitori = (RateLimitError, APIConnectionError)
        self._stato = APIStatusError

    async def trascrivi(self, immagine, prompt):
        try:
            risposta = await self._client.chat.completions.create(
                model=self.modello,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": _data_url(immagine)}},
                        ],
                    }
                ],
            )
        except self._transitori as e:
            raise OcrTransitorio(str(e)) from e
        except self._stato as e:
            if e.status_code >= 500:
                raise OcrTransitorio(str(e)) from e
            raise ErroreOcr(str(e)) from e
        scelta = risposta.choices[0]
        if scelta.finish_reason == "content_filter":
            raise OcrRifiutato("OpenAI: content_filter")
        return scelta.message.content


class OcrAnthropic(FornitoreOcr):
    nome = "anthropic"
    MAX_TOKEN = 8192  # obbligatorio nella Messages API

    def __init__(self, modello: str, api_key: str):
        import anthropic

        self.modello = modello
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._transitori = (anthropic.RateLimitError, anthropic.APIConnectionError)
        self._stato = anthropic.APIStatusError

    async def trascrivi(self, immagine, prompt):
        dati = base64.b64encode(immagine).decode()
        try:
            risposta = await self._client.messages.create(
                model=self.modello,
                max_tokens=self.MAX_TOKEN,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": _tipo(immagine), "data": dati}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
        except self._transitori as e:
            raise OcrTransitorio(str(e)) from e
        except self._stato as e:
            if e.status_code >= 500:
                raise OcrTransitorio(str(e)) from e
            raise ErroreOcr(str(e)) from e
        if risposta.stop_reason == "refusal":
            raise OcrRifiutato("Anthropic: refusal")
        return "".join(b.text for b in risposta.content if b.type == "text")


class OcrMistral(FornitoreOcr):
    nome = "mistral"

    def __init__(self, modello: str, api_key: str):
        from mistralai.client import Mistral
        from mistralai.client.errors import SDKError

        self.modello = modello
        self._client = Mistral(api_key=api_key)
        self._errore = SDKError

    async def trascrivi(self, immagine, prompt):
        url = _data_url(immagine)
        try:
            if "ocr" in self.modello:
                # l'API OCR non riceve il prompt e restituisce sempre Markdown
                risposta = await self._client.ocr.process_async(
                    model=self.modello, document={"type": "image_url", "image_url": url}
                )
                return "\n\n".join(p.markdown for p in risposta.pages)
            risposta = await self._client.chat.complete_async(
                model=self.modello,
                messages=[
                    {"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": url}]}
                ],
            )
            return risposta.choices[0].message.content
        except self._errore as e:
            stato = getattr(e, "status_code", None) or 0
            if stato == 429 or stato >= 500:
                raise OcrTransitorio(str(e)) from e
            raise ErroreOcr(str(e)) from e


class OcrOllama(FornitoreOcr):
    nome = "ollama"

    def __init__(self, modello: str, host: str | None = None):
        import ollama

        self.modello = modello
        self._client = ollama.AsyncClient(host=host)
        self._errore = ollama.ResponseError

    async def trascrivi(self, immagine, prompt):
        try:
            risposta = await self._client.chat(
                model=self.modello, messages=[{"role": "user", "content": prompt, "images": [immagine]}]
            )
        except ConnectionError as e:
            raise OcrTransitorio(str(e)) from e
        except self._errore as e:
            if e.status_code == 429 or e.status_code >= 500:
                raise OcrTransitorio(str(e)) from e
            raise ErroreOcr(str(e)) from e
        return risposta.message.content


FORNITORI = ("gemini", "openai", "anthropic", "mistral", "vllm", "ollama")


def crea_fornitore(fornitore: str, modello: str, *, api_key: str = "", base_url: str | None = None) -> FornitoreOcr:
    """Il fornitore OCR dal nome. `base_url`: indirizzo per vllm (obbligatorio), ollama e
    openai (facoltativo)."""
    fornitore = (fornitore or "").strip().lower()
    if not modello:
        raise ErroreOcr(f"OCR '{fornitore}': manca il nome del modello")
    if fornitore == "gemini":
        return OcrGemini(modello, api_key)
    if fornitore == "openai":
        return OcrOpenAI(modello, api_key, base_url)
    if fornitore == "vllm":
        if not base_url:
            raise ErroreOcr("OCR vllm: manca l'indirizzo del server (base_url)")
        fornitore_vllm = OcrOpenAI(modello, api_key, base_url)
        fornitore_vllm.nome = "vllm"
        return fornitore_vllm
    if fornitore == "anthropic":
        return OcrAnthropic(modello, api_key)
    if fornitore == "mistral":
        return OcrMistral(modello, api_key)
    if fornitore == "ollama":
        return OcrOllama(modello, base_url)
    raise ErroreOcr(f"Fornitore OCR '{fornitore}' sconosciuto: ammessi {', '.join(FORNITORI)}")


class Ocr:
    """Un fornitore con il limite di richieste al minuto e i nuovi tentativi.

    `al_minuto`: richieste al minuto al massimo (condivise da tutte le conversioni del
    processo); `tentativi`: chiamate al massimo per pagina sugli errori transitori, con attesa
    esponenziale casuale tra `attesa_minima` e `attesa_massima` secondi.
    """

    def __init__(
        self,
        fornitore: FornitoreOcr,
        *,
        al_minuto: int = 10,
        tentativi: int = 6,
        attesa_minima: float = 10,
        attesa_massima: float = 80,
    ):
        self.fornitore = fornitore
        self.al_minuto = al_minuto
        self.tentativi = max(1, tentativi)
        self._attesa = (attesa_minima, attesa_massima)
        self._limitatori: dict[int, object] = {}  # un limitatore per event loop

    def __repr__(self) -> str:
        return f"Ocr({self.fornitore.nome}, {self.fornitore.modello})"

    def _limitatore(self):
        from aiolimiter import AsyncLimiter

        chiave = id(asyncio.get_running_loop())
        if chiave not in self._limitatori:
            self._limitatori[chiave] = AsyncLimiter(max_rate=self.al_minuto, time_period=60)
        return self._limitatori[chiave]

    async def trascrivi(self, immagine: bytes, *, markdown: bool = True) -> str:
        """Testo dell'immagine (PNG, JPEG o altro formato leggibile da Pillow), ripulito.
        Solleva `OcrRifiutato`, `OcrTransitorio` (finiti i tentativi) o `ErroreOcr`."""
        from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_random_exponential

        immagine = in_png(immagine)
        prompt = PROMPT_MARKDOWN if markdown else PROMPT_TESTO
        minima, massima = self._attesa
        async for tentativo in AsyncRetrying(
            retry=retry_if_exception_type(OcrTransitorio),
            wait=wait_random_exponential(multiplier=2, min=minima, max=massima),
            stop=stop_after_attempt(self.tentativi),
            reraise=True,
        ):
            with tentativo:
                async with self._limitatore():
                    testo = pulisci_trascrizione(await self.fornitore.trascrivi(immagine, prompt))
        if not testo:
            raise ErroreOcr("OCR: risposta vuota dal modello")
        return testo


def crea_ocr(
    fornitore: str,
    modello: str,
    *,
    api_key: str = "",
    base_url: str | None = None,
    al_minuto: int = 10,
    tentativi: int = 6,
) -> Ocr:
    """`Ocr` pronto all'uso dal nome del fornitore (vedi `crea_fornitore`)."""
    ocr = Ocr(crea_fornitore(fornitore, modello, api_key=api_key, base_url=base_url), al_minuto=al_minuto, tentativi=tentativi)
    log.info("OCR: %s, modello %s", ocr.fornitore.nome, modello)
    return ocr
