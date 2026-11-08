"""Pulizia del testo e del Markdown prodotti dalla conversione e dall'OCR."""

from __future__ import annotations

import re

_SPAZI_HTML = re.compile(r"(?:&nbsp;|&#160;|\xa0|\s)+")


def normalizza_spazi(testo: str) -> str:
    """Entità HTML di spaziatura e spazi ripetuti diventano un solo spazio."""
    if not testo:
        return testo
    return _SPAZI_HTML.sub(" ", testo).strip()


# Spazio non separabile scritto dall'OCR come entità HTML o come carattere
_NBSP = r"(?:&nbsp;|&#160;|&#xa0;|\xa0)"
_NBSP_RE = re.compile(_NBSP, re.IGNORECASE)
_RIENTRO_RE = re.compile(rf"^(?:[ \t]|{_NBSP})*", re.IGNORECASE)
# Evidenziato e sottolineato: li aggiunge l'estrazione del PDF, il Markdown non li ha
_MARK_RE = re.compile(r"</?(?:mark|u)\s*>", re.IGNORECASE)
_COLONNE_RE = re.compile(r" {3,}")  # colonne allineate a spazi
_DOPPI_RE = re.compile(r" {2,}")
_RIGHE_VUOTE_RE = re.compile(r"\n{3,}")


def pulisci_ocr(testo: str) -> str:
    """Toglie gli spazi usati per imitare l'impaginazione, senza perdere la struttura Markdown
    né le colonne di dati. Riga per riga:

    - rientro iniziale fatto con &nbsp; (impaginazione): tolto; un rientro di soli spazi
      normali resta, perché in Markdown annida elenchi e codice;
    - 3 o più spazi dentro la riga (colonne allineate): una tabulazione, che l'LLM legge come
      "colonna successiva";
    - 2 spazi dentro la riga: uno; nelle righe di tabella (| ... |) gli spazi multipli sono
      solo imbottitura delle celle: uno;
    - spazi a fine riga tolti, più righe vuote di fila ridotte a una;
    - tag <mark> e <u> tolti, il testo che racchiudono resta.

    I blocchi di codice (```) restano come sono.
    """
    if not testo:
        return testo
    testo = _MARK_RE.sub("", testo)
    righe = []
    in_codice = False
    for riga in testo.split("\n"):
        if riga.lstrip().startswith("```"):
            in_codice = not in_codice
            righe.append(riga.rstrip())
            continue
        if in_codice:
            righe.append(riga)
            continue
        rientro = _RIENTRO_RE.match(riga).group(0)
        corpo = riga[len(rientro) :]
        rientro = "" if _NBSP_RE.search(rientro) else rientro.replace("\t", "    ")
        corpo = _NBSP_RE.sub(" ", corpo).replace("\t", "   ").rstrip()
        if corpo.startswith("|"):
            corpo = _DOPPI_RE.sub(" ", corpo)
        else:
            corpo = _DOPPI_RE.sub(" ", _COLONNE_RE.sub("\t", corpo))
        righe.append(rientro + corpo)
    return _RIGHE_VUOTE_RE.sub("\n\n", "\n".join(righe)).strip("\n")


_REGOLE_MD = (
    (re.compile(r"<!--.*?-->", re.DOTALL), ""),
    (re.compile(r"^\s*```[^\n]*$", re.MULTILINE), ""),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),  # immagini
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # collegamenti
    (re.compile(r"</?[a-zA-Z][^>\n]*>"), ""),  # tag HTML
    (re.compile(r"^[ \t]*\|?[ \t:|-]*-[ \t:|-]*\|?[ \t]*$", re.MULTILINE), ""),  # separatori di tabella
    (re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE), ""),  # titoli
    (re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE), ""),  # righe orizzontali
    (re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE), ""),  # citazioni
    (re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1"), r"\2"),  # grassetto
    (re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])"), r"\1"),  # corsivo
    (re.compile(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])"), r"\1"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"`([^`\n]+)`"), r"\1"),
)


def markdown_in_testo(markdown: str) -> str:
    """Testo semplice dal Markdown: via la formattazione, le righe di tabella diventano
    colonne separate da tabulazioni."""
    testo = pulisci_ocr(markdown or "")
    for regola, sostituzione in _REGOLE_MD:
        testo = regola.sub(sostituzione, testo)
    righe = []
    for riga in testo.split("\n"):
        pulita = riga.strip()
        if pulita.startswith("|") and pulita.endswith("|") and len(pulita) > 1:
            riga = "\t".join(cella.strip() for cella in pulita[1:-1].split("|"))
        righe.append(riga.rstrip())
    return _RIGHE_VUOTE_RE.sub("\n\n", "\n".join(righe)).strip()


_PENSIERO_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_RECINTO_RE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


def pulisci_trascrizione(testo: str | None) -> str:
    """Toglie ciò che i modelli aggiungono attorno a una trascrizione (ragionamento <think>,
    blocco di codice che racchiude tutto) e le sillabazioni a fine riga."""
    testo = _PENSIERO_RE.sub("", testo or "").strip()
    if m := _RECINTO_RE.match(testo):
        testo = m[1].strip()
    return testo.replace("-\n", "")
