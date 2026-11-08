import base64
import io
import zipfile

import pytest

from aiuti import busta_p7m, odt, pdf
from pa_common.errori import BustaNonValida, FileNonValido
from pa_common.ingresso import nome_sbustato, sbusta_p7m, tipo_reale, verifica


def _zip(nomi: dict[str, bytes]) -> bytes:
    uscita = io.BytesIO()
    with zipfile.ZipFile(uscita, "w") as zf:
        for nome, dati in nomi.items():
            zf.writestr(nome, dati)
    return uscita.getvalue()


def test_tipo_reale():
    assert tipo_reale(pdf(["ciao"])) == ".pdf"
    assert tipo_reale(b"\x89PNG\r\n\x1a\n...") == ".png"
    assert tipo_reale(b"\xff\xd8\xff\xe0...") == ".jpg"
    assert tipo_reale(b"II*\x00...") == ".tif"
    assert tipo_reale(b"{\\rtf1\\ansi ...}") == ".rtf"
    assert tipo_reale(b"\xef\xbb\xbf  <!DOCTYPE html><html>") == ".html"
    assert tipo_reale(odt()) == ".odt"
    assert tipo_reale(_zip({"word/document.xml": b"<w/>"})) == ".docx"
    assert tipo_reale(_zip({"xl/workbook.xml": b"<x/>"})) == ".xlsx"
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
    assert tipo_reale(ole + "WordDocument".encode("utf-16-le")) == ".doc"
    assert tipo_reale(ole + "Workbook".encode("utf-16-le")) == ".xls"
    assert tipo_reale(b"testo semplice") is None
    assert tipo_reale(_zip({"altro.txt": b"x"})) is None


def test_sbustamento_p7m():
    documento = pdf(["Determina firmata"])
    busta = busta_p7m(documento)
    assert sbusta_p7m(busta) == documento
    assert sbusta_p7m(base64.b64encode(busta)) == documento  # base64, frequente in Italia
    pem = b"-----BEGIN PKCS7-----\n" + base64.encodebytes(busta) + b"-----END PKCS7-----\n"
    assert sbusta_p7m(pem) == documento
    assert sbusta_p7m(documento) is None  # non è una busta
    assert sbusta_p7m(b"\x30\x03\x02\x01\x00") is None  # ASN.1 ma non signed-data
    with pytest.raises(BustaNonValida):
        sbusta_p7m(busta_p7m(b""))  # firma separata: nessun documento dentro


def test_nome_sbustato():
    assert nome_sbustato("fattura.pdf.p7m", ".pdf") == "fattura.pdf"
    assert nome_sbustato("fattura.p7m", ".pdf") == "fattura.pdf"
    assert nome_sbustato("FATTURA.PDF.P7M", ".pdf") == "FATTURA.PDF"


def test_verifica():
    documento = pdf(["Determina"])
    f = verifica(busta_p7m(busta_p7m(documento)), "atto.pdf.p7m.p7m", ammesse={".pdf"})
    assert (f.contenuto, f.nome, f.estensione, f.sbustato) == (documento, "atto.pdf", ".pdf", True)
    # busta non dichiarata (".pdf" che è ancora firmato): sbustata comunque
    assert verifica(busta_p7m(documento), "atto.pdf").contenuto == documento
    assert verifica(documento, "atto.PDF").estensione == ".pdf"
    assert verifica(b"\xff\xd8\xff\xe0x", "foto.jpeg").estensione == ".jpg"
    assert verifica("città;importo\n".encode("cp1252"), "dati.csv").estensione == ".csv"

    with pytest.raises(FileNonValido, match="non corrisponde"):
        verifica(documento, "atto.docx")
    with pytest.raises(FileNonValido, match="non ammesso"):
        verifica(documento, "atto.pdf", ammesse={".odt"})
    with pytest.raises(FileNonValido, match="non è un file di testo"):
        verifica(documento, "dati.txt")
    with pytest.raises(FileNonValido, match="busta"):
        verifica(documento, "atto.p7m")
    with pytest.raises(FileNonValido, match="vuoto"):
        verifica(b"", "atto.pdf")
    with pytest.raises(FileNonValido, match="non riconosciuto"):
        verifica(b"qualcosa", "atto.pdf")
    # doc e xls condividono il contenitore OLE: vale l'estensione dichiarata
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + "WordDocument".encode("utf-16-le")
    assert verifica(ole, "vecchio.xls").estensione == ".xls"
