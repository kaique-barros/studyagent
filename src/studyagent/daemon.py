from __future__ import annotations

import os
import stat
from pathlib import Path

import uvicorn

from .api import create_app
from .config import Settings


def serve(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    app = create_app(settings)
    sock: Path = settings.socket
    sock.parent.mkdir(parents=True, exist_ok=True)
    if sock.exists() or sock.is_socket():
        sock.unlink()
    old = os.umask(0o117)   # socket 0660: dono e grupo; o usuário entra no grupo do agente
    try:
        config = uvicorn.Config(app, uds=str(sock), log_level="info", access_log=False)
        server = uvicorn.Server(config)
        server.run()
    finally:
        os.umask(old)
