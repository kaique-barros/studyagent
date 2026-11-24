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
