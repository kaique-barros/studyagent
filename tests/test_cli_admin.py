import os

from typer.testing import CliRunner

from studyagent.cli import app


def run(args, tmp_path):
    env = {"STUDYAGENT_STATE_DIR": str(tmp_path / "state")}
    return CliRunner().invoke(app, args, env=env)


def test_token_create_replace(tmp_path):
    assert run(["admin", "init"], tmp_path).exit_code == 0
    out = tmp_path / "t1"
    r = run(["admin", "token-create", "shell", "user", "--out", str(out)], tmp_path)
    assert r.exit_code == 0 and out.read_text().startswith("sa_")
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"

    r = run(["admin", "token-create", "shell", "user"], tmp_path)           # nome repetido
    assert r.exit_code == 1 and "--replace" in r.output

    out2 = tmp_path / "t2"
    r = run(["admin", "token-create", "shell", "user", "--replace", "--out", str(out2)], tmp_path)
    assert r.exit_code == 0 and out2.read_text() != out.read_text()

    r = run(["admin", "token-create", "x", "inexistente"], tmp_path)
    assert r.exit_code == 1
