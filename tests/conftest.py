from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from studyagent import auth
from studyagent.api import create_app
from studyagent.config import Settings
from studyagent.db import connect

TZ = timezone(timedelta(hours=-3))


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 21, 10, 0, tzinfo=TZ)

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path / "state", socket=tmp_path / "s.sock", tasks_file=tmp_path / "vault" / "Tarefas.md")


@pytest.fixture
def env(settings, clock):
    app = create_app(settings, clock)
    conn = connect(settings.db_path)
    tokens = {s: auth.create_token(conn, f"t-{s}", s, "2026-11-21T10:00:00-03:00") for s in ("user", "moodle", "planner", "schedule", "viewer")}
    conn.close()
    client = TestClient(app)

    def h(scope):
        return {"Authorization": f"Bearer {tokens[scope]}"}

    client.h = h
    client.settings = settings
    return client
