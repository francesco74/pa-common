import asyncio
import io
import shutil
from datetime import date

import pytest

from aiuti import OcrFinto, busta_p7m, odt, pdf
from pa_common.conversione import Statistiche, Tempi, in_markdown, in_markdown_sync
from pa_common.conversione.fogli import fogli_in_markdown, numero_italiano
from pa_common.conversione.odt import odt_in_markdown
from pa_common.conversione.pdf import senza_intestazioni
from pa_common.errori import ConversioneNonRiuscita, FormatoNonSupportato, OcrNonDisponibile, TroppePagine

serve_libreoffice = pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice non installato")


def converti(contenuto: bytes, nome: str, **opzioni) -> str:
    opzioni.setdefault("in_processo", False)
    return asyncio.run(in_markdown(contenuto, nome, **opzioni))


# --- ODT -------------------------------------------------------------------------------------


def test_odt():
    testo = odt_in_markdown(odt())
    assert "# Determinazione" in testo
    assert "## Premesse" in testo  # titolo dallo stile derivato
    assert "Visto il **d.lgs. 33/2013** e la *legge 190*[^1];" in testo
    assert "a) prima riga;  \nb) seconda riga." in testo  # a capo forzato conservato
    assert "- voce uno\n- voce due" in testo
    assert "| Capitolo | Importo |\n|---|---|\n| 1010\\|A | 1.234,50 |" in testo  # colonne vuote tolte
    assert testo.rstrip().endswith("[^1]: Nota di prova.")


def test_odt_non_valido():
    with pytest.raises(ConversioneNonRiuscita):
        odt_in_markdown(b"PK\x03\x04 non un vero zip")


@serve_libreoffice
def test_docx_con_tabella_a_celle_unite():
    """DOCX via LibreOffice: le tabelle con celle unite restano (pandoc le perde)."""
    from pa_common.conversione import LibreOffice

    html = (
        b"<html><body><h1>Bilancio</h1><p>Testo <i>corsivo</i></p>"
        b"<table border=1><tr><td colspan=2>Entrate correnti</td><td>Anno</td></tr>"
        b"<tr><td>Titolo 1</td><td>Tributi</td><td>1.000</td></tr></table></body></html>"
    )
    lo = LibreOffice()
    docx = lo.converti(html, ".html", "docx")
    testo = converti(docx, "bilancio.docx", libreoffice=lo)
    assert "# Bilancio" in testo
    assert "*corsivo*" in testo
    assert "| Titolo 1 | Tributi | 1.000 |" in testo
    assert "Entrate correnti" in testo


# --- PDF -------------------------------------------------------------------------------------


_TESTO = "Il dirigente determina di approvare la spesa per il servizio di manutenzione degli immobili."


def _corpo(n: int) -> str:
    """Pagina realistica: righe diverse tra loro e da pagina a pagina."""
    parole = ["visto", "considerato", "ritenuto", "dato atto", "accertato", "rilevato", "preso atto", "richiamato"]
    return "\n".join(f"{parole[(n + i) % len(parole)].capitalize()} il punto {chr(97 + i)} della pagina {n}: {_TESTO[: 40 + 3 * i]}"
                     for i in range(8))


def test_pdf_toglie_intestazioni_e_numeri_di_pagina():
    contenuto = pdf([f"Art. {i} Oggetto\n{_corpo(i)}" for i in range(1, 4)], intestazione="PROVINCIA DI LUCCA")
    statistiche = Statistiche()
    testo = converti(contenuto, "atto.pdf", statistiche=statistiche)
    assert "PROVINCIA DI LUCCA" not in testo
    assert "Pagina" not in testo
    assert "punto h della pagina 3" in testo
    assert (statistiche.pagine, statistiche.pagine_ocr) == (3, 0)


def test_pdf_separatore_di_pagina():
    contenuto = pdf([_corpo(1), _corpo(2)])
    testo = converti(contenuto, "atto.pdf", separatore_pagine="\n\n---\n\n")
    assert testo.count("\n---\n") == 1


def test_pdf_con_pagine_scansionate_usa_l_ocr():
    contenuto = pdf([_corpo(1), "", _corpo(3)], scansionate={1})
    ocr = OcrFinto()
    statistiche = Statistiche()
    testo = converti(contenuto, "atto.pdf", ocr=ocr, statistiche=statistiche)
    assert ocr.chiamate == 1
    assert ocr.testo in testo
    assert statistiche.pagine_ocr == 1
    with pytest.raises(OcrNonDisponibile, match="1 pagine su 3"):
        converti(contenuto, "atto.pdf")


def test_pdf_tutto_scansionato_senza_ocr():
    with pytest.raises(OcrNonDisponibile, match="scansione"):
        converti(pdf(["", ""], scansionate={0, 1}), "atto.pdf")


