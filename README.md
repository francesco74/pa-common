# pa-common

Codice comune ai progetti per la pubblica amministrazione [atti-pa](https://github.com/francesco74/atti-pa)
e [rag](https://github.com/francesco74/rag): una libreria Python (≥ 3.11) che ogni progetto
installa con una versione fissata.

| Modulo | Cosa fa |
|---|---|
| `pa_common.ingresso` | tipo reale di un file dal contenuto; buste di firma `.p7m` (anche annidate o in base64); controllo dell'estensione dichiarata |
| `pa_common.conversione` | documenti in Markdown: PDF (PyMuPDF4LLM, OCR delle pagine scansionate, intestazioni ripetute tolte), ODT, DOC/DOCX/RTF/HTML (LibreOffice), XLSX/XLS/ODS, immagini, testo |
| `pa_common.ocr` | trascrizione di immagini con un modello multimodale: Gemini, OpenAI, Anthropic, Mistral, vLLM, Ollama; limite di richieste e nuovi tentativi |
| `pa_common.testo` | pulizia del testo dell'OCR, Markdown in testo semplice |
| `pa_common.processo` | esecuzione in un processo separato con un tempo massimo che lo termina davvero |

La libreria non legge variabili d'ambiente: ogni progetto passa la propria configurazione.

## Installazione

Nel `requirements.txt` del progetto, con la versione (tag) e gli extra che servono:

```
pa-common[pdf,fogli,ocr-gemini] @ git+https://github.com/francesco74/pa-common@v0.1.0
```

| Extra | Per |
|---|---|
| `pdf` | PDF (PyMuPDF, PyMuPDF4LLM) |
| `fogli` | fogli di calcolo (openpyxl, xlrd, odfpy) |
| `ocr-gemini`, `ocr-openai` (anche vLLM), `ocr-anthropic`, `ocr-mistral`, `ocr-ollama` | OCR con quel fornitore |
| `tutto` | tutto quanto sopra |

DOC, DOCX, RTF e HTML richiedono LibreOffice (`soffice`) nell'immagine, ad esempio
`apt-get install libreoffice-writer-nogui fonts-liberation fonts-dejavu-core`.

## Uso

```python
from pa_common.conversione import in_markdown
from pa_common.ocr import crea_ocr

ocr = crea_ocr("gemini", "gemini-3-flash-preview", api_key="...")
testo = await in_markdown(contenuto, "determina.pdf.p7m", ocr=ocr, max_pagine=300)
```

Da codice sincrono: `in_markdown_sync(...)`. Errori: `pa_common.errori` (`FileNonValido`,
`ConversioneNonRiuscita` e sottoclassi come `OcrNonDisponibile`, `TroppePagine`,
`TempoScaduto`).

### Scelte

- **DOCX con LibreOffice, non pandoc.** Sugli schemi di determina ANCI e sugli allegati ANAC
  pandoc sostituisce con `[TABLE]` le tabelle con celle unite (5 tabelle su 6 in un allegato)
  e riempie il testo di caratteri di escape; LibreOffice più la lettura diretta dell'ODT le
  conserva.
- **Intestazioni e piè di pagina dei PDF**: PyMuPDF4LLM li tiene (escluderli toglierebbe anche
  i titoli in cima alla pagina) e poi si tolgono le righe ripetute ai bordi delle pagine.
- **OCR**: una pagina va all'OCR se ha meno di 50 caratteri, o più di 1500 tracciati
  vettoriali e meno di 1000 caratteri. Senza OCR configurato, un PDF scansionato è rifiutato
  con `OcrNonDisponibile`.

## Sviluppo

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[pdf,fogli,ocr,test]"
pytest
```

Ogni rilascio: si aggiorna `version` in `pyproject.toml` e `__version__`, si crea il tag
`vX.Y.Z` e si aggiorna la riga di `pa-common` nei progetti.

## Licenza

Copyright (C) 2026 Francesco Bertozzi <info@bertozzifrancesco.it>.

pa-common è software libero: si può ridistribuire e modificare secondo i termini della
**GNU Affero General Public License** pubblicata dalla Free Software Foundation, versione 3
o (a scelta) successiva ([`LICENSE`](LICENSE), SPDX `AGPL-3.0-or-later`), con i termini
aggiuntivi del file [`NOTICE`](NOTICE): va conservata l'attribuzione all'autore e le
versioni modificate vanno indicate come tali. È distribuito senza alcuna garanzia.
PyMuPDF e PyMuPDF4LLM, usati per i PDF, sono anch'essi AGPL-3.0.
