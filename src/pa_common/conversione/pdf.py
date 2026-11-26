"""PDF in Markdown: testo nativo con PyMuPDF4LLM, OCR per le pagine scansionate.

Ogni pagina si classifica (`ispeziona_pagina`): con poco testo, o con tanti tracciati
vettoriali e poco testo (disegni CAD, testo vettorializzato), va all'OCR; le altre passano a
PyMuPDF4LLM, che ricostruisce titoli e tabelle dall'impaginazione.

L'estrazione gira in un processo separato con una catena di ripieghi, ognuno solo se il
precedente supera il suo tempo massimo:

1. PyMuPDF4LLM con il layout AI (ONNX), la qualità migliore su tabelle e colonne;
2. PyMuPDF4LLM in modalità semplice, senza ONNX;
3. OCR di tutte le pagine, rasterizzate.

Con `blocca_scansioni` le pagine non vanno all'OCR: quelle che sono scansioni
(`pagina_scansionata`: poco testo e coperte in buona parte da un'immagine che non sia uno
sfondo ripetuto in più pagine, come la carta intestata) fermano la
conversione con `DocumentoScansionato`; le altre con poco testo (l'ultima con la sola firma,
una pagina bianca, un grafico vettoriale) danno il testo che hanno. Un PDF ricercabile (la
scansione con il testo dell'OCR incorporato) ha il testo e non è una scansione.

Intestazioni e piè di pagina: PyMuPDF4LLM li tiene (la sua esclusione automatica scarta
anche i titoli in cima alla pagina, es. "Art. 22 Dirigenti") e poi si tolgono le righe ai
bordi che si ripetono in almeno metà delle pagine, e i numeri di pagina.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
from dataclasses import dataclass

from ..errori import ConversioneNonRiuscita, DocumentoScansionato, OcrNonDisponibile, TempoScaduto, TroppePagine
from .. import processo

log = logging.getLogger("pa_common.pdf")

MIN_CARATTERI = 50  # sotto, la pagina è un'immagine
MAX_VETTORI = 1500  # sopra, con meno di MAX_CARATTERI_VETTORIALE caratteri: disegno o testo vettorializzato
MAX_CARATTERI_VETTORIALE = 1000
ZOOM_OCR = 2  # risoluzione delle pagine rasterizzate per l'OCR (2 = 144 dpi)
SOGLIA_SCANSIONE = 0.5  # superficie della pagina coperta da immagini, oltre la quale (con poco testo) è una scansione


@dataclass
class Tempi:
    """Secondi massimi per ogni livello della catena di estrazione."""

    layout_ai: float = 600
    semplice: float = 300
    rasterizza: float = 300


@dataclass
class Statistiche:
    """Pagine del documento e quante sono passate dall'OCR (per i registri dei progetti)."""

    pagine: int = 0
    pagine_ocr: int = 0


def ispeziona_pagina(pagina, ritaglio: int = 0):
    """(area, caratteri, vettori, serve_ocr) di una pagina PyMuPDF: la regola che decide
    quali pagine vanno all'OCR. `ritaglio`: punti esclusi in alto e in basso."""
    import pymupdf

    try:
        vettori = len(pagina.get_drawings())
    except Exception:  # noqa: BLE001 - pagine con tracciati corrotti
        vettori = 0
    r = pagina.rect
    area = pymupdf.Rect(0, ritaglio, r.width, r.height - ritaglio) if r.height > 200 else r
    caratteri = len(pagina.get_text(sort=True, clip=area).strip())
    serve_ocr = caratteri < MIN_CARATTERI or (vettori > MAX_VETTORI and caratteri < MAX_CARATTERI_VETTORIALE)
    return area, caratteri, vettori, serve_ocr