def test_pdf_firmato_e_troppe_pagine():
    contenuto = pdf([_corpo(n) for n in range(3)])
    assert "punto h della pagina 2" in converti(busta_p7m(contenuto), "atto.pdf.p7m")
    with pytest.raises(TroppePagine):
        converti(contenuto, "atto.pdf", max_pagine=2)


def test_pdf_in_un_processo_separato():
    """Il percorso di produzione: estrazione in un processo dedicato con tempo massimo."""
    testo = converti(pdf([_corpo(1)]), "atto.pdf", in_processo=True, tempi=Tempi(layout_ai=120))
    assert "punto h della pagina 1" in testo


def test_senza_intestazioni():
    pagine = ["Comune di Prova\nTesto A\n1", "Comune di Prova\nTesto B\n2", "Comune di Prova\nTesto C\n3"]
    assert senza_intestazioni(pagine) == ["Testo A", "Testo B", "Testo C"]
    # le righe che compaiono in una sola pagina restano
    assert senza_intestazioni(["Titolo\nTesto A", "Altro\nTesto B"]) == ["Titolo\nTesto A", "Altro\nTesto B"]


# --- fogli di calcolo ------------------------------------------------------------------------


def _xlsx() -> bytes:
    import openpyxl

    libro = openpyxl.Workbook()
    foglio = libro.active
    foglio.title = "Spese"
    foglio.append(["Capitolo", "Importo", "Data"])
    foglio.append(["Manutenzione", 1234.5, date(2026, 3, 1)])
    foglio["B2"].number_format = "#,##0.00"
    secondo = libro.create_sheet("Entrate")
    secondo.append(["Voce", "Valore"])
    secondo.append(["Tributi", 1000])
    vuoto = libro.create_sheet("Vuoto")
    vuoto.append([None])
    uscita = io.BytesIO()
    libro.save(uscita)
    return uscita.getvalue()


def test_fogli_xlsx():
    testo = fogli_in_markdown(_xlsx(), ".xlsx")
    assert "## Spese" in testo and "## Entrate" in testo and "Vuoto" not in testo
    assert "| Manutenzione | 1.234,50 | 01/03/2026 |" in testo
    annotato = fogli_in_markdown(_xlsx(), ".xlsx", annota_formato=True)
    assert "1.234,50 [visualizzato in Excel; formato interno: #,##0.00]" in annotato


def test_fogli_ods():
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableCell, TableRow
    from odf.text import P

    documento = OpenDocumentSpreadsheet()
    tabella = Table(name="Foglio1")
    for valori in (["Voce", "Importo"], ["Tributi", "1.000,00"]):
        riga = TableRow()
        for v in valori:
            cella = TableCell()
            cella.addElement(P(text=v))
            riga.addElement(cella)
        tabella.addElement(riga)
    documento.spreadsheet.addElement(tabella)
    uscita = io.BytesIO()
    documento.save(uscita)
    assert fogli_in_markdown(uscita.getvalue(), ".ods") == "| Voce | Importo |\n|---|---|\n| Tributi | 1.000,00 |\n"


@serve_libreoffice
def test_fogli_xls(tmp_path):
    import subprocess

    (tmp_path / "f.xlsx").write_bytes(_xlsx())
    subprocess.run(["soffice", "--headless", "--convert-to", "xls", "--outdir", str(tmp_path), str(tmp_path / "f.xlsx")],
                   check=True, capture_output=True, timeout=120)  # fmt: skip
    testo = converti((tmp_path / "f.xls").read_bytes(), "f.xls")
    assert "Manutenzione" in testo and "1.234,50" in testo


def test_fogli_in_processo_separato():
    assert "Manutenzione" in converti(_xlsx(), "f.xlsx", in_processo=True)


def test_numero_italiano():
    assert numero_italiano(1234.5, "#,##0.00") == "1.234,50"
    assert numero_italiano(1234.5, "0.0") == "1234,5"
    assert numero_italiano(12.0) == "12"
    assert numero_italiano(0.25) == "0,25"


# --- altri formati ---------------------------------------------------------------------------


def test_immagini_e_testo():
    ocr = OcrFinto("Testo della foto")
    png = pdf_png()
    assert converti(png, "foto.png", ocr=ocr) == "Testo della foto\n"
    with pytest.raises(OcrNonDisponibile):
        converti(png, "foto.png")
    assert converti("città\n".encode("cp1252"), "nota.txt") == "città\n"
    assert converti(b"a;b\n1;2\n", "dati.csv") == "a;b\n1;2\n"
    with pytest.raises(FormatoNonSupportato):
        converti(b"x", "dati.xyz", verifica_ingresso=False)


def test_versione_sincrona():
    assert "Determinazione" in in_markdown_sync(odt(), "atto.odt")


def pdf_png() -> bytes:
    import pymupdf

    documento = pymupdf.open(stream=pdf([_TESTO]), filetype="pdf")
    return documento[0].get_pixmap().tobytes("png")
