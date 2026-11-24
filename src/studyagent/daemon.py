from __future__ import annotations

import os
import socket
from pathlib import Path

import uvicorn

from .api import create_app
from .config import Settings


def _bind(path: Path) -> socket.socket:
    """Cria o socket Unix com permissão 0660 (dono e grupo) SEM alterar o umask do processo:
    o umask só vale durante o bind, para que arquivos e pastas criados depois tenham permissões normais."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_socket():
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o117)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old)
    sock.set_inheritable(True)
    return sock


def serve(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    app = create_app(settings)
    sock = _bind(settings.socket)
    try:
        server = uvicorn.Server(uvicorn.Config(app, log_level="info", access_log=False))
        server.run(sockets=[sock])
    finally:
        sock.close()
        try:
            settings.socket.unlink()
        except OSError:
            pass
