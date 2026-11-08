"""Esecuzione in un processo separato con un timeout che lo termina davvero.

Con un pool di processi e `asyncio.wait_for`, allo scadere si smette solo di aspettare: il
processo bloccato (un PDF vettoriale enorme, un foglio infinito) resta vivo e occupa uno
slot per sempre. Qui ogni chiamata ha un processo dedicato che, allo scadere, riceve SIGTERM
e poi SIGKILL.

Il risultato si legge dalla coda fin da subito (non dopo `join`): un risultato più grande del
buffer della pipe (es. pagine rasterizzate) bloccherebbe il figlio in `put` mentre il padre
aspetta che finisca.

Da codice asincrono: `await asyncio.to_thread(esegui, funzione, argomenti, timeout)`.
La funzione e i suoi argomenti devono essere serializzabili (funzioni di modulo).
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errori import ConversioneNonRiuscita, TempoScaduto

log = logging.getLogger("pa_common.processo")

_CONTESTO = mp.get_context("spawn")
_DIAGNOSI_SECONDI = 30  # se il figlio resta bloccato, scrive il proprio stack su stderr


def _avvio(coda, funzione, argomenti):
    import faulthandler

    faulthandler.dump_traceback_later(_DIAGNOSI_SECONDI, repeat=True, exit=False)
    try:
        coda.put(("ok", funzione(*argomenti)))
    except ConversioneNonRiuscita as e:  # errori attesi: si ripropongono uguali
        coda.put(("conversione", (type(e).__name__, str(e))))
    except Exception as e:  # noqa: BLE001
        coda.put(("errore", f"{type(e).__name__}: {e}"))
    finally:
        faulthandler.cancel_dump_traceback_later()


def esegui(funzione: Callable[..., Any], argomenti: tuple, timeout: float) -> Any:
    """Risultato di `funzione(*argomenti)` calcolato in un processo dedicato.

    Solleva `TempoScaduto` se non finisce entro `timeout` secondi (il processo viene
    terminato), la stessa `ConversioneNonRiuscita` del figlio, `RuntimeError` per gli altri
    errori.
    """
    coda = _CONTESTO.Queue()
    processo = _CONTESTO.Process(target=_avvio, args=(coda, funzione, argomenti))
    processo.start()
    try:
        esito, dato = coda.get(timeout=timeout)
    except queue.Empty:
        log.warning("Processo %s fermo da %.0f s: terminato", processo.pid, timeout)
        processo.terminate()
        processo.join(5)
        if processo.is_alive():
            processo.kill()
            processo.join()
        raise TempoScaduto(f"Elaborazione interrotta dopo {timeout:.0f} s") from None
    processo.join(5)
    if processo.is_alive():
        processo.terminate()
        processo.join()
    if esito == "conversione":
        from . import errori

        nome, messaggio = dato
        raise getattr(errori, nome, ConversioneNonRiuscita)(messaggio)
    if esito == "errore":
        raise RuntimeError(f"Errore nel processo di elaborazione: {dato}")
    return dato


def cpu_disponibili() -> int:
    """CPU utilizzabili dal container (limite del cgroup v2 o v1), almeno 1. `os.cpu_count`
    restituisce quelle dell'host, troppe in un pod con un limite basso."""
    try:
        cpu_max = Path("/sys/fs/cgroup/cpu.max")
        if cpu_max.exists():
            quota, periodo = cpu_max.read_text().split()[:2]
            if quota != "max":
                return max(1, int(quota) // int(periodo))
        quota_v1 = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        periodo_v1 = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota_v1.exists() and periodo_v1.exists():
            quota = int(quota_v1.read_text().strip())
            if quota > 0:
                return max(1, quota // int(periodo_v1.read_text().strip()))
    except (OSError, ValueError):
        pass
    return max(1, os.cpu_count() or 1)
