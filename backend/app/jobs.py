"""Fila de tarefas persistida no banco (alternativa ao Celery+Redis).

Justificativa: o volume é baixo (um usuário/poucos projetos) e todas as tarefas já
precisam de estado durável no banco (etapas, progresso, resultado). Uma tabela
``jobs`` com ``SELECT ... FOR UPDATE SKIP LOCKED`` no PostgreSQL dá fila durável,
retomada por etapa e cancelamento cooperativo sem um serviço Redis adicional.

- Cada tarefa tem etapas nomeadas; uma etapa concluída não é refeita na retomada.
- Cancelamento: a API marca ``cancel_requested``; o worker verifica entre unidades.
- Reinício do worker: tarefas ``running`` com heartbeat antigo voltam para a fila.
- Repetição: ``retry`` recoloca a tarefa na fila a partir da etapa com erro.
"""
from __future__ import annotations

import logging
import traceback
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import select, update

from .config import get_settings
from .db import Job, SessionLocal, utcnow

log = logging.getLogger("app.jobs")

HANDLERS: dict[str, Callable[["JobContext"], dict | None]] = {}


class JobCancelled(Exception):
    pass


class JobFailed(Exception):
    """Falha com mensagem acionável para o usuário."""


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


class JobContext:
    def __init__(self, job_id: str):
        self.job_id = job_id
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            self.kind = job.kind
            self.project_id = job.project_id
            self.params = dict(job.params or {})
            self.steps = list(job.steps or [])

    # -- estado
    def _save(self, **fields):
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            for k, v in fields.items():
                setattr(job, k, v)
            job.heartbeat_at = utcnow()
            db.commit()

    def check_cancel(self):
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            job.heartbeat_at = utcnow()
            cancel = job.cancel_requested
            db.commit()
        if cancel:
            raise JobCancelled()

    def progress(self, frac: float, message: str | None = None):
        self._save(progress=max(0.0, min(1.0, frac)), message=message)

    def _step(self, name: str) -> dict:
        for s in self.steps:
            if s["name"] == name:
                return s
        s = {"name": name, "status": "pending", "message": None, "result": None}
        self.steps.append(s)
        return s

    def done(self, name: str) -> bool:
        return self._step(name)["status"] == "done"

    def result(self, name: str) -> Any:
        return self._step(name).get("result")

    def run_step(self, name: str, fn: Callable[[], Any], message: str | None = None) -> Any:
        """Executa uma etapa idempotente; se já concluída (retomada), reaproveita o resultado."""
        s = self._step(name)
        if s["status"] == "done":
            return s.get("result")
        self.check_cancel()
        s.update(status="running", message=message, error=None)
        self._save(steps=[dict(x) for x in self.steps], message=message or name)
        try:
            r = fn()
        except JobCancelled:
            s.update(status="cancelled")
            self._save(steps=[dict(x) for x in self.steps])
            raise
        except Exception as exc:
            s.update(status="error", error=str(exc)[:2000])
            self._save(steps=[dict(x) for x in self.steps])
            raise
        s.update(status="done", result=r)
        self._save(steps=[dict(x) for x in self.steps])
        return r

    def reset_steps(self, prefix: str):
        """Permite reexecutar etapas repetitivas (ex.: iterações)."""
        self.steps = [s for s in self.steps if not s["name"].startswith(prefix)]
        self._save(steps=[dict(x) for x in self.steps])


def enqueue(db, project_id: str | None, kind: str, params: dict | None = None) -> Job:
    if kind not in HANDLERS:
        raise ValueError(f"tipo de tarefa desconhecido: {kind}")
    job = Job(project_id=project_id, kind=kind, params=params or {}, status="queued")
    db.add(job)
    db.commit()
    if get_settings().job_inline:
        run_job(job.id)
        db.refresh(job)
    return job


def cancel(db, job: Job):
    if job.status == "queued":
        job.status = "cancelled"
        job.finished_at = utcnow()
    elif job.status == "running":
        job.cancel_requested = True
    db.commit()


def retry(db, job: Job):
    if job.status not in ("failed", "cancelled"):
        raise ValueError("só é possível repetir tarefas com falha ou canceladas")
    steps = [s if s["status"] == "done" else {**s, "status": "pending", "error": None} for s in job.steps or []]
    job.steps = steps
    job.status = "queued"
    job.error = None
    job.cancel_requested = False
    job.finished_at = None
    db.commit()
    if get_settings().job_inline:
        run_job(job.id)


def claim_next() -> str | None:
    with SessionLocal() as db:
        q = select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(1)
        if db.bind.dialect.name == "postgresql":
            q = q.with_for_update(skip_locked=True)
        job = db.scalar(q)
        if job is None:
            return None
        res = db.execute(update(Job).where(Job.id == job.id, Job.status == "queued")
                         .values(status="running", started_at=utcnow(), heartbeat_at=utcnow(),
                                 attempts=Job.attempts + 1))
        db.commit()
        return job.id if res.rowcount else None


def requeue_stale(max_age_s: int | None = None) -> int:
    """Recoloca na fila tarefas cujo worker parou (heartbeat antigo)."""
    age = max_age_s if max_age_s is not None else get_settings().job_stale_seconds
    limit = utcnow() - timedelta(seconds=age)
    with SessionLocal() as db:
        jobs = db.scalars(select(Job).where(Job.status == "running")).all()
        n = 0
        for j in jobs:
            hb = j.heartbeat_at
            if hb is not None and hb.tzinfo is None:
                hb = hb.replace(tzinfo=limit.tzinfo)
            if hb is None or hb < limit:
                j.status = "queued"
                j.message = "retomada após interrupção do worker"
                j.steps = [s if s["status"] == "done" else {**s, "status": "pending"} for s in j.steps or []]
                n += 1
        db.commit()
        return n


def run_job(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job.status == "queued":
            job.status = "running"
            job.started_at = utcnow()
            job.attempts += 1
        job.heartbeat_at = utcnow()
        db.commit()
    ctx = JobContext(job_id)
    fn = HANDLERS[ctx.kind]
    try:
        result = fn(ctx) or {}
        ctx._save(status="succeeded", progress=1.0, result=result, finished_at=utcnow(),
                  message="concluída", steps=[dict(x) for x in ctx.steps])
    except JobCancelled:
        ctx._save(status="cancelled", finished_at=utcnow(), message="cancelada pelo usuário",
                  steps=[dict(x) for x in ctx.steps])
    except JobFailed as exc:
        ctx._save(status="failed", error=str(exc), finished_at=utcnow(), steps=[dict(x) for x in ctx.steps])
    except Exception as exc:  # erro inesperado: registra detalhe técnico sem conteúdo do livro
        log.error("tarefa %s (%s) falhou: %s", job_id, ctx.kind, exc)
        log.debug(traceback.format_exc())
        ctx._save(status="failed", error=f"{type(exc).__name__}: {exc}"[:2000], finished_at=utcnow(),
                  steps=[dict(x) for x in ctx.steps])


@contextmanager
def session():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def job_dict(j: Job) -> dict:
    return {
        "id": j.id, "project_id": j.project_id, "kind": j.kind, "status": j.status, "params": j.params,
        "steps": [{k: v for k, v in s.items() if k != "result"} for s in (j.steps or [])],
        "progress": j.progress, "message": j.message, "error": j.error, "result": j.result,
        "cancel_requested": j.cancel_requested, "attempts": j.attempts,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        "updated_at": j.updated_at.isoformat() if j.updated_at else None,
    }
