"""Conversioni con LibreOffice headless (soffice): DOC, DOCX, RTF, HTML in ODT per il
Markdown, e documenti in PDF, PDF/A, ODT o DOCX.

Ogni conversione usa un profilo LibreOffice separato in una cartella temporanea, così più
conversioni possono girare in parallelo; il semaforo limita quante (ognuna occupa
300-500 MB).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ..errori import ConversioneNonRiuscita, FormatoNonSupportato, TempoScaduto

log = logging.getLogger("pa_common.libreoffice")

INGRESSI = {".odt", ".docx", ".doc", ".rtf", ".html"}
# L'HTML si apre come documento di Writer (non Writer/Web), così valgono gli stessi filtri
_FILTRI_INGRESSO = {".html": "HTML (StarWriter)"}
FILTRI = {
    "pdf": 'pdf:writer_pdf_Export:{"SelectPdfVersion":{"type":"long","value":"0"}}',
    "pdfa": 'pdf:writer_pdf_Export:{"SelectPdfVersion":{"type":"long","value":"2"}}',
    "odt": "odt",
    "docx": "docx:MS Word 2007 XML",
}


class Occupato(ConversioneNonRiuscita):
    """Troppe conversioni in corso: riprovare più tardi."""


class LibreOffice:
    """`soffice`: percorso del programma; `timeout`: secondi per conversione; `parallele`:
    conversioni contemporanee al massimo; `attesa`: secondi di attesa di un posto libero."""

    def __init__(self, soffice: str = "soffice", *, timeout: float = 120, parallele: int = 2, attesa: float = 25):
        self.soffice = soffice
        self.timeout = timeout
        self.attesa = attesa
        self._semaforo = threading.BoundedSemaphore(parallele)

    def disponibile(self) -> bool:
        return shutil.which(self.soffice) is not None

    def converti(self, contenuto: bytes, estensione: str, formato: str) -> bytes:
        """Il documento (`estensione` di ingresso, es. ".docx") nel `formato` di uscita (pdf,
        pdfa, odt, docx). Solleva `Occupato`, `TempoScaduto` o `ConversioneNonRiuscita`."""
        if estensione not in INGRESSI:
            raise FormatoNonSupportato(f"Formato non supportato da LibreOffice: {estensione or 'sconosciuto'}")
        if formato not in FILTRI:
            raise FormatoNonSupportato(f"Formato di uscita non valido: {formato}")
        if not self._semaforo.acquire(timeout=self.attesa):
            log.warning("Conversione rifiutata: troppe in corso da oltre %.0f s", self.attesa)
            raise Occupato("Servizio di conversione occupato, riprovare")
        try:
            return self._esegui(contenuto, estensione, formato)
        finally:
            self._semaforo.release()

    def _esegui(self, contenuto: bytes, estensione: str, formato: str) -> bytes:
        inizio = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="conv-") as cartella:
            base = Path(cartella)
            sorgente = base / f"documento{estensione}"
            sorgente.write_bytes(contenuto)
            uscita = base / "uscita"
            comando = [
                self.soffice,
                f"-env:UserInstallation={(base / 'profilo').as_uri()}",
                "--headless",
                "--norestore",
                "--nolockcheck",
                *([f"--infilter={_FILTRI_INGRESSO[estensione]}"] if estensione in _FILTRI_INGRESSO else []),
                "--convert-to",
                FILTRI[formato],
                "--outdir",
                str(uscita),
                str(sorgente),
            ]
            try:
                processo = subprocess.run(comando, check=False, capture_output=True, timeout=self.timeout)
            except subprocess.TimeoutExpired as e:
                log.warning("Conversione %s -> %s interrotta dopo %.0f s", estensione, formato, self.timeout)
                raise TempoScaduto("Conversione troppo lunga") from e
            except FileNotFoundError as e:
                raise ConversioneNonRiuscita(f"LibreOffice non installato ({self.soffice})") from e
            risultato = uscita / f"documento.{'pdf' if formato.startswith('pdf') else formato}"
            if processo.returncode != 0 or not risultato.exists():
                log.error("Conversione fallita: %s", processo.stderr.decode(errors="replace")[:500])
                raise ConversioneNonRiuscita("Il documento non può essere convertito")
            dati = risultato.read_bytes()
        log.debug("Conversione %s -> %s in %.1f s (%d byte)", estensione, formato, time.perf_counter() - inizio, len(dati))
        return dati
