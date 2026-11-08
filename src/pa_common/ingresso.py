"""Controlli sui file in ingresso: tipo reale dal contenuto e buste di firma .p7m.

L'estensione del nome non basta: un ".pdf" può essere una busta di firma non sbustata, un
".docx" un file rinominato. `verifica` sbusta le firme (anche annidate o in base64), riconosce
il tipo reale e lo confronta con quello dichiarato.

    f = verifica(contenuto, "determina.pdf.p7m", ammesse={".pdf", ".odt"})
    f.contenuto, f.nome, f.estensione   # il PDF, "determina.pdf", ".pdf"
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import PurePath

from .errori import BustaNonValida, FileNonValido

log = logging.getLogger("pa_common.ingresso")

# Formati riconosciuti dal contenuto (firma iniziale o struttura dell'archivio)
DOCUMENTI = {".pdf", ".odt", ".docx", ".doc", ".rtf", ".html"}
FOGLI = {".ods", ".xlsx", ".xls"}
IMMAGINI = {".png", ".jpg", ".tif"}
# Formati di solo testo: senza firma, si controlla che siano testo davvero
TESTI = {".txt", ".md", ".csv"}
BUSTE = {".p7m"}
RICONOSCIUTI = DOCUMENTI | FOGLI | IMMAGINI | TESTI

# Nomi diversi per lo stesso formato
_SINONIMI = {".jpeg": ".jpg", ".tiff": ".tif", ".htm": ".html"}

_FIRME = (
    (b"%PDF-", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
    (b"{\\rtf", ".rtf"),
)
_ODF = {
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
}
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # contenitore dei vecchi formati Office (doc, xls)
_MAX_BUSTE = 3  # buste annidate (es. ".pdf.p7m.p7m", firme successive)


@dataclass(frozen=True)
class FileInIngresso:
    contenuto: bytes
    nome: str
    estensione: str  # reale, minuscola, con il punto (".pdf")
    sbustato: bool = False  # era dentro una busta di firma


def estensione(nome: str) -> str:
    """Estensione minuscola con il punto, con i sinonimi unificati (".JPEG" -> ".jpg")."""
    est = PurePath(nome or "").suffix.lower()
    return _SINONIMI.get(est, est)


def _tipo_zip(contenuto: bytes) -> str | None:
    """docx, xlsx, odt e ods sono archivi zip: il tipo si riconosce da cosa contengono."""
    try:
        with zipfile.ZipFile(io.BytesIO(contenuto)) as archivio:
            nomi = set(archivio.namelist())
            if "mimetype" in nomi:
                return _ODF.get(archivio.read("mimetype").decode("ascii", "ignore").strip())
            if "word/document.xml" in nomi:
                return ".docx"
            if "xl/workbook.xml" in nomi:
                return ".xlsx"
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    return None


def _tipo_ole(contenuto: bytes) -> str | None:
    """doc e xls: i nomi dei flussi del contenitore OLE sono scritti in UTF-16."""
    if "WordDocument".encode("utf-16-le") in contenuto:
        return ".doc"
    if "Workbook".encode("utf-16-le") in contenuto or "Book".encode("utf-16-le") in contenuto:
        return ".xls"
    return None


def tipo_reale(contenuto: bytes) -> str | None:
    """Estensione del formato riconosciuto dal contenuto, None se non è uno dei formati con
    una firma riconoscibile (i formati di testo non ne hanno)."""
    for firma, est in _FIRME:
        if contenuto.startswith(firma):
            return est
    if contenuto.startswith(b"PK\x03\x04"):
        return _tipo_zip(contenuto)
    if contenuto.startswith(_OLE):
        return _tipo_ole(contenuto)
    inizio = contenuto[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if inizio.startswith((b"<!doctype html", b"<html")):
        return ".html"
    return None


def _der(contenuto: bytes) -> bytes | None:
    """Il contenuto come DER: le buste si trovano anche codificate in base64 (PEM o no)."""
    if contenuto[:1] == b"\x30":
        return contenuto
    testo = contenuto.strip()
    if testo.startswith(b"-----BEGIN"):
        testo = b"".join(r for r in testo.splitlines() if not r.startswith(b"-----"))
    if not testo.startswith(b"MI"):  # "MI" = 0x30 0x8x in base64
        return None
    try:
        return base64.b64decode(testo, validate=False)
    except (binascii.Error, ValueError):
        return None


def sbusta_p7m(contenuto: bytes) -> bytes | None:
    """Documento contenuto in una busta di firma PKCS#7/CAdES (signed-data), None se il
    contenuto non è una busta. Solleva `BustaNonValida` se è una busta ma il documento non si
    estrae (es. firma "detached", senza il documento dentro)."""
    der = _der(contenuto)
    if der is None:
        return None
    from asn1crypto.cms import ContentInfo

    try:
        info = ContentInfo.load(der)
        if info["content_type"].native != "signed_data":
            return None
    except Exception:  # noqa: BLE001 - qualunque errore di lettura ASN.1: non è una busta
        return None
    try:
        incapsulato = info["content"]["encap_content_info"]["content"]
        grezzo = incapsulato.native
        documento = grezzo if isinstance(grezzo, bytes) else incapsulato.chosen.contents
    except Exception as e:  # noqa: BLE001
        raise BustaNonValida(f"Busta di firma non leggibile: {e}") from e
    if not documento:
        raise BustaNonValida("La busta di firma non contiene il documento (firma separata)")
    return documento


def _senza_p7m(nome: str) -> str:
    return nome[: -len(".p7m")] if nome.lower().endswith(".p7m") else nome


def nome_sbustato(nome: str, est: str | None) -> str:
    """"fattura.pdf.p7m" -> "fattura.pdf"; "fattura.p7m" -> "fattura.pdf" (con est=".pdf")."""
    base = _senza_p7m(nome)
    return base if not est or estensione(base) == est else f"{base}{est}"


def _e_testo(contenuto: bytes) -> bool:
    if b"\x00" in contenuto[:65536]:
        return False
    try:
        contenuto.decode("utf-8")
    except UnicodeDecodeError:
        try:  # CSV e testi esportati da Windows
            contenuto.decode("cp1252")
        except UnicodeDecodeError:
            return False
    return True


def verifica(contenuto: bytes, nome: str, ammesse: set[str] | None = None) -> FileInIngresso:
    """Sbusta le firme, riconosce il tipo reale e lo confronta con l'estensione del nome.

    `ammesse`: estensioni accettate dal chiamante (es. {".pdf", ".odt"}); None = tutte quelle
    riconosciute. Solleva `FileNonValido` (o `BustaNonValida`) se il file non va accettato.
    """
    if not contenuto:
        raise FileNonValido(f"{nome}: il file è vuoto")
    sbustato = False
    for _ in range(_MAX_BUSTE):
        interno = sbusta_p7m(contenuto)
        if interno is None:
            break
        if estensione(nome) not in BUSTE:
            log.warning("%s: contenuto in una busta di firma non dichiarata, sbustato", nome)
        contenuto, sbustato = interno, True
        nome = nome_sbustato(nome, tipo_reale(contenuto))
    else:
        raise FileNonValido(f"{nome}: troppe buste di firma annidate")

    dichiarata = estensione(nome)
    if dichiarata in BUSTE:
        raise FileNonValido(f"{nome}: dichiarato come busta di firma, ma non lo è")
    reale = tipo_reale(contenuto)
    if dichiarata in TESTI:
        if reale is not None or not _e_testo(contenuto):
            raise FileNonValido(f"{nome}: il contenuto non è un file di testo")
        reale = dichiarata
    elif reale is None:
        raise FileNonValido(f"{nome}: formato non riconosciuto")
    elif reale != dichiarata:
        # .doc e .xls hanno lo stesso contenitore: si tiene il dichiarato se è uno dei due
        if not ({reale, dichiarata} <= {".doc", ".xls"}):
            raise FileNonValido(
                f"{nome}: il contenuto ({reale[1:].upper()}) non corrisponde all'estensione"
            )
        reale = dichiarata
    if ammesse is not None and reale not in {_SINONIMI.get(e, e) for e in ammesse}:
        raise FileNonValido(f"{nome}: formato {reale[1:].upper()} non ammesso")
    return FileInIngresso(contenuto=contenuto, nome=nome, estensione=reale, sbustato=sbustato)
