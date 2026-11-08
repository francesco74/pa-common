"""Fogli di calcolo (xlsx, xls, ods) in Markdown: una tabella per foglio, con il nome del
foglio come titolo se la cartella ne ha più di uno (anche vuoti); la prima riga piena fa da
intestazione.

I numeri si scrivono come li vede l'utente, all'italiana (1.234,50), le date come gg/mm/aaaa.
Con `annota_formato=True` i numeri riportano anche come sono memorizzati, per chi deve
verificare come il dato è presentato (es. le verifiche della trasparenza):
"1.234,50 [visualizzato in Excel; formato interno: #,##0.00]" per xlsx e xls,
"1234.5 [Visualizzato come: 1.234,50]" per ods.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime, time

from ..errori import ConversioneNonRiuscita, FormatoNonSupportato
from .odt import tabella_markdown

FORMATI = {".xlsx", ".xls", ".ods"}


def numero_italiano(valore: float, formato: str = "General") -> str:
    """Valore numerico come lo vede l'utente (punto per le migliaia, virgola per i decimali).
    Il number_format di Excel è scritto sempre in sintassi en-US e non dice come appare il
    dato: va tradotto."""
    if formato == "General":
        return str(int(valore)) if float(valore).is_integer() else str(valore).replace(".", ",")
    m = re.search(r"\.(0+)", formato)
    decimali = len(m.group(1)) if m else 0
    migliaia = "," in (formato.split(".")[0] if "." in formato else formato)
    testo = f"{float(valore):,.{decimali}f}"
    if migliaia:
        return testo.replace(",", "§").replace(".", ",").replace("§", ".")
    return testo.replace(",", "").replace(".", ",")


def _data(valore: date) -> str:
    if isinstance(valore, datetime):
        return valore.strftime("%d/%m/%Y") if valore.time() == time() else valore.strftime("%d/%m/%Y %H:%M")
    return valore.strftime("%d/%m/%Y")


def _numero(valore: float, formato: str, annota: bool) -> str:
    """Con `annota`: "1.234,50 [visualizzato in Excel; formato interno: #,##0.00]", anche per
    i decimali senza formato ("General"), così si vede che la cella non è formattata."""
    if formato == "General" and float(valore).is_integer():
        return str(int(valore))
    testo = numero_italiano(valore, formato)
    return f"{testo} [visualizzato in Excel; formato interno: {formato}]" if annota else testo


def _cella(testo: str) -> str:
    return testo.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _fogli_xlsx(contenuto: bytes, annota: bool) -> list[tuple[str, list[list[str]]]]:
    import openpyxl

    libro = openpyxl.load_workbook(io.BytesIO(contenuto), data_only=True, read_only=True)
    fogli = []
    for foglio in libro.worksheets:
        righe = []
        for i, riga in enumerate(foglio.iter_rows()):
            valori = []
            for cella in riga:
                v = cella.value
                if v is None:
                    valori.append("")
                elif isinstance(v, (datetime, date)):
                    valori.append(_data(v))
                elif i == 0 or isinstance(v, bool) or not isinstance(v, (int, float)):
                    valori.append(_cella(str(v)))
                else:
                    valori.append(_numero(v, getattr(cella, "number_format", "General") or "General", annota))
            righe.append(valori)
        fogli.append((foglio.title, righe))
    libro.close()
    return fogli


def _fogli_xls(contenuto: bytes, annota: bool) -> list[tuple[str, list[list[str]]]]:
    import xlrd

    libro = xlrd.open_workbook(file_contents=contenuto, formatting_info=True)
    fogli = []
    for foglio in libro.sheets():
        righe = []
        for r in range(foglio.nrows):
            valori = []
            for c in range(foglio.ncols):
                v, tipo = foglio.cell_value(r, c), foglio.cell_type(r, c)
                if tipo == xlrd.XL_CELL_DATE and v:
                    valori.append(_data(xlrd.xldate.xldate_as_datetime(v, libro.datemode)))
                elif r == 0 or v == "" or tipo != xlrd.XL_CELL_NUMBER:
                    valori.append(_cella(str(v)))
                else:
                    fmt = libro.format_map.get(libro.xf_list[foglio.cell_xf_index(r, c)].format_key)
                    valori.append(_numero(v, fmt.format_str if fmt else "General", annota))
            righe.append(valori)
        fogli.append((foglio.name, righe))
    return fogli


def _fogli_ods(contenuto: bytes, annota: bool) -> list[tuple[str, list[list[str]]]]:
    from odf.opendocument import load
    from odf.table import Table, TableCell, TableRow
    from odf.teletype import extractText

    fogli = []
    for foglio in load(io.BytesIO(contenuto)).spreadsheet.getElementsByType(Table):
        righe = []
        for i, riga in enumerate(foglio.getElementsByType(TableRow)):
            ripeti_riga = min(int(riga.getAttribute("numberrowsrepeated") or 1), 50)
            valori = []
            for cella in riga.getElementsByType(TableCell):
                visibile = _cella(extractText(cella))
                tipo, grezzo = cella.getAttribute("valuetype"), cella.getAttribute("value")
                if annota and i and tipo in ("float", "currency", "percentage") and grezzo is not None:
                    if not (grezzo.endswith(".0") and visibile == grezzo[:-2]):
                        visibile = f"{grezzo} [Visualizzato come: {visibile}]"
                ripeti = min(int(cella.getAttribute("numbercolumnsrepeated") or 1), 50)
                valori.extend([visibile] * ripeti)
            righe.extend([valori] * (ripeti_riga if any(valori) else 1))
        fogli.append((str(foglio.getAttribute("name")), righe))
    return fogli


_LETTORI = {".xlsx": _fogli_xlsx, ".xls": _fogli_xls, ".ods": _fogli_ods}


def fogli_in_markdown(contenuto: bytes, estensione: str, *, annota_formato: bool = False) -> str:
    """Tutti i fogli con dati, in Markdown. Solleva `ConversioneNonRiuscita`."""
    lettore = _LETTORI.get(estensione)
    if lettore is None:
        raise FormatoNonSupportato(f"Formato di foglio non supportato: {estensione}")
    try:
        fogli = lettore(contenuto, annota_formato)
    except ConversioneNonRiuscita:
        raise
    except Exception as e:  # noqa: BLE001 - file danneggiato o struttura inattesa
        raise ConversioneNonRiuscita(f"Foglio di calcolo non leggibile: {e}") from e
    tabelle = [(nome, tabella_markdown(righe)) for nome, righe in fogli]
    tabelle = [(nome, t) for nome, t in tabelle if t]
    if not tabelle:
        raise ConversioneNonRiuscita("Il foglio di calcolo non contiene dati")
    if len(fogli) == 1:  # un foglio solo: il nome (di solito "Foglio1") non aggiunge nulla
        return tabelle[0][1] + "\n"
    return "\n\n".join(f"## {nome}\n\n{t}" for nome, t in tabelle) + "\n"