def copertura_immagini(pagina, escluse: frozenset[int] = frozenset()) -> float:
    """Frazione della pagina coperta da immagini raster (0-1), senza quelle in `escluse`
    (xref). Gli scanner possono dividere la pagina in più strisce: si sommano."""
    import pymupdf

    r = pagina.rect
    if r.is_empty:
        return 0.0
    coperta = 0.0
    for info in pagina.get_image_info(xrefs=True):
        if info.get("xref") in escluse:
            continue
        b = pymupdf.Rect(info["bbox"]) & r
        if not b.is_empty:
            coperta += b.width * b.height
    return min(1.0, coperta / (r.width * r.height))


def immagini_ripetute(documento) -> frozenset[int]:
    """Immagini (xref) presenti in più pagine: sfondi e carta intestata, non scansioni (ogni
    pagina scansionata ha la sua immagine)."""
    pagine_per_immagine: dict[int, int] = {}
    for pagina in documento:
        for xref in {img[0] for img in pagina.get_images()}:
            pagine_per_immagine[xref] = pagine_per_immagine.get(xref, 0) + 1
    return frozenset(x for x, n in pagine_per_immagine.items() if x and n > 1)


def pagina_scansionata(pagina, caratteri: int, sfondi: frozenset[int] = frozenset()) -> bool:
    """Una scansione: poco testo nativo e un'immagine su buona parte della pagina, che non
    sia uno sfondo ripetuto (`immagini_ripetute`). Più stretta di `serve_ocr`, che manda
    all'OCR anche le pagine quasi vuote e i disegni."""
    return caratteri < MIN_CARATTERI and copertura_immagini(pagina, sfondi) >= SOGLIA_SCANSIONE


def _apri(contenuto: bytes):
    import pymupdf

    try:
        return pymupdf.open(stream=contenuto, filetype="pdf")
    except (RuntimeError, ValueError) as e:
        raise ConversioneNonRiuscita("PDF non leggibile") from e


def conta_pagine(contenuto: bytes) -> int:
    with _apri(contenuto) as documento:
        return len(documento)


def _limita_thread_onnx() -> None:
    """ONNX Runtime usa le CPU dell'host, non quelle del container: in un pod con un limite
    basso crea troppi thread e rallenta. Si fissano su quelle disponibili, per ogni sessione
    (anche quelle interne del layout di PyMuPDF4LLM)."""
    try:
        import onnxruntime as ort
    except ImportError:
        return
    if getattr(ort.SessionOptions, "_pa_common_limitato", False):
        return
    limite = processo.cpu_disponibili()
    originale = ort.SessionOptions.__init__

    def __init__(self, *args, **kwargs):
        originale(self, *args, **kwargs)
        self.intra_op_num_threads = limite
        self.inter_op_num_threads = limite

    ort.SessionOptions.__init__ = __init__
    ort.SessionOptions._pa_common_limitato = True


def _markdown_pagina(documento, numero: int) -> str:
    import pymupdf4llm

    opzioni = {"pages": [numero], "header": True, "footer": True, "show_progress": False}
    messaggi = io.StringIO()
    with contextlib.redirect_stdout(messaggi):
        try:
            testo = pymupdf4llm.to_markdown(documento, use_ocr=False, **opzioni)
        except TypeError:  # versioni senza il parametro use_ocr
            testo = pymupdf4llm.to_markdown(documento, **opzioni)
    if messaggi.getvalue().strip():
        log.debug("PyMuPDF4LLM: %s", messaggi.getvalue().strip()[:500])
    return testo.replace("<br>", "\n")


