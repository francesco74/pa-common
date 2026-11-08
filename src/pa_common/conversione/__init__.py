"""Documenti in Markdown, il testo letto dagli LLM.

    from pa_common.conversione import in_markdown
    testo = await in_markdown(contenuto, "determina.pdf.p7m", ocr=ocr)

| Formato | Come |
|---|---|
| PDF | PyMuPDF4LLM, OCR delle pagine scansionate (`conversione.pdf`) |
| ODT | lettura diretta dell'XML (`conversione.odt`) |
| DOC, DOCX, RTF, HTML | LibreOffice in ODT, poi come sopra |
| XLSX, XLS, ODS | una tabella per foglio (`conversione.fogli`) |
| PNG, JPG, TIF | OCR |
| TXT, MD, CSV | il testo così com'è |

Prima della conversione `pa_common.ingresso.verifica` sbusta le firme .p7m e controlla che
il contenuto corrisponda all'estensione.
"""

from __future__ import annotations

import asyncio

from .. import ingresso, processo
from ..errori import ConversioneNonRiuscita, FormatoNonSupportato, OcrNonDisponibile
from .fogli import FORMATI as FORMATI_FOGLI
from .fogli import fogli_in_markdown
from .libreoffice import LibreOffice
from .odt import odt_in_markdown
from .pdf import Statistiche, Tempi, pdf_in_markdown

__all__ = ["FORMATI", "LibreOffice", "Statistiche", "Tempi", "in_markdown", "in_markdown_sync", "testo"]

VIA_LIBREOFFICE = {".docx", ".doc", ".rtf", ".html"}
IMMAGINI = ingresso.IMMAGINI
TESTI = ingresso.TESTI
FORMATI = {".pdf", ".odt"} | VIA_LIBREOFFICE | FORMATI_FOGLI | IMMAGINI | TESTI

_libreoffice: LibreOffice | None = None


def _libreoffice_predefinito() -> LibreOffice:
    global _libreoffice
    if _libreoffice is None:
        _libreoffice = LibreOffice()
    return _libreoffice


def testo(contenuto: bytes) -> str:
    """Un file di testo: UTF-8, altrimenti la codifica di Windows (CSV esportati da Excel)."""
    for codifica in ("utf-8-sig", "cp1252"):
        try:
            return contenuto.decode(codifica)
        except UnicodeDecodeError:
            continue
    return contenuto.decode("latin-1")


async def in_markdown(
    contenuto: bytes,
    nome: str,
    *,
    ocr=None,
    libreoffice: LibreOffice | None = None,
    max_pagine: int | None = None,
    separatore_pagine: str = "\n\n",
    tempi: Tempi | None = None,
    in_processo: bool = True,
    annota_formato: bool = False,
    statistiche: Statistiche | None = None,
    verifica_ingresso: bool = True,
) -> str:
    """Testo Markdown del documento `contenuto` di nome `nome` (l'estensione dice il formato).

    - `ocr`: `pa_common.ocr.Ocr` per scansioni e immagini; senza, solleva `OcrNonDisponibile`;
    - `libreoffice`: per DOC/DOCX/RTF/HTML (predefinito: `soffice` nel PATH);
    - `max_pagine`: PDF più lunghi rifiutati (`TroppePagine`);
    - `separatore_pagine`: tra le pagine del PDF;
    - `tempi`, `in_processo`: estrazione del PDF e dei fogli in un processo separato con tempi
      massimi (vedi `conversione.pdf`);
    - `annota_formato`: nei fogli, i numeri riportano anche il formato interno;
    - `statistiche`: riempito con le pagine e quelle passate dall'OCR;
    - `verifica_ingresso`: sbusta le firme e controlla il contenuto (`ingresso.verifica`).

    Solleva `FileNonValido` (ingresso) o `ConversioneNonRiuscita` e sottoclassi.
    """
    if verifica_ingresso:
        f = ingresso.verifica(contenuto, nome)
        contenuto, est = f.contenuto, f.estensione
    else:
        est = ingresso.estensione(nome)
    tempi = tempi or Tempi()

    if est == ".pdf":
        return await pdf_in_markdown(
            contenuto,
            ocr=ocr,
            max_pagine=max_pagine,
            separatore=separatore_pagine,
            tempi=tempi,
            in_processo=in_processo,
            statistiche=statistiche,
        )
    if est == ".odt":
        return await asyncio.to_thread(odt_in_markdown, contenuto)
    if est in VIA_LIBREOFFICE:
        lo = libreoffice or _libreoffice_predefinito()
        odt = await asyncio.to_thread(lo.converti, contenuto, est, "odt")
        return await asyncio.to_thread(odt_in_markdown, odt)
    if est in FORMATI_FOGLI:
        # un foglio enorme non deve bloccare il servizio: processo separato con tempo massimo
        if in_processo:
            return await asyncio.to_thread(processo.esegui, _fogli, (contenuto, est, annota_formato), tempi.semplice)
        return await asyncio.to_thread(_fogli, contenuto, est, annota_formato)
    if est in IMMAGINI:
        if ocr is None:
            raise OcrNonDisponibile("Le immagini si leggono solo con l'OCR, che non è configurato")
        if statistiche is not None:
            statistiche.pagine += 1
            statistiche.pagine_ocr += 1
        return (await ocr.trascrivi(contenuto, markdown=True)).strip() + "\n"
    if est in TESTI:
        risultato = testo(contenuto)
        if not risultato.strip():
            raise ConversioneNonRiuscita("Il file è vuoto")
        return risultato
    raise FormatoNonSupportato(f"Formato non supportato: {est or 'sconosciuto'}")


def _fogli(contenuto: bytes, est: str, annota: bool) -> str:
    # funzione di modulo: serve al processo separato
    return fogli_in_markdown(contenuto, est, annota_formato=annota)


def in_markdown_sync(contenuto: bytes, nome: str, **opzioni) -> str:
    """`in_markdown` da codice sincrono (es. un servizio Flask): stesse opzioni."""
    return asyncio.run(in_markdown(contenuto, nome, **opzioni))
