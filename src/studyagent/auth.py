from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass

# Escopo -> permissões. Cada módulo recebe um token com o escopo mínimo de que precisa.
PERMS: dict[str, set[str]] = {
    # você (shell/CLI): cria, conclui, pede mudanças e DECIDE propostas
    "user": {"tasks:read", "tasks:create", "tasks:complete", "tasks:request", "proposals:read", "proposals:decide", "events:read",
             "dayoffs:write", "planner:run", "schedule:shift"},
    # scraper do Moodle: só cria/atualiza tarefas com source=moodle
    "moodle": {"tasks:read", "tasks:create"},
    # planejador/avaliador e leitor de cronogramas: só PROPÕEM
    "planner": {"tasks:read", "proposals:read", "proposals:create", "events:read", "feedback:create"},
    "schedule": {"tasks:read", "proposals:read", "proposals:create"},
    # leitura (ex.: widgets que só mostram)
    "viewer": {"tasks:read", "proposals:read", "events:read"},
}

# Escopo -> origem (`source`) que ele escreve e os tipos (`kind`) permitidos
SOURCE_OF_SCOPE = {"user": "manual", "moodle": "moodle", "planner": "planner", "schedule": "schedule"}
KINDS_OF_SCOPE: dict[str, set[str]] = {
    "user": {"manual", "study", "review"},
    "moodle": {"moodle"},
    "planner": {"study", "review"},
    "schedule": {"study"},
}


@dataclass(frozen=True)
class Actor:
    name: str
    scope: str

    def can(self, perm: str) -> bool:
        return perm in PERMS.get(self.scope, set())

    @property
    def source(self) -> str:
        return SOURCE_OF_SCOPE.get(self.scope, self.scope)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(conn: sqlite3.Connection, name: str, scope: str, now: str) -> str:
    if scope not in PERMS:
        raise ValueError(f"escopo inválido: {scope} (use um de {', '.join(sorted(PERMS))})")
    token = "sa_" + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO token(name, scope, hash, created_at) VALUES (?,?,?,?)", (name, scope, _hash(token), now))
    return token


def revoke_token(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("UPDATE token SET revoked=1 WHERE name=?", (name,)).rowcount > 0


def authenticate(conn: sqlite3.Connection, token: str) -> Actor | None:
    row = conn.execute("SELECT name, scope FROM token WHERE hash=? AND revoked=0", (_hash(token),)).fetchone()
    return Actor(row["name"], row["scope"]) if row else None