def estrai_pagine(
    contenuto: bytes, layout_ai: bool = True, ritaglio: int = 0, blocca_scansioni: bool = False
) -> list[tuple[str, object]]:
    """Pagina per pagina ("testo", markdown) oppure ("immagine", png da trascrivere). Con
    `blocca_scansioni` niente immagini: ("scansione", None) per le scansioni, il testo per le
    altre. Lavoro di CPU: da eseguire in un processo separato (`processo.esegui`)."""
    import pymupdf
    import pymupdf4llm

    _limita_thread_onnx()
    if hasattr(pymupdf4llm, "use_layout"):
        pymupdf4llm.use_layout(layout_ai)
    pagine: list[tuple[str, object]] = []
    with _apri(contenuto) as documento:
        sfondi = immagini_ripetute(documento) if blocca_scansioni else frozenset()
        for numero in range(len(documento)):
            pagina = documento[numero]
            area, caratteri, vettori, serve_ocr = ispeziona_pagina(pagina, ritaglio)
            if blocca_scansioni:
                if serve_ocr and pagina_scansionata(pagina, caratteri, sfondi):
                    pagine.append(("scansione", None))
                else:
                    pagine.append(("testo", _markdown_pagina(documento, numero)))
            elif serve_ocr:
                log.debug("Pagina %d all'OCR (%d caratteri, %d vettori)", numero + 1, caratteri, vettori)
                immagine = pagina.get_pixmap(matrix=pymupdf.Matrix(ZOOM_OCR, ZOOM_OCR), alpha=False, clip=area)
                pagine.append(("immagine", immagine.tobytes("png")))
            else:
                pagine.append(("testo", _markdown_pagina(documento, numero)))
    return pagine


def rasterizza(contenuto: bytes) -> list[bytes]:
    """Tutte le pagine come PNG, per l'OCR completo (ultimo ripiego)."""
    import pymupdf

    with _apri(contenuto) as documento:
        return [
            documento[n].get_pixmap(matrix=pymupdf.Matrix(ZOOM_OCR, ZOOM_OCR), alpha=False).tobytes("png")
            for n in range(len(documento))
        ]


# --- intestazioni e piè di pagina ------------------------------------------------------------

_NUMERO_PAGINA = re.compile(r"^[-–\s]*(?:pag(?:ina|\.)?\s*)?\d{1,4}(?:\s*(?:di|/)\s*\d{1,4})?[-–\s]*$", re.IGNORECASE)
_RIGHE_BORDO = 3  # righe in cima e in fondo alla pagina dove stanno intestazioni e piè
_RIGHE_VUOTE = re.compile(r"\n{3,}")


def _chiave_riga(riga: str) -> str:
    """Forma confrontabile tra pagine: senza formattazione, numeri e maiuscole."""
    riga = re.sub(r"[#*_`>|]", " ", riga)
    return " ".join(re.sub(r"\d+", "#", riga).lower().split())


