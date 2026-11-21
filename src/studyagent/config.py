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

    @property
    def db_path(self) -> Path:
        return self.state_dir / "studyagent.db"
