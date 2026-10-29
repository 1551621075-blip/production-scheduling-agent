"""Acceptance reporting and isolation without model inference or a running server."""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_acceptance.py"
spec = importlib.util.spec_from_file_location("run_acceptance", SCRIPT)
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


@pytest.fixture
def output(tmp_path):
    # macOS /var is an alias; pass the real path rather than allowing symlink traversal.
    return tmp_path.resolve() / "acceptance-new"


def test_defaults_and_explicit_case_selection(output):
    test = acceptance.parse_args(["--data-root", str(output), "--test-provider"])
    assert test.cases == list(acceptance.CASES)
    real = acceptance.parse_args(["--data-root", str(output), "--runtime-config", "/unused.json"])
    assert real.cases == ["normal"]
    selected = acceptance.parse_args(["--data-root", str(output), "--test-provider",
                                      "--case", "supply_delay", "--case", "normal"])
    assert selected.cases == ["supply_delay", "normal"]


@pytest.mark.parametrize("arguments", [
    ["--data-root", "relative", "--test-provider"],
    ["--data-root", "/tmp/../output", "--test-provider"],
    ["--data-root", "/unused"],
    ["--data-root", "/unused", "--test-provider", "--runtime-config", "/config"],
    ["--data-root", "/unused", "--runtime-config", "relative"],
    ["--data-root", "/unused", "--test-provider", "--case", "normal", "--case", "normal"],
    ["--data-root", "/unused", "--test-provider", "--case", "unknown"],
])
def test_invalid_cli_fails_before_provider_or_output(arguments):
    with pytest.raises(SystemExit) as caught:
        acceptance.parse_args(arguments)
    assert caught.value.code == 2


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling", "parent-symlink",
                                    "missing-parent", "source"])
def test_unsafe_output_is_never_overwritten(output, kind, monkeypatch):
    root = output
    if kind == "directory":
        root.mkdir()
        (root / "acceptance.json").write_text("original", encoding="utf-8")
    elif kind == "file":
        root.write_text("original", encoding="utf-8")
    elif kind in {"symlink", "dangling"}:
        target = output.parent / "target"
        if kind == "symlink":
            target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    elif kind == "parent-symlink":
        alias = output.parent / "alias"
        alias.symlink_to(output.parent, target_is_directory=True)
        root = alias / "uncreated"
    elif kind == "missing-parent":
        root = output / "uncreated"
    else:
        monkeypatch.setattr(acceptance, "SOURCE_ROOT", output.parent)
    with pytest.raises(acceptance.AcceptanceFailure):
        with acceptance.new_report(root):
            pytest.fail("unsafe output accepted")
    if kind == "directory":
        assert (root / "acceptance.json").read_text("utf-8") == "original"
    if kind == "file":
        assert root.read_text("utf-8") == "original"
    if kind in {"missing-parent", "source"}:
        assert not output.exists()


def test_report_and_root_are_exclusive_private(output):
    with acceptance.new_report(output) as stream:
        stream.write('{"status":"failed"}\n')
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    report = output / "acceptance.json"
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert report.stat().st_nlink == 1
    assert json.loads(report.read_text("utf-8")) == {"status": "failed"}
    with pytest.raises(acceptance.AcceptanceFailure):
        with acceptance.new_report(output):
            pytest.fail("existing output accepted")


def test_report_finally_when_provider_creation_fails(output, monkeypatch, capsys):
    from scheduling_agent.provider import ProviderError

    def fail(_args):
        raise ProviderError("runtime_config", "secret credential and prompt must never leak", sent=False)

    monkeypatch.setattr(acceptance, "make_provider", fail)
    assert acceptance.main(["--data-root", str(output), "--test-provider"]) == 1
    report = json.loads((output / "acceptance.json").read_text("utf-8"))
    assert report["status"] == "failed"
    assert report["error_code"] == "runtime_config"
    assert report["provider_close"] == {"status": "not_created"}
    assert "secret" not in json.dumps(report) + capsys.readouterr().out


