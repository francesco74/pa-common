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
from pa_common.errori import (
    ConversioneNonRiuscita, DocumentoScansionato, FormatoNonSupportato, OcrNonDisponibile, TroppePagine,
)

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


def test_pdf_blocca_scansioni():
    # pagina 2 scansionata (immagine senza testo), 4 scansionata con OCR incorporato
    # (ricercabile), 3 con la sola firma, 5 bianca, 6 con un disegno vettoriale
    pagine = [_corpo(1), "", "Il Dirigente", _corpo(4), "", "", _corpo(7)]
    contenuto = pdf(pagine, immagini={1, 3}, scansionate={5})
    with pytest.raises(DocumentoScansionato) as e:
        converti(contenuto, "atto.pdf", blocca_scansioni=True)
    assert (e.value.pagine, e.value.totale) == ([2], 7)
    assert isinstance(e.value, ConversioneNonRiuscita)

    # senza la scansione: le pagine con poco testo danno il loro testo, niente OCR
    contenuto = pdf([_corpo(1), "Il Dirigente", _corpo(3), "", ""], immagini={2}, scansionate={4})
    statistiche = Statistiche()
    testo = converti(contenuto, "atto.pdf", blocca_scansioni=True, statistiche=statistiche)
    assert "Il Dirigente" in testo and "punto h della pagina 3" in testo
    assert statistiche.pagine_ocr == 0
    with pytest.raises(OcrNonDisponibile):  # senza l'opzione le stesse pagine vanno all'OCR
        converti(contenuto, "atto.pdf")


def test_pdf_carta_intestata_non_e_una_scansione():
    # stessa immagine di sfondo su ogni pagina: l'ultima, con la sola firma, non è una scansione
    contenuto = pdf([_corpo(1), _corpo(2), "Il Dirigente"], sfondo=True)
    assert "Il Dirigente" in converti(contenuto, "atto.pdf", blocca_scansioni=True)


def test_immagine_con_blocca_scansioni():
    import pymupdf

    png = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10), False).tobytes("png")
    with pytest.raises(DocumentoScansionato):
        converti(png, "foto.png", blocca_scansioni=True, ocr=OcrFinto())


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


def test_fogli_xlsx_nascosti():
    import openpyxl

    libro = openpyxl.Workbook()
    libro.active.title = "Pagamenti"
    libro.active.append(["Beneficiario", "Importo"])
    libro.active.append(["Ditta Rossi", 10])
    for nome, stato in (("Appoggio", "hidden"), ("Codici", "veryHidden")):
        foglio = libro.create_sheet(nome)
        foglio.append(["Codice"])
        foglio.append(["segreto"])
        foglio.sheet_state = stato
    uscita = io.BytesIO()
    libro.save(uscita)
    testo = fogli_in_markdown(uscita.getvalue(), ".xlsx")
    # un solo foglio visibile: senza titolo, con la nota sui nascosti
    assert testo.startswith("| Beneficiario | Importo |")
    assert "segreto" not in testo
    assert testo.endswith("Fogli nascosti non letti: Appoggio, Codici\n")


def _ods(*fogli: tuple[str, list[list[tuple[str, str | None]]], bool]) -> bytes:
    """ODS con i fogli (nome, righe di celle (testo, valore numerico o None), nascosto)."""
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.style import Style, TableProperties
    from odf.table import Table, TableCell, TableRow
    from odf.text import P

    documento = OpenDocumentSpreadsheet()
    nascosto = Style(name="ta2", family="table")
    nascosto.addElement(TableProperties(display="false"))
    documento.automaticstyles.addElement(nascosto)
    for nome, righe, invisibile in fogli:
        tabella = Table(name=nome, stylename=nascosto if invisibile else None)
        for valori in righe:
            riga = TableRow()
            for testo, valore in valori:
                cella = TableCell(valuetype="float", value=valore) if valore is not None else TableCell()
                cella.addElement(P(text=testo))
                riga.addElement(cella)
            tabella.addElement(riga)
        documento.spreadsheet.addElement(tabella)
    uscita = io.BytesIO()
    documento.save(uscita)
    return uscita.getvalue()


def test_fogli_ods_nascosti_e_annotazioni():
    contenuto = _ods(
        ("Report", [[("Anno", None), ("Importo", None)], [("2026", "2026"), ("650,50", "650.5")], [("2026", "2026"), ("7", "7.0")]], False),
        ("'file://server/dati.xls'#Soggetti", [[("Codice", None)], [("", "320533")]], True),
    )
    testo = fogli_in_markdown(contenuto, ".ods", annota_formato=True)
    # annotati solo i numeri visualizzati diversamente da come sono memorizzati
    assert "| 2026 | 650.5 [Visualizzato come: 650,50] |" in testo
    assert "| 2026 | 7 |" in testo
    assert "320533" not in testo
    assert testo.endswith("Fogli nascosti non letti: Soggetti (collegato a un file esterno)\n")


def test_fogli_solo_nascosti():
    contenuto = _ods(("Visibile", [], False), ("Dati", [[("x", None)]], True))
    with pytest.raises(ConversioneNonRiuscita, match="non contiene dati visibili"):
        fogli_in_markdown(contenuto, ".ods")


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
