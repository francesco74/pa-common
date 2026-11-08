"""Documenti di prova costruiti al momento: niente file esterni nel repository."""

from __future__ import annotations

import io
import zipfile

from asn1crypto import cms


def busta_p7m(documento: bytes) -> bytes:
    """Busta CAdES (signed-data) minima con il documento incapsulato, senza firmatari."""
    firmato = cms.SignedData(
        {
            "version": "v1",
            "digest_algorithms": [],
            "encap_content_info": {"content_type": "data", "content": documento},
            "signer_infos": [],
        }
    )
    return cms.ContentInfo({"content_type": "signed_data", "content": firmato}).dump()


_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
  xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
  xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
  xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
  xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" office:version="1.3">
 <office:automatic-styles>
  <style:style style:name="T1" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>
  <style:style style:name="T2" style:family="text"><style:text-properties fo:font-style="italic"/></style:style>
  <style:style style:name="P1" style:family="paragraph" style:parent-style-name="Heading_20_2"/>
 </office:automatic-styles>
 <office:body><office:text>
  <text:h text:outline-level="1">Determinazione</text:h>
  <text:p text:style-name="P1">Premesse</text:p>
  <text:p>Visto il <text:span text:style-name="T1">d.lgs. 33/2013</text:span> e la <text:span text:style-name="T2">legge 190</text:span><text:note><text:note-body><text:p>Nota di prova.</text:p></text:note-body></text:note>;</text:p>
  <text:p>a) prima riga;<text:line-break/>b) seconda riga.</text:p>
  <text:list>
   <text:list-item><text:p>voce uno</text:p></text:list-item>
   <text:list-item><text:p>voce due</text:p></text:list-item>
  </text:list>
  <table:table>
   <table:table-row>
    <table:table-cell><text:p>Capitolo</text:p></table:table-cell>
    <table:table-cell><text:p>Importo</text:p></table:table-cell>
    <table:table-cell table:number-columns-repeated="3"/>
   </table:table-row>
   <table:table-row>
    <table:table-cell><text:p>1010|A</text:p></table:table-cell>
    <table:table-cell><text:p>1.234,50</text:p></table:table-cell>
   </table:table-row>
  </table:table>
 </office:text></office:body>
</office:document-content>"""


def odt(contenuto_xml: str = _CONTENT) -> bytes:
    """ODT minimo con titoli, grassetto, corsivo, nota, a capo forzato, elenco e tabella."""
    uscita = io.BytesIO()
    with zipfile.ZipFile(uscita, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        zf.writestr("content.xml", contenuto_xml)
    return uscita.getvalue()


def pdf(pagine: list[str], intestazione: str | None = None, scansionate: set[int] = frozenset()) -> bytes:
    """PDF con una pagina per testo; `intestazione` ripetuta in cima a ogni pagina; le pagine
    in `scansionate` sono solo un rettangolo disegnato (nessun testo: vanno all'OCR)."""
    import pymupdf

    documento = pymupdf.open()
    for i, testo in enumerate(pagine):
        pagina = documento.new_page()
        if i in scansionate:
            pagina.draw_rect(pymupdf.Rect(50, 50, 500, 700), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
            continue
        y = 50
        if intestazione:
            pagina.insert_text((50, y), intestazione, fontsize=9)
            y += 40
        for riga in testo.split("\n"):
            pagina.insert_text((50, y), riga, fontsize=11)
            y += 16
        pagina.insert_text((280, 800), f"Pagina {i + 1}", fontsize=9)
    dati = documento.tobytes()
    documento.close()
    return dati


class OcrFinto:
    """Al posto di `pa_common.ocr.Ocr`: restituisce un testo fisso e conta le chiamate."""

    def __init__(self, testo: str = "Testo trascritto dalla pagina scansionata, abbastanza lungo da contare."):
        self.testo = testo
        self.chiamate = 0

    async def trascrivi(self, immagine: bytes, *, markdown: bool = True) -> str:
        assert immagine.startswith(b"\x89PNG")
        self.chiamate += 1
        return self.testo
