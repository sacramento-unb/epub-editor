"""Worker de tarefas: `python -m app.worker`.

Retoma tarefas interrompidas (heartbeat antigo) ao iniciar e periodicamente.
Encerra de forma limpa com SIGTERM/SIGINT depois da tarefa corrente; uma tarefa
interrompida à força é retomada na etapa pendente pelo próximo worker.
"""
from __future__ import annotations

import logging
import signal
import time

from . import tasks  # noqa: F401  (registra handlers)
from .config import get_settings
from .db import init_db
from .jobs import claim_next, requeue_stale, run_job
from .secrets import install_log_redaction

log = logging.getLogger("app.worker")
_stop = False


def _sig(*_):
    global _stop
    _stop = True
    log.info("sinal recebido; encerrando após a tarefa atual")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    init_db()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    n = requeue_stale()
    if n:
        log.info("%d tarefa(s) interrompida(s) recolocada(s) na fila", n)
    last_sweep = time.monotonic()
    poll = get_settings().worker_poll_seconds
    while not _stop:
        jid = claim_next()
        if jid:
            log.info("executando tarefa %s", jid)
            run_job(jid)
            continue
        if time.monotonic() - last_sweep > 30:
            requeue_stale()
            last_sweep = time.monotonic()
        time.sleep(poll)


if __name__ == "__main__":
    main()
