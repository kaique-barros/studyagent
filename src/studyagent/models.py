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
    kind: Literal["task.create", "task.reschedule", "task.drop"]
    task_id: int | None = None
    task: TaskIn | None = None
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
