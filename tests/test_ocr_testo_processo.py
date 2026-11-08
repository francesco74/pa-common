import asyncio
import time

import pytest

from pa_common import processo
from pa_common.errori import ConversioneNonRiuscita, ErroreOcr, OcrRifiutato, OcrTransitorio, TempoScaduto
from pa_common.ocr import FornitoreOcr, Ocr, crea_fornitore
from pa_common.testo import markdown_in_testo, normalizza_spazi, pulisci_ocr, pulisci_trascrizione

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10


class FornitoreFinto(FornitoreOcr):
    nome, modello = "finto", "prova"

    def __init__(self, esiti):
        self.esiti = list(esiti)
        self.chiamate = 0

    async def trascrivi(self, immagine, prompt):
        self.chiamate += 1
        esito = self.esiti.pop(0)
        if isinstance(esito, Exception):
            raise esito
        return esito


def _ocr(esiti, tentativi=3):
    fornitore = FornitoreFinto(esiti)
    return fornitore, Ocr(fornitore, al_minuto=1000, tentativi=tentativi, attesa_minima=0, attesa_massima=0)


def test_ocr_ritenta_gli_errori_transitori():
    fornitore, ocr = _ocr([OcrTransitorio("429"), "<think>...</think>```markdown\nTesto della pa-\ngina\n```"])
    assert asyncio.run(ocr.trascrivi(PNG)) == "Testo della pagina"
    assert fornitore.chiamate == 2


def test_ocr_non_ritenta_rifiuti_ed_errori_definitivi():
    fornitore, ocr = _ocr([OcrRifiutato("SAFETY"), "mai"])
    with pytest.raises(OcrRifiutato):
        asyncio.run(ocr.trascrivi(PNG))
    assert fornitore.chiamate == 1
    fornitore, ocr = _ocr([OcrTransitorio("503")] * 3)
    with pytest.raises(OcrTransitorio):
        asyncio.run(ocr.trascrivi(PNG))
    assert fornitore.chiamate == 3
    _, ocr = _ocr(["   "])
    with pytest.raises(ErroreOcr, match="vuota"):
        asyncio.run(ocr.trascrivi(PNG))


def test_crea_fornitore_controlla_la_configurazione():
    with pytest.raises(ErroreOcr, match="modello"):
        crea_fornitore("gemini", "")
    with pytest.raises(ErroreOcr, match="sconosciuto"):
        crea_fornitore("tesseract", "x")
    with pytest.raises(ErroreOcr, match="base_url"):
        crea_fornitore("vllm", "qwen-vl")


def test_pulizia_del_testo():
    assert normalizza_spazi("a&nbsp;&nbsp; b\xa0\nc") == "a b c"
    assert pulisci_ocr("&nbsp;&nbsp;Firma\nVoce   Importo   12\n|  a  |  b  |\n\n\n\nfine  ") == (
        "Firma\nVoce\tImporto\t12\n| a | b |\n\nfine"
    )
    assert pulisci_ocr("```\n  codice   allineato\n```") == "```\n  codice   allineato\n```"
    assert markdown_in_testo("# Titolo\n\n**grassetto** e [link](http://x)\n\n| a | b |\n|---|---|\n| 1 | 2 |") == (
        "Titolo\n\ngrassetto e link\n\na\tb\n\n1\t2"
    )
    assert pulisci_trascrizione(None) == ""


def _lento():
    time.sleep(30)


def _sbagliato():
    raise ValueError("rotto")


def _non_convertibile():
    raise ConversioneNonRiuscita("PDF non leggibile")


def test_processo_separato():
    assert processo.esegui(sum, ([1, 2, 3],), 30) == 6
    inizio = time.monotonic()
    with pytest.raises(TempoScaduto):
        processo.esegui(_lento, (), 1)
    assert time.monotonic() - inizio < 15  # terminato, non atteso
    with pytest.raises(RuntimeError, match="rotto"):
        processo.esegui(_sbagliato, (), 30)
    with pytest.raises(ConversioneNonRiuscita, match="non leggibile"):
        processo.esegui(_non_convertibile, (), 30)
    assert processo.cpu_disponibili() >= 1
