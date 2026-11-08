"""Eccezioni della libreria: tutte derivano da `ErrorePaCommon`."""

from __future__ import annotations


class ErrorePaCommon(Exception):
    """Base di tutti gli errori di pa-common."""


# --- file in ingresso ------------------------------------------------------------------------


class FileNonValido(ErrorePaCommon, ValueError):
    """Il file non si può accettare: formato non ammesso, contenuto diverso dall'estensione,
    busta di firma illeggibile."""


class BustaNonValida(FileNonValido):
    """Il contenuto è una busta di firma (PKCS#7/CAdES) ma il documento non si estrae."""


# --- conversione -----------------------------------------------------------------------------


class ConversioneNonRiuscita(ErrorePaCommon, ValueError):
    """Il documento non si converte: danneggiato, vuoto o non leggibile."""


class FormatoNonSupportato(ConversioneNonRiuscita):
    """Formato che la conversione non gestisce."""


class TroppePagine(ConversioneNonRiuscita):
    """PDF oltre il numero massimo di pagine ammesso."""


class OcrNonDisponibile(ConversioneNonRiuscita):
    """Il documento (o una sua pagina) è un'immagine e nessun OCR è configurato."""


class TempoScaduto(ConversioneNonRiuscita, TimeoutError):
    """La conversione ha superato il tempo massimo."""


# --- OCR -------------------------------------------------------------------------------------


class ErroreOcr(ErrorePaCommon):
    """Errore definitivo del modello OCR (configurazione, richiesta rifiutata)."""


class OcrTransitorio(ErroreOcr):
    """Limite di richieste, sovraccarico o rete: la chiamata si può ritentare."""


class OcrRifiutato(ErroreOcr):
    """Il modello ha rifiutato la pagina per le sue regole: ritentare non cambia l'esito."""