def senza_intestazioni(pagine: list[str]) -> list[str]:
    """Toglie intestazioni e piè di pagina: le righe ai bordi della pagina che si ripetono in
    almeno metà delle pagine (almeno due), e i numeri di pagina."""
    bordi = []
    for testo in pagine:
        righe = [i for i, r in enumerate(testo.splitlines()) if r.strip()]
        bordi.append(set(righe[:_RIGHE_BORDO] + righe[-_RIGHE_BORDO:]))
    conteggio: dict[str, int] = {}
    for testo, indici in zip(pagine, bordi):
        righe = testo.splitlines()
        for chiave in {_chiave_riga(righe[i]) for i in indici}:
            conteggio[chiave] = conteggio.get(chiave, 0) + 1
    soglia = max(2, (len(pagine) + 1) // 2)
    ripetute = {k for k, n in conteggio.items() if k and n >= soglia}

    def da_togliere(riga: str) -> bool:
        chiave = _chiave_riga(riga)
        return chiave in ripetute or bool(_NUMERO_PAGINA.match(chiave.replace("#", "1")))

    risultato = []
    for testo, indici in zip(pagine, bordi):
        righe = testo.splitlines()
        risultato.append("\n".join(r for i, r in enumerate(righe) if i not in indici or not da_togliere(r)))
    return risultato


# --- catena completa -------------------------------------------------------------------------


async def _esegui(funzione, argomenti: tuple, timeout: float, in_processo: bool):
    if in_processo:
        return await asyncio.to_thread(processo.esegui, funzione, argomenti, timeout)
    return await asyncio.to_thread(funzione, *argomenti)


async def pdf_in_markdown(
    contenuto: bytes,
    *,
    ocr=None,
    max_pagine: int | None = None,
    separatore: str = "\n\n",
    tempi: Tempi | None = None,
    in_processo: bool = True,
    statistiche: Statistiche | None = None,
    blocca_scansioni: bool = False,
) -> str:
    """Testo Markdown del PDF.

    `ocr`: un `pa_common.ocr.Ocr` per le pagine scansionate; senza, un PDF con pagine
    scansionate solleva `OcrNonDisponibile`. `blocca_scansioni`: niente OCR, le pagine che
    sono scansioni sollevano `DocumentoScansionato` (vedi sopra). `separatore`: tra una pagina e l'altra (es.
    "\\n\\n---\\n\\n" per tenerle distinte). `in_processo=False` estrae nel processo corrente,
    senza tempi massimi (per i test). Solleva `ConversioneNonRiuscita` e sottoclassi.
    """
    tempi = tempi or Tempi()
    statistiche = statistiche if statistiche is not None else Statistiche()
    pagine_totali = conta_pagine(contenuto)
    statistiche.pagine = pagine_totali
    if max_pagine and pagine_totali > max_pagine:
        raise TroppePagine(f"Il PDF ha {pagine_totali} pagine, oltre il limite di {max_pagine}")

    try:
        pagine = await _esegui(estrai_pagine, (contenuto, True, 0, blocca_scansioni), tempi.layout_ai, in_processo)
    except TempoScaduto:
        log.warning("PDF: estrazione con layout AI oltre %.0f s, provo la modalità semplice", tempi.layout_ai)
        try:
            pagine = await _esegui(estrai_pagine, (contenuto, False, 0, blocca_scansioni), tempi.semplice, in_processo)
        except TempoScaduto:
            log.warning("PDF: anche la modalità semplice oltre %.0f s, OCR di tutte le pagine", tempi.semplice)
            if ocr is None or blocca_scansioni:
                raise OcrNonDisponibile("PDF troppo complesso da leggere e OCR non configurato") from None
            try:
                immagini = await _esegui(rasterizza, (contenuto,), tempi.rasterizza, in_processo)
            except TempoScaduto as e:
                raise TempoScaduto("PDF non leggibile: tempo scaduto a ogni livello di estrazione") from e
            pagine = [("immagine", png) for png in immagini]

    scansioni = [n for n, (tipo, _) in enumerate(pagine, start=1) if tipo == "scansione"]
    if scansioni:
        raise DocumentoScansionato(
            f"{len(scansioni)} pagine su {len(pagine)} sono scansioni (immagini senza testo)", scansioni, len(pagine)
        )
    da_trascrivere = sum(1 for tipo, _ in pagine if tipo == "immagine")
    if da_trascrivere and ocr is None:
        if da_trascrivere == len(pagine):
            raise OcrNonDisponibile(
                "Il PDF non contiene testo: probabilmente è una scansione e l'OCR non è configurato. "
                "Caricare il PDF nativo o l'originale ODT/DOCX."
            )
        raise OcrNonDisponibile(
            f"{da_trascrivere} pagine su {len(pagine)} sono scansionate e l'OCR non è configurato"
        )
    testi = []
    for tipo, dato in pagine:
        if tipo == "immagine":
            statistiche.pagine_ocr += 1
            testi.append(await ocr.trascrivi(dato, markdown=True))
        else:
            testi.append(dato)

    testo = separatore.join(t.strip("\n") for t in senza_intestazioni(testi))
    testo = _RIGHE_VUOTE.sub("\n\n", testo).strip()
    if len(re.sub(r"[\s#*|\-]", "", testo)) < MIN_CARATTERI:
        raise ConversioneNonRiuscita("Il PDF non contiene testo leggibile")
    return testo + "\n"
