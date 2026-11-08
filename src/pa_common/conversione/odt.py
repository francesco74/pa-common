"""ODT in Markdown, leggendo direttamente l'XML del documento.

Titoli (anche dagli stili derivati, come nei documenti convertiti da Word), paragrafi,
grassetto e corsivo, a capo forzati, elenchi numerati e puntati, tabelle (anche con celle
unite o ripetute) e note a piè di pagina. È il passaggio finale anche per DOC, DOCX e RTF,
che LibreOffice converte prima in ODT: nel confronto con pandoc conserva le tabelle con
celle unite, che pandoc sostituisce con "[TABLE]", e non riempie il testo di caratteri di
escape.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from ..errori import ConversioneNonRiuscita

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
}
_T = "{%s}" % NS["text"]
_TB = "{%s}" % NS["table"]
_FO = "{%s}" % NS["fo"]
_ST = "{%s}" % NS["style"]

_SPAZI = re.compile(r"[ \t]+")
# stili di paragrafo dei titoli (nomi interni ODT, anche dei documenti convertiti da Word)
_STILE_TITOLO = re.compile(r"^(?:Heading|Title|Titolo|Intestazione)(?:_20_(\d))?$", re.IGNORECASE)
_RIGHE_VUOTE = re.compile(r"\n{3,}")
_A_CAPO = " "  # a capo forzato dentro un paragrafo, finché non si sistemano gli spazi
_MAX_RIPETIZIONI = 50  # celle ripetute di riempimento (fino alla fine del foglio)


def _formattazione(radici: list[ET.Element]) -> tuple[set[str], set[str]]:
    """Stili che rendono il testo in grassetto e in corsivo."""
    grassetto, corsivo = set(), set()
    for radice in radici:
        for stile in radice.iterfind(".//style:style", NS):
            prop = stile.find("style:text-properties", NS)
            if prop is None:
                continue
            nome = stile.get(_ST + "name")
            if prop.get(_FO + "font-weight") in ("bold", "700", "800", "900"):
                grassetto.add(nome)
            if prop.get(_FO + "font-style") in ("italic", "oblique"):
                corsivo.add(nome)
    return grassetto, corsivo


def _genitori_stili(radici: list[ET.Element]) -> dict[str, str]:
    """Stile -> stile da cui deriva (es. P3 -> Heading_20_1)."""
    genitori = {}
    for radice in radici:
        for stile in radice.iterfind(".//style:style", NS):
            nome = stile.get(_ST + "name")
            genitore = stile.get(_ST + "parent-style-name")
            if nome and genitore:
                genitori[nome] = genitore
    return genitori


def _elenchi_numerati(radici: list[ET.Element]) -> set[str]:
    """Stili di elenco il cui primo livello è numerato (1., a., i.) e non puntato."""
    numerati = set()
    for radice in radici:
        for stile in radice.iterfind(".//text:list-style", NS):
            primo = next(iter(stile), None)
            if primo is not None and primo.tag == _T + "list-level-style-number":
                numerati.add(stile.get(_ST + "name"))
    return numerati


def _racchiudi(testo: str, segno: str) -> str:
    """`segno` attorno al testo, lasciando fuori gli spazi iniziali e finali."""
    if not testo.strip():
        return testo
    prima = " " if testo[:1].isspace() else ""
    dopo = " " if testo[-1:].isspace() else ""
    return f"{prima}{segno}{testo.strip()}{segno}{dopo}"


class _Odt:
    def __init__(self, radice: ET.Element, stili: ET.Element | None = None):
        radici = [radice] + ([stili] if stili is not None else [])
        self.grassetto, self.corsivo = _formattazione(radici)
        self.genitori = _genitori_stili(radici)
        self.numerati = _elenchi_numerati(radici)
        self.note: list[str] = []

    def _eredita(self, nome: str | None, insieme: set[str]) -> bool:
        for _ in range(5):  # catena di derivazione degli stili
            if not nome:
                return False
            if nome in insieme:
                return True
            nome = self.genitori.get(nome)
        return False

    def segni(self, nome: str | None) -> str:
        """Segni Markdown della formattazione di uno stile: "**", "*", "***" o ""."""
        g = self._eredita(nome, self.grassetto)
        c = self._eredita(nome, self.corsivo)
        return "***" if g and c else "**" if g else "*" if c else ""

    def livello_titolo(self, el: ET.Element) -> int | None:
        """Livello di titolo di un paragrafo con stile "Titolo/Heading", altrimenti None."""
        nome = el.get(_T + "style-name")
        for _ in range(5):
            if not nome:
                return None
            m = _STILE_TITOLO.match(nome)
            if m:
                return int(m.group(1) or 1)
            nome = self.genitori.get(nome)
        return None

    def inline(self, el: ET.Element) -> str:
        """Testo di un paragrafo con la formattazione in linea."""
        parti = [el.text or ""]
        for figlio in el:
            tag = figlio.tag
            if tag == _T + "s":
                parti.append(" " * int(figlio.get(_T + "c", "1")))
            elif tag == _T + "tab":
                parti.append(" ")
            elif tag == _T + "line-break":
                parti.append(_A_CAPO)
            elif tag == _T + "note":
                corpo = figlio.find("text:note-body", NS)
                testo = " ".join(self.inline(p) for p in corpo) if corpo is not None else ""
                self.note.append(_SPAZI.sub(" ", testo.replace(_A_CAPO, " ")).strip())
                parti.append(f"[^{len(self.note)}]")
            elif tag in (_T + "span", _T + "a"):
                interno = self.inline(figlio)
                segno = self.segni(figlio.get(_T + "style-name"))
                parti.append(_racchiudi(interno, segno) if segno else interno)
            elif tag in (_T + "bookmark", _T + "bookmark-start", _T + "bookmark-end"):
                pass
            else:
                parti.append(self.inline(figlio))
            parti.append(figlio.tail or "")
        return "".join(parti)

    def paragrafo(self, el: ET.Element) -> str:
        righe = [_SPAZI.sub(" ", r).strip() for r in self.inline(el).split(_A_CAPO)]
        testo = "  \n".join(r for r in righe if r)
        segno = self.segni(el.get(_T + "style-name"))
        if testo and segno and not testo.startswith("*"):
            testo = "  \n".join(_racchiudi(r, segno) for r in testo.split("  \n"))
        return testo

    def blocchi(self, contenitore: ET.Element, livello_elenco: int = 0) -> list[str]:
        """Blocchi Markdown (separati da riga vuota) degli elementi del contenitore."""
        risultato: list[str] = []
        for el in contenitore:
            tag = el.tag
            if tag == _T + "h":
                testo = self.paragrafo(el).replace("  \n", " ").strip("* ")
                if testo:
                    livello = min(int(el.get(_T + "outline-level", "1")), 6)
                    risultato.append(f"{'#' * livello} {testo}")
            elif tag == _T + "p":
                testo = self.paragrafo(el)
                livello = self.livello_titolo(el)
                if testo and livello:
                    titolo = testo.replace("  \n", " ").strip("* ")
                    risultato.append(f"{'#' * min(livello, 6)} {titolo}")
                elif testo:
                    risultato.append(testo)
            elif tag == _T + "list":
                righe = self.elenco(el, livello_elenco)
                if righe:
                    risultato.append("\n".join(righe))
            elif tag == _TB + "table":
                risultato.append(self.tabella(el))
            elif tag in (_T + "section", _T + "index-body", _T + "table-of-content"):
                risultato.extend(self.blocchi(el, livello_elenco))
        return risultato

    def elenco(self, el: ET.Element, livello: int, stile: str | None = None) -> list[str]:
        righe = []
        rientro = "   " * livello
        stile = el.get(_T + "style-name") or stile
        numerato = stile in self.numerati
        numero = 0
        for voce in el.findall("text:list-item", NS) + el.findall("text:list-header", NS):
            primo = True
            for figlio in voce:
                if figlio.tag == _T + "list":
                    righe.extend(self.elenco(figlio, livello + 1, stile))
                elif figlio.tag in (_T + "p", _T + "h"):
                    testo = self.paragrafo(figlio).replace("  \n", " ")
                    if not testo:
                        continue
                    if primo:
                        numero += 1
                        segno = f"{numero}. " if numerato else "- "
                    else:
                        segno = "   "
                    righe.append(f"{rientro}{segno}{testo}")
                    primo = False
        return righe

    def tabella(self, el: ET.Element) -> str:
        righe: list[list[str]] = []
        for riga in el.iter(_TB + "table-row"):
            celle = []
            for cella in riga:
                if cella.tag not in (_TB + "table-cell", _TB + "covered-table-cell"):
                    continue
                testo = " ".join(self.blocchi(cella)).replace("  \n", " ").replace("\n", " ").replace("|", "\\|")
                ripeti = int(cella.get(_TB + "number-columns-repeated", "1"))
                celle.extend([_SPAZI.sub(" ", testo).strip()] * min(ripeti, _MAX_RIPETIZIONI))
            righe.append(celle)
        return tabella_markdown(righe)


def tabella_markdown(righe: list[list[str]]) -> str:
    """Tabella Markdown (la prima riga piena fa da intestazione), senza righe vuote e senza
    colonne finali vuote in tutte le righe (celle di riempimento)."""
    righe = [r for r in righe if any(c.strip() for c in r)]
    if not righe:
        return ""
    colonne = max(len(r) for r in righe)
    righe = [r + [""] * (colonne - len(r)) for r in righe]
    while colonne > 1 and all(not r[colonne - 1] for r in righe):
        colonne -= 1
        righe = [r[:colonne] for r in righe]
    linee = ["| " + " | ".join(righe[0]) + " |", "|" + "---|" * colonne]
    linee += ["| " + " | ".join(r) + " |" for r in righe[1:]]
    return "\n".join(linee)


def odt_in_markdown(contenuto: bytes) -> str:
    """Testo Markdown di un documento ODT. Solleva `ConversioneNonRiuscita`."""
    try:
        with zipfile.ZipFile(io.BytesIO(contenuto)) as zf:
            radice = ET.fromstring(zf.read("content.xml"))
            stili = ET.fromstring(zf.read("styles.xml")) if "styles.xml" in zf.namelist() else None
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise ConversioneNonRiuscita("Documento ODT non valido") from e
    corpo = radice.find(".//office:body/office:text", NS)
    if corpo is None:
        raise ConversioneNonRiuscita("Documento ODT senza testo")
    odt = _Odt(radice, stili)
    testo = "\n\n".join(b for b in odt.blocchi(corpo) if b)
    if odt.note:
        testo += "\n\n" + "\n".join(f"[^{i}]: {n}" for i, n in enumerate(odt.note, 1))
    return _RIGHE_VUOTE.sub("\n\n", testo).strip() + "\n"
