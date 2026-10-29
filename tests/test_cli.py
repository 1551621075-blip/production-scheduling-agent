"""CLI tests use real private Stores, but mock every server and runtime constructor."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from scheduling_agent import cli
from scheduling_agent.domain import Snapshot
from scheduling_agent.storage import Store

RUNNER = CliRunner()


@pytest.fixture
def initialized(tmp_path):
    path = tmp_path.resolve() / "local-data"
    store = Store(path)
    token = "".join(("cli-test-", "admin-token-", "a" * 24))
    principal = store.create_user("cli-admin", token, "admin", ["synthetic-plant"])
    return path, store, principal


def test_init_imports_only_snapshots_and_shows_one_random_token(tmp_path, monkeypatch):
    from scheduling_agent import provider, solver

    inference = Mock(side_effect=AssertionError("init must not infer"))
    solve = Mock(side_effect=AssertionError("init must not solve"))
    monkeypatch.setattr(provider.LocalProvider, "decide", inference)
    monkeypatch.setattr(provider.SyntheticProvider, "decide", inference)
    monkeypatch.setattr(solver, "solve", solve)
    path = tmp_path.resolve() / "new-data"
    result = RUNNER.invoke(cli.app, ["init", str(path)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    token = report["token"]
    assert result.output.count(token) == 1
    assert len(token) >= 32
    assert report["role"] == "admin"
    assert report["plants"] == ["synthetic-plant"]
    assert report["snapshots_imported"] == 3
    assert path.stat().st_mode & 0o777 == 0o700
    store = Store(path)
    principal = store.authenticate(token)
    snapshots = store.list_objects("snapshot", principal)
    assert len(snapshots) == 3
    for obj in snapshots:
        assert Snapshot.model_validate(obj["payload"]).plant_id == "synthetic-plant"
    for kind in ("task", "recommendation", "publication"):
        assert store.list_objects(kind, principal) == []
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    for file in path.rglob("*"):
        if file.is_file():
            assert token.encode() not in file.read_bytes()
    solve.assert_not_called()
    inference.assert_not_called()
    again = RUNNER.invoke(cli.app, ["init", str(path)])
    assert again.exit_code != 0
    assert token not in again.output
    assert store.authenticate(token) == principal
    second = RUNNER.invoke(cli.app, ["init", str(tmp_path.resolve() / "another-data")])
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["token"] != token


def test_init_requires_new_root_with_existing_parent(tmp_path):
    root = tmp_path.resolve()
    existing = root / "existing"
    existing.mkdir()
    assert RUNNER.invoke(cli.app, ["init", str(existing)]).exit_code != 0
    missing = root / "missing" / "child"
    assert RUNNER.invoke(cli.app, ["init", str(missing)]).exit_code != 0
    assert not missing.parent.exists()
    regular = root / "file"
    regular.write_text("unchanged")
    assert RUNNER.invoke(cli.app, ["init", str(regular)]).exit_code != 0
    assert regular.read_text() == "unchanged"


def test_init_rejects_symlinks_and_parent_traversal(tmp_path):
    root = tmp_path.resolve()
    real = root / "real"
    real.mkdir()
    link = root / "link"
    link.symlink_to(real, target_is_directory=True)
    for path in (link / "new", real / ".." / "new"):
        result = RUNNER.invoke(cli.app, ["init", str(path)])
        assert result.exit_code != 0
        assert "unsafe_path" in result.output
    assert not (real / "new").exists()
    assert not (root / "new").exists()


@pytest.mark.parametrize("case", cli.FIXTURE_CASES)
def test_fixture_outputs_one_valid_snapshot_without_wrapper(case):
    result = RUNNER.invoke(cli.app, ["fixture", "--case", case])
    assert result.exit_code == 0, result.output
    snapshot = Snapshot.model_validate(json.loads(result.stdout))
    assert snapshot.snapshot_id == f"synthetic-{case}-v1"
    assert snapshot.plant_id == "synthetic-plant"
    if case == "missing":
        assert snapshot.materials[0].initial_stock is None
        assert snapshot.orders[0].due_min is None


def test_fixture_default_normal_no_path_and_no_training_command():
    result = RUNNER.invoke(cli.app, ["fixture"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["snapshot_id"] == "synthetic-normal-v1"
    for args in (["fixture", "--case", "../../secret"], ["fixture", "--path", "/not-read"],
                 ["train"], ["fixture", "--token", "not-accepted"]):
        assert RUNNER.invoke(cli.app, args).exit_code != 0


def test_init_failure_sanitizes_internal_details_and_prints_no_token(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.SchedulingService, "import_snapshot",
                        Mock(side_effect=RuntimeError("sensitive-details")))
    result = RUNNER.invoke(cli.app, ["init", str(tmp_path.resolve() / "failed-init")])
    assert result.exit_code != 0
    assert "sensitive-details" not in result.output
    assert '"token"' not in result.output


def test_serve_never_implicitly_initializes(tmp_path):
    path = tmp_path.resolve() / "missing"
    result = RUNNER.invoke(cli.app, ["serve", str(path), "--test-mode"])
    assert result.exit_code != 0
    assert "not_initialized" in result.output
    assert not path.exists()


@pytest.mark.parametrize("options", [
    [], ["--test-mode", "--model-url", "http://127.0.0.1:9999"],
    ["--test-mode", "--runtime-config", "/not-read.json"],
    ["--runtime-config", "/not-read.json", "--model-url", "http://127.0.0.1:9999"],
    ["--test-mode", "--runtime-config", "/not-read.json", "--model-url", "http://127.0.0.1"],
])
def test_serve_requires_exactly_one_explicit_provider(initialized, options):
    path, *_ = initialized
    result = RUNNER.invoke(cli.app, ["serve", str(path), *options])
    assert result.exit_code != 0
    assert "provider_choice" in result.output


@pytest.mark.parametrize("options", [
    ["--port", "0"], ["--port", "65536"], ["--port", "1.5"],
    ["--host", "0.0.0.0"], ["--host", "localhost"], ["--host", "::1"],
])
def test_serve_host_is_fixed_ipv4_loopback_and_port_is_valid(initialized, options):
    path, *_ = initialized
    assert RUNNER.invoke(cli.app, ["serve", str(path), "--test-mode", *options]).exit_code != 0


@pytest.mark.parametrize("lifespan", [False, True])
def test_serve_mock_server_no_proxy_or_inference_closes_once(initialized, monkeypatch, lifespan):
    import uvicorn
    from fastapi.testclient import TestClient

    from scheduling_agent import provider

    path, *_ = initialized
    instance = Mock(fingerprint="TEST-no-inference")
    factory = Mock(return_value=instance)
    monkeypatch.setattr(provider, "SyntheticProvider", factory)

    def run(application, **kwargs):
        assert kwargs == {"host": "127.0.0.1", "port": 8006, "access_log": False,
                          "proxy_headers": False, "server_header": False, "log_level": "warning"}
        if lifespan:
            with TestClient(application, base_url="http://127.0.0.1:8006") as client:
                assert client.get("/health").status_code == 200

    server = Mock(side_effect=run)
    monkeypatch.setattr(uvicorn, "run", server)
    result = RUNNER.invoke(cli.app, ["serve", str(path), "--test-mode"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["inference_checked"] is False
    assert report["listen"] == "http://127.0.0.1:8006"
    factory.assert_called_once_with()
    server.assert_called_once()
    instance.decide.assert_not_called()
    instance.session.assert_not_called()
    instance.close.assert_called_once_with()


@pytest.mark.parametrize("url", ["http://127.0.0.1:9999", "http://[::1]:9999/"])
def test_serve_explicit_local_url_and_custom_port_are_mocked(initialized, monkeypatch, url):
    import uvicorn

    from scheduling_agent import provider

    path, *_ = initialized
    instance = Mock(fingerprint="local-explicit")
    factory = Mock(return_value=instance)
    monkeypatch.setattr(provider, "LocalProvider", factory)
    server = Mock()
    monkeypatch.setattr(uvicorn, "run", server)
    result = RUNNER.invoke(cli.app, ["serve", str(path), "--model-url", url, "--port", "8016"])
    assert result.exit_code == 0, result.output
    factory.assert_called_once_with(url)
    assert server.call_args.kwargs["port"] == 8016
    instance.decide.assert_not_called()
    instance.close.assert_called_once_with()


@pytest.mark.parametrize("url", [
    "http://localhost:9999", "http://192.168.1.5:9999", "https://127.0.0.1:9999",
    "http://127.0.0.1:9999/v1", "".join(("http://127.0.0.1:9999", "?", "token=x")),
    "".join(("http://", "user@", "127.0.0.1:9999")),
    "http://127.0.0.1:9999#", "http://[::1]suffix:9999", "http://127.0.0.1:0",
    "http://2130706433:9999", "http://127.0.0.1:", "http://[::1%25lo0]:9999",
])
def test_invalid_url_rejected_before_provider_constructor(initialized, monkeypatch, url):
    from scheduling_agent import provider

    path, *_ = initialized
    factory = Mock(side_effect=AssertionError("must not construct provider"))
    monkeypatch.setattr(provider, "LocalProvider", factory)
    result = RUNNER.invoke(cli.app, ["serve", str(path), "--model-url", url])
    assert result.exit_code != 0
    assert "model_url" in result.output
    factory.assert_not_called()


def test_managed_config_is_only_passed_to_mock_and_failure_closes(initialized, monkeypatch):
    import uvicorn

    from scheduling_agent import managed_provider

    path, *_ = initialized
    instance = Mock(fingerprint="managed-explicit")
    factory = Mock(return_value=instance)
    monkeypatch.setattr(managed_provider, "ManagedLocalProvider", factory)
    monkeypatch.setattr(uvicorn, "run", Mock(side_effect=RuntimeError("sensitive-details")))
    result = RUNNER.invoke(cli.app, ["serve", str(path), "--runtime-config", "/explicit-not-read.json"])
    assert result.exit_code != 0
    assert "sensitive-details" not in result.output
    factory.assert_called_once_with(Path("/explicit-not-read.json"))
    instance.session.assert_not_called()
    instance.close.assert_called_once_with()


def test_relative_runtime_path_is_rejected_before_constructor(initialized, monkeypatch):
    from scheduling_agent import managed_provider

    path, *_ = initialized
    factory = Mock(side_effect=AssertionError("must not open runtime file"))
    monkeypatch.setattr(managed_provider, "ManagedLocalProvider", factory)
    result = RUNNER.invoke(cli.app, ["serve", str(path), "--runtime-config", "not-read.json"])
    assert result.exit_code != 0
    assert "runtime_config" in result.output
    factory.assert_not_called()


def test_create_app_failure_closes_constructed_provider(initialized, monkeypatch):
    from scheduling_agent import api, provider

    path, *_ = initialized
    instance = Mock(fingerprint="TEST-startup-failure")
    monkeypatch.setattr(provider, "SyntheticProvider", Mock(return_value=instance))
    monkeypatch.setattr(api, "create_app", Mock(side_effect=RuntimeError("sensitive-details")))
    result = RUNNER.invoke(cli.app, ["serve", str(path), "--test-mode"])
    assert result.exit_code != 0
    assert "sensitive-details" not in result.output
    instance.close.assert_called_once_with()


def test_main_dispatches_without_running_server(monkeypatch):
    app = Mock()
    monkeypatch.setattr(cli, "app", app)
    cli.main(["fixture", "--case", "normal"])
    app.assert_called_once_with(args=["fixture", "--case", "normal"], prog_name="production-scheduling")
