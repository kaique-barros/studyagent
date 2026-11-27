from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Migrações em ordem; o índice + 1 é o `user_version` depois de aplicada.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE task (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        title        TEXT NOT NULL,
        course       TEXT,
        due          TEXT,                       -- aaaa-mm-dd (prazo; vem da fonte)
        planned_for  TEXT,                       -- aaaa-mm-dd (quando fazer; vem do planejador)
        est_minutes  INTEGER,
        kind         TEXT NOT NULL DEFAULT 'manual',   -- manual | moodle | study | review
        source       TEXT NOT NULL DEFAULT 'manual',   -- quem criou: manual | moodle | planner | schedule
        source_ref   TEXT,                       -- id na origem (idempotência)
        tag          TEXT,
        status       TEXT NOT NULL DEFAULT 'open',     -- open | done | dropped
        done_at      TEXT,
        verification TEXT NOT NULL DEFAULT 'self',     -- self | quiz (futuro) | delivery
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE UNIQUE INDEX task_source_ref ON task(source, source_ref) WHERE source_ref IS NOT NULL;
    CREATE INDEX task_status_due ON task(status, due);

    CREATE TABLE proposal (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source       TEXT NOT NULL,
        kind         TEXT NOT NULL,              -- task.create | task.reschedule | task.drop
        payload      TEXT NOT NULL,              -- JSON validado
        reason       TEXT,
        status       TEXT NOT NULL DEFAULT 'pending',  -- pending | applied | rejected | stale
        task_id      INTEGER REFERENCES task(id),
        task_version TEXT,                       -- updated_at da tarefa quando a proposta foi feita
        created_at   TEXT NOT NULL,
        decided_at   TEXT,
        result       TEXT
    );
    CREATE INDEX proposal_status ON proposal(status);

    CREATE TABLE event (                          -- log append-only (XP, auditoria, SSE)
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT NOT NULL,
        type    TEXT NOT NULL,
        payload TEXT NOT NULL
    );

    CREATE TABLE token (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL UNIQUE,
        scope      TEXT NOT NULL,
        hash       TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        revoked    INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE dayoff (                         -- dias de folga escolhidos por você (meta diária menor)
        date       TEXT PRIMARY KEY,              -- aaaa-mm-dd
        source     TEXT NOT NULL DEFAULT 'manual',
        created_at TEXT NOT NULL
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    """
    CREATE TABLE course (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        semester     TEXT NOT NULL,               -- 2026-2
        key          TEXT NOT NULL,               -- sigla (nome da pasta da matéria)
        code         TEXT, name TEXT, section TEXT, professor TEXT,
        source_hash  TEXT,                        -- sha256 do Cronograma.pdf importado por último
        study_start  TEXT,                        -- aaaa-mm-dd: início do plano de estudo adaptado (vazio = data da aula - study_lead)
        study_k      REAL NOT NULL DEFAULT 1.0,   -- ritmo: 1 = mesmo espaçamento das aulas; 0.5 = comprime à metade
        study_lead   INTEGER NOT NULL DEFAULT 5,  -- dias de antecedência do estudo em relação à aula
        imported_at  TEXT,
        UNIQUE(semester, key)
    );
    CREATE TABLE session (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
        n         INTEGER,
        date      TEXT NOT NULL,
        start     TEXT NOT NULL DEFAULT '',
        end       TEXT NOT NULL DEFAULT '',
        kind      TEXT NOT NULL,                  -- class | exam | holiday | no_class | empty
        flag      TEXT,                           -- duvidas | revisao (destacar no calendário)
        title     TEXT NOT NULL,
        topic     TEXT,
        label     TEXT NOT NULL,                  -- texto do calendário: "SIGLA: assunto" ou "SIGLA: vazio"
        task_id   INTEGER REFERENCES task(id),    -- tarefa de estudo desta aula
        UNIQUE(course_id, date, start)
    );
    CREATE INDEX session_date ON session(date);
    """,
    """
    CREATE TABLE day_snap (                       -- última foto de cada dia: base do fechamento (streak)
        day        TEXT PRIMARY KEY,
        scheduled  TEXT NOT NULL DEFAULT '[]',    -- ids das tarefas marcadas para o dia
        programmed INTEGER NOT NULL DEFAULT 0,    -- tarefas programadas (abertas + feitas no dia)
        dayoff     INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
    ALTER TABLE task ADD COLUMN started_at TEXT;                         -- cronômetro em andamento
    ALTER TABLE task ADD COLUMN spent_seconds INTEGER NOT NULL DEFAULT 0;
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)  # autocommit; transações explícitas
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(version, len(MIGRATIONS)):
        conn.executescript("BEGIN;" + MIGRATIONS[i] + f"PRAGMA user_version={i + 1};COMMIT;")
    return len(MIGRATIONS)


@contextmanager
def tx(conn: sqlite3.Connection):
    """Transação que pega o lock de escrita logo no início (evita deadlock entre leitores/escritores)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
