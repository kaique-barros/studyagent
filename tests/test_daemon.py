import os
import stat

from studyagent.daemon import _bind


def test_bind_keeps_umask_and_socket_mode(tmp_path):
    before = os.umask(0o022); os.umask(before)
    sock = _bind(tmp_path / "run" / "api.sock")
    try:
        assert stat.S_IMODE(os.stat(tmp_path / "run" / "api.sock").st_mode) == 0o660
        now = os.umask(0o022); os.umask(now)
        assert now == before                           # o umask do processo não mudou
    finally:
        sock.close()


def test_lifespan_runs_daily_plan(tmp_path, clock):
    import time
    from fastapi.testclient import TestClient
    from studyagent.api import create_app
    from studyagent.config import Settings
    s = Settings(state_dir=tmp_path / "st", socket=tmp_path / "s.sock", tasks_file=None, planner_check_seconds=1)
    with TestClient(create_app(s, clock)):
        time.sleep(0.5)
    from studyagent.db import connect
    row = connect(s.db_path).execute("SELECT value FROM meta WHERE key='last_plan_day'").fetchone()
    assert row and row[0] == "2026-11-21"