def test_case_failure_preserved_and_provider_close_failure_tracked(output, monkeypatch, capsys):
    from scheduling_agent.provider import ProviderError, SyntheticProvider

    class CannotClose(SyntheticProvider):
        def close(self):
            raise ProviderError("provider_unknown", "sensitive shutdown details")

    def fail_case(root, name, provider, case):
        raise acceptance.AcceptanceFailure("independent_invalid")

    monkeypatch.setattr(acceptance, "make_provider", lambda args: CannotClose())
    monkeypatch.setattr(acceptance, "run_case", fail_case)
    monkeypatch.setattr(acceptance, "check_api_contract", lambda root: {"status": "passed"})
    assert acceptance.main(["--data-root", str(output), "--test-provider", "--case", "normal"]) == 1
    report = json.loads((output / "acceptance.json").read_text("utf-8"))
    assert report["cases"][0]["error_code"] == "independent_invalid"
    assert report["provider_close"] == {"status": "failed", "error_code": "provider_unknown"}
    assert report["status"] == "failed"
    assert "sensitive" not in json.dumps(report) + capsys.readouterr().out


def test_api_close_failure_is_reported_and_fails_overall(output, monkeypatch):
    monkeypatch.setattr(acceptance, "run_case", lambda root, name, provider, case: None)
    monkeypatch.setattr(acceptance, "check_api_contract", lambda root: {
        "status": "failed", "provider_close": {"status": "failed", "error_code": "provider_unknown"},
    })
    assert acceptance.main(["--data-root", str(output), "--test-provider", "--case", "normal"]) == 1
    report = json.loads((output / "acceptance.json").read_text("utf-8"))
    assert report["api_contract"]["provider_close"]["error_code"] == "provider_unknown"
    assert report["status"] == "failed"


def test_exact_task_error_is_retained_not_mislabelled():
    task = {"status": "blocked", "payload": {"error": "provider_invalid"}}
    with pytest.raises(acceptance.AcceptanceFailure, match="^provider_invalid$"):
        acceptance.expect_task(task, "submitted", "task_not_submitted")


def test_untyped_case_exception_is_redacted(output, monkeypatch, capsys):
    def fail_case(root, name, provider, case):
        raise RuntimeError("private prompt and bearer credential")

    monkeypatch.setattr(acceptance, "run_case", fail_case)
    monkeypatch.setattr(acceptance, "check_api_contract", lambda root: {"status": "passed"})
    assert acceptance.main(["--data-root", str(output), "--test-provider", "--case", "normal"]) == 1
    text = (output / "acceptance.json").read_text("utf-8")
    assert json.loads(text)["cases"][0]["error_code"] == "internal_error"
    assert "private prompt" not in text + capsys.readouterr().out


def test_actual_all_cases_independent_stores_and_secret_free_report(output, monkeypatch, capsys):
    token = "acceptance-" "secret-not-" "in-report-" "0123456789"
    monkeypatch.setattr(acceptance.secrets, "token_urlsafe", lambda length: token)
    assert acceptance.main(["--data-root", str(output), "--test-provider"]) == 0
    text = (output / "acceptance.json").read_text("utf-8")
    report = json.loads(text)
    assert token not in text + capsys.readouterr().out
    assert report["status"] == "passed"
    assert report["mode"] == "TEST-tool-selector" and report["model_inference"] is False
    assert report["causal_effect_proven"] is False and report["production_execution"] is False
    assert report["provider_close"]["status"] == "closed"
    assert report["api_contract"]["fault_exposed"] is False
    cases = {case["case"]: case for case in report["cases"]}
    assert set(cases) == set(acceptance.CASES)
    databases = [output / name / "data.sqlite3" for name in cases]
    assert len({path.stat().st_ino for path in databases}) == len(cases)
    assert cases["normal"]["lost_ack"]["unknown_persisted"] is True
    assert cases["infeasible"]["task_status"] == "no_schedule"
    assert {item["status"] for item in cases["infeasible"]["cp_sat"].values()} == {"INFEASIBLE"}
    assert cases["infeasible"]["publications"] == 0
    assert cases["missing"]["supplement"]["answers"] == [
        {"field": "materials[0].initial_stock", "value": 0},
        {"field": "orders[0].due_min", "value": 8},
    ]
    assert cases["missing"]["supplement"]["observed_time_unchanged"] is True
    for name, case in cases.items():
        assert case["status"] == "passed"
        assert case["terminal_rerun_no_model_call"] is True
        assert case["model_calls"]["unknown_token_calls"] == 0
        assert case["model_calls"]["known_tokens"] == case["model_calls"]["count"] * 128
        assert case["model_calls"]["status_counts"] == {"completed": case["model_calls"]["count"]}
        if name != "infeasible":
            assert case["publication_status"] == "completed"
            assert case["partial_execute_refused"]["error_code"] == "receiver_state"
            assert case["independent_validation"]["valid"] is True
            assert case["execution"]["causal_effect_proven"] is False
            assert case["human_actions_no_model_call"] is True
