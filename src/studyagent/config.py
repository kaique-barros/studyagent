from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_state_dir() -> Path:
    # systemd define STATE_DIRECTORY (ex.: /var/lib/studyagent); em desenvolvimento usa ~/.local/share
    sd = os.environ.get("STATE_DIRECTORY")
    if sd:
        return Path(sd.split(":")[0])
    return Path.home() / ".local" / "share" / "studyagent"


def _default_socket() -> Path:
    rd = os.environ.get("RUNTIME_DIRECTORY")
    if rd:
        return Path(rd.split(":")[0]) / "api.sock"
    if Path("/run/studyagent").is_dir():          # instalação com systemd: o cliente também acha o socket
        return Path("/run/studyagent/api.sock")
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    return Path(xdg) / "studyagent.sock" if xdg else Path("/tmp/studyagent.sock")


@dataclass
class Settings:
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("STUDYAGENT_STATE_DIR") or _default_state_dir()))
    socket: Path = field(default_factory=lambda: Path(os.environ.get("STUDYAGENT_SOCKET") or _default_socket()))
    # arquivo do Kanban do Obsidian que o agente gera (projeção). Vazio = não gera.
    tasks_file: Path | None = field(default_factory=lambda: Path(os.path.expanduser(os.environ["STUDYAGENT_TASKS_FILE"])) if os.environ.get("STUDYAGENT_TASKS_FILE") else None)
    lane_open: str = field(default_factory=lambda: os.environ.get("STUDYAGENT_LANE_OPEN", "A fazer"))
    lane_done: str = field(default_factory=lambda: os.environ.get("STUDYAGENT_LANE_DONE", "Concluído"))
    done_visible_days: int = 14               # concluídas aparecem na coluna "Concluído" por tantos dias
    day_start_hour: int = 4                   # o "dia" vira às 04:00
    # cronogramas (lidos pelo CLI, não pelo daemon): <raiz>/<AAAA-S>/Matérias/<Sigla>/Cronograma.pdf
    schedule_root: Path = field(default_factory=lambda: Path(os.path.expanduser(os.environ.get("STUDYAGENT_SCHEDULE_ROOT", "~/Obsidian Vault/UFMG"))))
    # planejador
    planner_enabled: bool = field(default_factory=lambda: os.environ.get("STUDYAGENT_PLANNER", "1") != "0")
    daily_target: int = 4                     # tarefas por dia
    dayoff_target: int = 2                    # tarefas em dia de folga
    horizon_days: int = 14                    # quantos dias à frente o planejador organiza
    near_due_days: int = 3                    # prazo dentro dessa janela dispara um replanejamento
    programmed_goal: int = 24                 # tarefas abertas "programadas" para a sequência não depender de sorte
    dayoff_max_month: int = 10
    dayoff_min_month: int = 4
    dayoff_lead_days: int = 1                 # folga só pode ser criada/removida com 1 dia de antecedência
    planner_check_seconds: int = 300          # o daemon confere se precisa planejar a cada 5 min

    @property
    def db_path(self) -> Path:
        return self.state_dir / "studyagent.db"
