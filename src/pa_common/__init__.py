"""pa-common: codice comune ai progetti per la pubblica amministrazione (atti-pa, rag).

Copyright (C) 2026 Francesco Bertozzi <info@bertozzifrancesco.it>
Licenza: GNU AGPL v3.0 o successiva, con i termini aggiuntivi del file NOTICE.

Moduli:
- `pa_common.ingresso`: tipo reale di un file dal contenuto, buste di firma .p7m;
- `pa_common.conversione`: documenti in Markdown (PDF con OCR delle pagine scansionate,
  ODT, DOC/DOCX/RTF con LibreOffice, fogli di calcolo, immagini, testo);
- `pa_common.ocr`: trascrizione delle immagini con un modello multimodale (Gemini, OpenAI,
  Anthropic, Mistral, vLLM, Ollama);
- `pa_common.testo`: pulizia del testo e del Markdown;
- `pa_common.processo`: esecuzione in un processo separato con un timeout vero.

La libreria non legge variabili d'ambiente: ogni progetto passa la propria configurazione.
"""

__version__ = "0.1.0"
