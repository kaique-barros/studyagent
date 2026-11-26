from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

Kind = Literal["manual", "moodle", "study", "review"]


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


class TaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    course: str | None = None
    due: date | None = None
    planned_for: date | None = None
    est_minutes: int | None = None
    kind: Kind | None = None            # padrão: o do escopo de quem cria
    tag: str | None = None
    source_ref: str | None = None       # id na origem: reenviar o mesmo atualiza, não duplica

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = _clean(v) or ""
        if not v:
            raise ValueError("título vazio")
        if len(v) > 200:
            raise ValueError("título com mais de 200 caracteres")
        # o título vai para uma linha do Markdown do Kanban: nada de marcadores que mudariam o significado
        if re.search(r"@\{|#\S", v):
            raise ValueError("título não pode conter '#tag' nem '@{data}' (use os campos tag e due)")
        return v

    @field_validator("course", "tag")
    @classmethod
    def _short(cls, v: str | None) -> str | None:
        v = _clean(v)
        if v is not None and len(v) > 60:
            raise ValueError("máximo de 60 caracteres")
        return v

    @field_validator("tag")
    @classmethod
    def _tag(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.lstrip("#")
            if not re.fullmatch(r"[\w-]+", v):
                raise ValueError("tag só aceita letras, números, _ e -")
        return v

    @field_validator("est_minutes")
    @classmethod
    def _est(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 1440):
            raise ValueError("est_minutes deve estar entre 1 e 1440")
        return v

    @field_validator("source_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        v = _clean(v)
        if v is not None and len(v) > 200:
            raise ValueError("source_ref muito longo")
        return v


class SessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int | None = None
    date: date
    start: str = ""
    end: str = ""
    kind: Literal["class", "exam", "holiday", "no_class", "empty"]
    flag: str | None = None
    title: str
    topic: str | None = None
    label: str

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if v and not re.fullmatch(r"\d{2}:\d{2}", v):
            raise ValueError("horário deve ser HH:MM")
        return v

    @field_validator("title", "label")
    @classmethod
    def _len(cls, v: str) -> str:
        if len(v) > 500:
            raise ValueError("texto longo demais")
        return v

    @field_validator("topic")
    @classmethod
    def _topic(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 3000:
            raise ValueError("topic longo demais")
        return v

    @field_validator("flag")
    @classmethod
    def _flag(cls, v: str | None) -> str | None:
        if v is not None and not set(v.split(",")) <= {"duvidas", "revisao"}:
            raise ValueError("flag inválida")
        return v


class ScheduleImportIn(BaseModel):
    """Cronograma lido de um PDF (proposta `schedule.import`)."""
    model_config = ConfigDict(extra="forbid")
    semester: str
    key: str
    code: str | None = None
    name: str | None = None
    section: str | None = None
    professor: str | None = None
    source_hash: str
    sessions: list[SessionIn]
    warnings: list[str] = []

    @field_validator("semester")
    @classmethod
    def _sem(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-[12]", v):
            raise ValueError("semestre deve ser AAAA-S (ex.: 2026-2)")
        return v

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", v):
            raise ValueError("sigla inválida")
        return v

    @field_validator("code", "name", "section", "professor")
    @classmethod
    def _short(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 200:
            raise ValueError("texto longo demais")
        return v

    @field_validator("sessions")
    @classmethod
    def _n(cls, v: list[SessionIn]) -> list[SessionIn]:
        if not (1 <= len(v) <= 250):
            raise ValueError("entre 1 e 250 linhas de cronograma")
        return v

    @field_validator("warnings")
    @classmethod
    def _w(cls, v: list[str]) -> list[str]:
        return [x[:300] for x in v[:50]]


class ShiftIn(BaseModel):
    """Adapta o plano de estudo de um semestre (ou de uma matéria) a partir de uma data."""
    model_config = ConfigDict(extra="forbid")
    semester: str
    key: str | None = None
    start: date | None = None
    clear: bool = False                 # volta ao modo "data da aula - folga"
    k: float | None = None
    lead: int | None = None

    @field_validator("k")
    @classmethod
    def _k(cls, v: float | None) -> float | None:
        if v is not None and not (0.1 <= v <= 3):
            raise ValueError("k deve estar entre 0.1 e 3")
        return v

    @field_validator("lead")
    @classmethod
    def _lead(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 60):
            raise ValueError("lead deve estar entre 0 e 60 dias")
        return v


class RescheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    due: date | None = None
    planned_for: date | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _one(self):
        if self.due is None and self.planned_for is None:
            raise ValueError("informe due e/ou planned_for")
        return self


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None


class ProposalIn(BaseModel):
    """Proposta feita por planejador/leitor de cronogramas (ou por você, para mudar prazo/apagar)."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["task.create", "task.reschedule", "task.drop", "schedule.import"]
    task_id: int | None = None
    task: TaskIn | None = None
    schedule: ScheduleImportIn | None = None
    due: date | None = None
    planned_for: date | None = None
    reason: str | None = None


class FeedbackIn(BaseModel):
    """Texto do agente para você (avaliação do cronograma, revisão semanal). Vira um evento `agent.feedback`."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["evaluation", "review"]
    text: str
    data: dict | None = None

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 4000:
            raise ValueError("texto vazio ou maior que 4000 caracteres")
        return v


class DayoffIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
