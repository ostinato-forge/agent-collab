from __future__ import annotations

import importlib.util
import subprocess
import os
from pathlib import Path
import threading


MODULE_PATH = Path(__file__).resolve().parents[1] / "brokerctl.py"
SPEC = importlib.util.spec_from_file_location("brokerctl", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
brokerctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(brokerctl)


def test_chat_channel_is_stable_and_sorted() -> None:
    assert brokerctl.chat_channel("builder", "architect") == "dm:architect:builder"
    assert brokerctl.chat_channel("architect", "builder") == "dm:architect:builder"


def test_resolve_token_prefers_explicit_then_env_then_file(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n", encoding="utf-8")

    monkeypatch.setenv("AGENT_COLLAB_TOKEN", "env-token")
    assert brokerctl.resolve_token("explicit-token", str(token_file)) == "explicit-token"
    assert brokerctl.resolve_token(None, str(token_file)) == "env-token"

    monkeypatch.delenv("AGENT_COLLAB_TOKEN")
    assert brokerctl.resolve_token(None, str(token_file)) == "file-token"


def test_auth_headers_omits_empty_token() -> None:
    assert brokerctl.auth_headers(None) == {}
    assert brokerctl.auth_headers("token-123") == {"X-Agent-Collab-Token": "token-123"}


def test_default_peer_for_standard_roles() -> None:
    assert brokerctl.default_peer_for("builder", None) == "architect"
    assert brokerctl.default_peer_for("architect", None) == "builder"
    assert brokerctl.default_peer_for("builder", "reviewer") == "reviewer"


def test_sanitize_terminal_text_escapes_control_characters() -> None:
    assert brokerctl.sanitize_terminal_text("line\nnext\t\x1b") == "line\\nnext\\t\\x1b"


def test_print_message_sanitizes_output(capsys) -> None:
    brokerctl.print_message(
        {
            "at": "2026-04-22T12:00:00+00:00",
            "from": "architect",
            "body": "hello\n\x1b[31mworld",
            "kind": "chat",
        },
        print_lock=threading.Lock(),
    )
    output = capsys.readouterr().out
    assert "\\n" in output
    assert "\\x1b" in output


def test_ensure_daemon_running_reuses_healthy_daemon(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(repo, collab_script, argv, *, check=True):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="daemon: status=running run_mode=foreground pid=123 age_seconds=1 health=fresh\n",
            stderr="",
        )

    def fake_popen(*args, **kwargs):
        raise AssertionError("daemon should not be launched when status is already healthy")

    monkeypatch.setattr(brokerctl, "run_collab_command", fake_run)
    monkeypatch.setattr(brokerctl.subprocess, "Popen", fake_popen)

    result = brokerctl.ensure_daemon_running(
        tmp_path,
        tmp_path / "scripts" / "collab.py",
        role="builder",
        interval=15,
        log_path=tmp_path / ".collab" / "builder-daemon.log",
    )

    assert result.action == "already running"
    assert calls == [["daemon", "--role", "builder", "status"]]


def test_ensure_daemon_running_launches_daemon_until_healthy(tmp_path, monkeypatch) -> None:
    responses = iter(
        [
            subprocess.CompletedProcess(
                ["daemon", "--role", "builder", "status"],
                1,
                stdout="daemon: not running\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                ["daemon", "--role", "builder", "status"],
                1,
                stdout="daemon: status=starting\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                ["daemon", "--role", "builder", "status"],
                0,
                stdout="daemon: status=running run_mode=foreground pid=456 age_seconds=0 health=fresh\n",
                stderr="",
            ),
        ]
    )
    launched: list[list[str]] = []

    def fake_run(repo, collab_script, argv, *, check=True):
        return next(responses)

    class FakePopen:
        pid = 456

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        launched.append(list(cmd))
        return FakePopen()

    monkeypatch.setattr(brokerctl, "run_collab_command", fake_run)
    monkeypatch.setattr(brokerctl.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(brokerctl.time, "sleep", lambda _: None)

    log_path = tmp_path / ".collab" / "builder-daemon.log"
    result = brokerctl.ensure_daemon_running(
        tmp_path,
        tmp_path / "scripts" / "collab.py",
        role="builder",
        interval=15,
        log_path=log_path,
    )

    assert result.action == "started"
    assert result.pid == 456
    assert result.log_path == log_path
    assert launched == [
        [
            os.sys.executable,
            str(tmp_path / "scripts" / "collab.py"),
            "daemon",
            "--role",
            "builder",
            "up",
            "--interval",
            "15",
        ]
    ]


def test_start_session_bootstraps_repo_and_starts_daemon(tmp_path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    collab_script = repo / "scripts" / "collab.py"
    collab_script.parent.mkdir(parents=True)
    collab_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_get_json(url: str, *, token: str | None):
        assert url == "http://127.0.0.1:9111/status"
        assert token == "token-123"
        return {"total": 4}

    def fake_run(repo_path, collab_path, argv, *, check=True):
        calls.append(list(argv))
        command = argv[0]
        outputs = {
            "init": "initialized .collab\n",
            "enable": "enabled agent runtime for builder (actor=builder, peer=architect, enforce=warn)\n",
            "sync": "sync builder by builder at 2026-04-23T15:00:00+00:00\n",
            "next": "TASK-010 [proposed] Fix startup path\n",
            "doctor": "doctor: healthy\n",
        }
        return subprocess.CompletedProcess(argv, 0, stdout=outputs[command], stderr="")

    daemon_calls: list[tuple[Path, Path, str, int, Path]] = []

    def fake_ensure_daemon_running(repo_path, collab_path, *, role, interval, log_path):
        daemon_calls.append((repo_path, collab_path, role, interval, log_path))
        return brokerctl.DaemonLaunchResult(action="started", log_path=log_path, pid=4321)

    monkeypatch.setattr(brokerctl, "_get_json", fake_get_json)
    monkeypatch.setattr(brokerctl, "run_collab_command", fake_run)
    monkeypatch.setattr(brokerctl, "ensure_daemon_running", fake_ensure_daemon_running)

    args = brokerctl.build_parser().parse_args(
        ["start-session", "--role", "builder", "--repo", str(repo)]
    )
    assert brokerctl.command_start_session(
        args,
        base_url="http://127.0.0.1:9111",
        token="token-123",
    ) == 0

    assert calls == [
        ["init"],
        ["enable", "--role", "builder", "--actor", "builder", "--peer", "architect", "--enforce", "warn"],
        ["sync", "--role", "builder", "--actor", "builder", "--note", "session start"],
        ["next", "--role", "builder"],
        ["doctor", "--role", "builder"],
    ]
    assert daemon_calls == [
        (
            repo.resolve(),
            collab_script.resolve(),
            "builder",
            15,
            repo.resolve() / ".collab" / "builder-daemon.log",
        )
    ]

    output = capsys.readouterr().out
    assert "Agent-collab session started" in output
    assert "pending_messages=4" in output
    assert "runtime:" in output
    assert "daemon: started pid=4321 interval=15s" in output
    assert "python3 brokerctl.py recv --role builder --wait 60" in output


def test_start_session_skips_enable_and_doctor_when_daemon_boot_is_disabled(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo = tmp_path / "repo"
    collab_script = repo / "scripts" / "collab.py"
    collab_script.parent.mkdir(parents=True)
    collab_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    runtime_config = repo / ".collab" / "runtime" / "builder.json"
    runtime_config.parent.mkdir(parents=True)
    runtime_config.write_text("{}", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_get_json(url: str, *, token: str | None):
        return {"message_count": 0}

    def fake_run(repo_path, collab_path, argv, *, check=True):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=f"{argv[0]} ok\n", stderr="")

    def fake_ensure_daemon_running(*args, **kwargs):
        raise AssertionError("daemon bootstrap should be skipped when --no-daemon is set")

    monkeypatch.setattr(brokerctl, "_get_json", fake_get_json)
    monkeypatch.setattr(brokerctl, "run_collab_command", fake_run)
    monkeypatch.setattr(brokerctl, "ensure_daemon_running", fake_ensure_daemon_running)

    args = brokerctl.build_parser().parse_args(
        [
            "start-session",
            "--role",
            "builder",
            "--repo",
            str(repo),
            "--actor",
            "codex",
            "--no-daemon",
        ]
    )
    assert brokerctl.command_start_session(
        args,
        base_url="http://127.0.0.1:9111",
        token="token-123",
    ) == 0

    assert calls == [
        ["init"],
        ["sync", "--role", "builder", "--actor", "codex", "--note", "session start"],
        ["next", "--role", "builder"],
    ]

    output = capsys.readouterr().out
    assert "runtime: already enabled" in output
    assert "daemon: skipped (--no-daemon)" in output
    assert "doctor: skipped (daemon bootstrap disabled)" in output


def test_start_session_reuses_existing_runtime_identity_when_not_overridden(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo = tmp_path / "repo"
    collab_script = repo / "scripts" / "collab.py"
    collab_script.parent.mkdir(parents=True)
    collab_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    runtime_config = repo / ".collab" / "runtime" / "builder.json"
    runtime_config.parent.mkdir(parents=True)
    runtime_config.write_text(
        '{"actor": "codex-builder", "peer": "reviewer"}',
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    def fake_get_json(url: str, *, token: str | None):
        return {"message_count": 0}

    def fake_run(repo_path, collab_path, argv, *, check=True):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=f"{argv[0]} ok\n", stderr="")

    monkeypatch.setattr(brokerctl, "_get_json", fake_get_json)
    monkeypatch.setattr(brokerctl, "run_collab_command", fake_run)

    args = brokerctl.build_parser().parse_args(
        [
            "start-session",
            "--role",
            "builder",
            "--repo",
            str(repo),
            "--no-daemon",
        ]
    )
    assert brokerctl.command_start_session(
        args,
        base_url="http://127.0.0.1:9111",
        token="token-123",
    ) == 0

    assert calls == [
        ["init"],
        ["sync", "--role", "builder", "--actor", "codex-builder", "--note", "session start"],
        ["next", "--role", "builder"],
    ]

    output = capsys.readouterr().out
    assert "actor: codex-builder" in output
    assert "peer: reviewer" in output
    assert (repo / ".collab" / "runtime" / "active-role.txt").read_text(encoding="utf-8") == "builder\n"


def test_start_session_surfaces_doctor_warnings_without_failing(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo = tmp_path / "repo"
    collab_script = repo / "scripts" / "collab.py"
    collab_script.parent.mkdir(parents=True)
    collab_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_get_json(url: str, *, token: str | None):
        return {"total": 1}

    def fake_run(repo_path, collab_path, argv, *, check=True):
        calls.append(list(argv))
        if argv[0] == "doctor":
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="issues:\n  - current task expects another branch\n",
                stderr="",
            )
        if argv[0] == "enable":
            return subprocess.CompletedProcess(argv, 0, stdout="enable ok\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=f"{argv[0]} ok\n", stderr="")

    def fake_ensure_daemon_running(repo_path, collab_path, *, role, interval, log_path):
        return brokerctl.DaemonLaunchResult(action="already running", log_path=log_path, pid=None)

    monkeypatch.setattr(brokerctl, "_get_json", fake_get_json)
    monkeypatch.setattr(brokerctl, "run_collab_command", fake_run)
    monkeypatch.setattr(brokerctl, "ensure_daemon_running", fake_ensure_daemon_running)

    args = brokerctl.build_parser().parse_args(
        ["start-session", "--role", "builder", "--repo", str(repo)]
    )
    assert brokerctl.command_start_session(
        args,
        base_url="http://127.0.0.1:9111",
        token="token-123",
    ) == 0

    assert calls[-1] == ["doctor", "--role", "builder"]
    output = capsys.readouterr().out
    assert "doctor (warnings):" in output
    assert "current task expects another branch" in output


def test_start_session_updates_active_role_when_runtime_already_exists(
    tmp_path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    collab_script = repo / "scripts" / "collab.py"
    collab_script.parent.mkdir(parents=True)
    collab_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    runtime_dir = repo / ".collab" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "builder.json").write_text("{}", encoding="utf-8")
    (runtime_dir / "active-role.txt").write_text("architect\n", encoding="utf-8")

    def fake_get_json(url: str, *, token: str | None):
        return {"total": 0}

    def fake_run(repo_path, collab_path, argv, *, check=True):
        return subprocess.CompletedProcess(argv, 0, stdout=f"{argv[0]} ok\n", stderr="")

    monkeypatch.setattr(brokerctl, "_get_json", fake_get_json)
    monkeypatch.setattr(brokerctl, "run_collab_command", fake_run)

    args = brokerctl.build_parser().parse_args(
        ["start-session", "--role", "builder", "--repo", str(repo), "--no-daemon"]
    )
    assert brokerctl.command_start_session(
        args,
        base_url="http://127.0.0.1:9111",
        token="token-123",
    ) == 0

    assert (runtime_dir / "active-role.txt").read_text(encoding="utf-8") == "builder\n"
