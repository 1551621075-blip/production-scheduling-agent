"""Artifact fixtures and installation contracts; never build or run a real model.

The installer subprocess tests are mocks, not claims of an actual wheel install.
Source resource/API tests complement, but never replace, the post-freeze wheel gate.
"""
from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import importlib
import io
import json
import sys
import zipfile
from importlib.resources import files
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parents[1]
META = "production_scheduling_agent-0.1.0.dist-info"
WHEEL_NAME = "production_scheduling_agent-0.1.0-py3-none-any.whl"


@pytest.fixture
def checks(monkeypatch):
    monkeypatch.syspath_prepend(str(PROJECT / "scripts"))
    release, install = importlib.import_module("check_release"), importlib.import_module("check_install")

    def denied(*args, **kwargs):
        pytest.fail("package contract tests must mock all installer and Git subprocesses")

    monkeypatch.setattr(install.subprocess, "run", denied)
    return release, install


def fixture_payload(release):
    return {
        **{f"scheduling_agent/{module}.py": b"# original synthetic package fixture\n"
           for module in release.MODULES},
        "scheduling_agent/static/app.js": b"'use strict';",
        "scheduling_agent/static/style.css": b"body { color: black; }",
        "scheduling_agent/templates/index.html": b"<!doctype html><title>Scheduling fixture</title>",
        META + "/METADATA": b"Metadata-Version: 2.3\nName: production-scheduling-agent\n"
        b"Version: 0.1.0\nRequires-Python: >=3.12,<3.13\n",
        META + "/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        META + "/entry_points.txt": b"[console_scripts]\nproduction-scheduling = scheduling_agent.cli:main\n",
    }


def fixture_source(payload):
    return {**{"src/" + name: data for name, data in payload.items()
               if name.startswith("scheduling_agent/")},
            "pyproject.toml": b'[project]\nname="production-scheduling-agent"\nversion="0.1.0"\n'
            b'requires-python=">=3.12,<3.13"\n[project.scripts]\n'
            b'production-scheduling="scheduling_agent.cli:main"\n'}


def write_wheel(path, payload, *, mutate_record=None, tamper=False):
    content = dict(payload)
    rows = []
    for name, data in content.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        rows.append([name, "sha256=" + digest, str(len(data))])
    rows.append([META + "/RECORD", "", ""])
    if mutate_record:
        mutate_record(rows)
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    content[META + "/RECORD"] = output.getvalue().encode()
    if tamper:
        content["scheduling_agent/cli.py"] += b"# tampered\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in content.items():
            archive.writestr(name, data)
    return content


def test_wheel_identity_modules_resources_record_and_source_parity(checks, tmp_path):
    release, _ = checks
    payload = fixture_payload(release)
    path = tmp_path.resolve() / WHEEL_NAME
    expected = write_wheel(path, payload)
    assert release.inspect_wheel(path, fixture_source(payload)) == expected


@pytest.mark.parametrize("missing", [
    "scheduling_agent/templates/index.html", "scheduling_agent/static/app.js", "scheduling_agent/static/style.css",
    "scheduling_agent/compiler.py", "scheduling_agent/solver.py", "scheduling_agent/validator.py",
    "scheduling_agent/simulation.py", "scheduling_agent/agent.py",
    "scheduling_agent/managed_provider.py", "scheduling_agent/receiver.py",
    META + "/entry_points.txt", META + "/WHEEL", META + "/METADATA",
])
def test_missing_required_payload_fails(checks, tmp_path, missing):
    release, _ = checks
    payload = fixture_payload(release)
    del payload[missing]
    path = tmp_path.resolve() / WHEEL_NAME
    write_wheel(path, payload)
    with pytest.raises(release.ReleaseError):
        release.inspect_wheel(path)


@pytest.mark.parametrize("kind", ["empty-resource", "native-tag", "platlib", "extra-entry",
                                      "foreign-metadata", "duplicate-identity", "filename"])
def test_changed_wheel_contract_fails(checks, tmp_path, kind):
    release, _ = checks
    payload = fixture_payload(release)
    if kind == "empty-resource":
        payload["scheduling_agent/static/app.js"] = b""
    elif kind == "native-tag":
        payload[META + "/WHEEL"] = payload[META + "/WHEEL"].replace(
            b"py3-none-any", b"cp312-cp312-macosx_11_0_arm64")
    elif kind == "platlib":
        payload[META + "/WHEEL"] = payload[META + "/WHEEL"].replace(b"true", b"false")
    elif kind == "extra-entry":
        payload[META + "/entry_points.txt"] += b"other = scheduling_agent.cli:main\n"
    elif kind == "foreign-metadata":
        payload["production_scheduling_agent-9.9.dist-info/WHEEL"] = b"Wheel-Version: 1.0\n"
    elif kind == "duplicate-identity":
        payload[META + "/METADATA"] += b"Version: 0.1.0\n"
    path = tmp_path.resolve() / ("renamed.whl" if kind == "filename" else WHEEL_NAME)
    write_wheel(path, payload)
    with pytest.raises(release.ReleaseError):
        release.inspect_wheel(path)


@pytest.mark.parametrize("kind", ["tampered", "missing-row", "extra-row", "duplicate-row",
                                      "bad-size", "bad-hash", "self-hash"])
def test_record_exact_coverage_hash_and_size(checks, tmp_path, kind):
    release, _ = checks

    def mutate(rows):
        if kind == "missing-row":
            rows.pop(0)
        elif kind == "extra-row":
            rows.append(["scheduling_agent/extra.py", "", ""])
        elif kind == "duplicate-row":
            rows.append(rows[0])
        elif kind == "bad-size":
            rows[0][2] = "9999"
        elif kind == "bad-hash":
            rows[0][1] = "sha256=invalid"
        elif kind == "self-hash":
            rows[-1][1] = "sha256=invalid"

    path = tmp_path.resolve() / WHEEL_NAME
    write_wheel(path, fixture_payload(release), mutate_record=mutate, tamper=kind == "tampered")
    with pytest.raises(release.ReleaseError, match="RECORD"):
        release.inspect_wheel(path)


@pytest.mark.parametrize("kind", ["changed-source", "extra-source", "extra-wheel", "version"])
def test_wheel_source_parity_is_exact(checks, tmp_path, kind):
    release, _ = checks
    payload = fixture_payload(release)
    source = fixture_source(payload)
    if kind == "changed-source":
        source["src/scheduling_agent/cli.py"] += b"# new source\n"
    elif kind == "extra-source":
        source["src/scheduling_agent/new_module.py"] = b"# new source\n"
    elif kind == "extra-wheel":
        payload["scheduling_agent/new_module.py"] = b"# unreviewed artifact\n"
    else:
        source["pyproject.toml"] = source["pyproject.toml"].replace(b"0.1.0", b"0.2.0")
    path = tmp_path.resolve() / WHEEL_NAME
    write_wheel(path, payload)
    with pytest.raises(release.ReleaseError, match="parity|identity"):
        release.inspect_wheel(path, source)


def install_fixture(checks, tmp_path):
    release, install = checks
    base = tmp_path.resolve()
    root, work, wheelhouse = (base / name for name in ("source", "work", "wheelhouse"))
    for path in (root, work, wheelhouse):
        path.mkdir()
    payload = fixture_payload(release)
    (root / "dist").mkdir()
    wheel = root / "dist" / WHEEL_NAME
    write_wheel(wheel, payload)
    # Not a real dependency: command-contract tests mock installation, never execute this file.
    (wheelhouse / "generated_dependency.whl").write_bytes(b"fixture")
    return install, wheel, fixture_source(payload), root, wheelhouse, work


def good_report():
    return {"source_free": True, "fresh_venv": True, "new_os": False,
            "actual_distribution": True, "modules": True, "console_help": True,
            "fixture_json": True, "health": True, "static_templates": True,
            "inference_checked": False, "network_used": False}


@pytest.mark.parametrize("case", [None, "normal"])
@pytest.mark.parametrize("extra_module", [False, True])
def test_install_new_venv_offline_isolated_contract(
        checks, tmp_path, monkeypatch, case, extra_module):
    install, wheel, source, root, wheelhouse, work = install_fixture(checks, tmp_path)
    if extra_module:
        payload = fixture_payload(checks[0])
        payload["scheduling_agent/new_module.py"] = b"# reviewed public addition\n"
        source = fixture_source(payload)
        write_wheel(wheel, payload)
    calls = []

    def run(command, **options):
        calls.append((command, options))
        if "venv" in command:
            python = Path(command[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"generated interpreter fixture; not executed")
        return SimpleNamespace(returncode=0, stdout=json.dumps(good_report()))

    monkeypatch.setenv("PYTHONPATH", "ambient-path-must-not-propagate")
    monkeypatch.setenv("HTTP_PROXY", "ambient-proxy-must-not-propagate")
    monkeypatch.setenv("SCHEDULING_RUNTIME_CONFIG", "ambient-config-must-not-propagate")
    monkeypatch.setattr(install.subprocess, "run", run)
    assert install.probe_install(wheel, source, root, wheelhouse, work, case) == good_report()
    create, install_call, dependency_check, probe = [command for command, _ in calls]
    assert create[:5] == [sys.executable, "-I", "-m", "venv", "--copies"]
    assert "--system-site-packages" not in create
    assert {"--no-index", "--only-binary=:all:", "--no-cache-dir", "--find-links"} <= set(install_call)
    assert install.INSTALL in install_call and "build" not in install_call
    assert dependency_check[-1] == "check"
    assert probe[1:5] == ["-I", "-B", "-c", install.PROBE]
    for _, options in calls:
        assert options["cwd"] == work and not options.get("shell")
        assert options["stdin"] == install.subprocess.DEVNULL
        assert not {"PYTHONPATH", "HTTP_PROXY", "SCHEDULING_RUNTIME_CONFIG"} & options["env"].keys()
    manifest = json.loads((work / "probe-manifest.json").read_text())
    assert manifest["case"] == "normal"
    assert set(manifest["modules"]) == checks[0].MODULES | (
        {"new_module"} if extra_module else set())
    assert (work / wheel.name).read_bytes() == wheel.read_bytes()


@pytest.mark.parametrize("failure", ["install", "dependency", "invalid-json", "incomplete"])
def test_install_fails_closed_with_redacted_subprocess_output(checks, tmp_path, monkeypatch, failure):
    install, wheel, source, root, wheelhouse, work = install_fixture(checks, tmp_path)
    calls = []

    def run(command, **options):
        calls.append(command)
        if "venv" in command:
            python = Path(command[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"fixture")
        failed = (failure == "install" and "install" in command
                  or failure == "dependency" and command[-1] == "check")
        return SimpleNamespace(returncode=int(failed), stderr="withheld subprocess details",
                               stdout="not json" if failure == "invalid-json" else "{}")

    monkeypatch.setattr(install.subprocess, "run", run)
    with pytest.raises(checks[0].ReleaseError) as exc:
        install.probe_install(wheel, source, root, wheelhouse, work)
    assert "withheld subprocess details" not in str(exc.value)
    if failure == "install":
        assert len(calls) == 2


@pytest.mark.parametrize("kind", ["inside-source", "nonwheel", "link", "empty", "nested"])
def test_wheelhouse_boundary(checks, tmp_path, kind):
    release, install = checks
    root = tmp_path.resolve() / "source"
    root.mkdir()
    path = root / "wheelhouse" if kind == "inside-source" else tmp_path.resolve() / "wheelhouse"
    path.mkdir()
    if kind == "nonwheel":
        (path / "config.json").write_bytes(b"never read")
    elif kind == "link":
        (path / "dependency.whl").symlink_to(tmp_path / "missing")
    elif kind == "nested":
        (path / "nested.whl").mkdir()
    with pytest.raises(release.ReleaseError):
        install.validate_wheelhouse(path, root)


@pytest.mark.parametrize("kind", ["count", "file-size", "total-size"])
def test_wheelhouse_count_and_size_bounds_without_reading(checks, tmp_path, monkeypatch, kind):
    release, install = checks
    root, wheelhouse = tmp_path.resolve() / "source", tmp_path.resolve() / "wheelhouse"
    root.mkdir()
    wheelhouse.mkdir()
    count = 257 if kind == "count" else 5 if kind == "total-size" else 1
    for index in range(count):
        (wheelhouse / f"dependency_{index}.whl").write_bytes(b"generated fixture")
    size = 128 * 1024 * 1024 + (kind == "file-size")
    if kind != "count":
        monkeypatch.setattr(install, "regular_file", lambda *args: SimpleNamespace(
            lstat=lambda: SimpleNamespace(st_size=size)))
    monkeypatch.setattr(install, "read_regular", lambda *args: pytest.fail("dependency wheel read"))
    with pytest.raises(release.ReleaseError, match="bounded|size limit"):
        install.validate_wheelhouse(wheelhouse, root)


def test_no_source_tree_environment_or_invalid_case(checks, tmp_path):
    install, wheel, source, root, wheelhouse, work = install_fixture(checks, tmp_path)
    with pytest.raises(checks[0].ReleaseError, match="outside"):
        install.probe_install(wheel, source, root, wheelhouse, root)
    with pytest.raises(checks[0].ReleaseError, match="case"):
        install.probe_install(wheel, source, root, wheelhouse, work, "../private")


def test_probe_code_compiles_and_denies_external_effects(checks):
    _, install = checks
    compile(install.PROBE, "<installation-probe>", "exec")
    compile(install.INSTALL, "<offline-install>", "exec")
    for code in (install.PROBE, install.INSTALL):
        assert "sys.addaudithook" in code
        assert all(event in code for event in ("socket.connect", "subprocess.Popen", "os.system"))
    assert 'def decide(' in install.PROBE and 'def session(' in install.PROBE
    assert 'snapshots["normal"]' in install.PROBE
    assert 'invoke(["fixture", "--case", manifest["case"]])' in install.PROBE
    assert '"--now-ms"' not in install.PROBE
    assert "demo_snapshots(now_ms)" in install.PROBE


@pytest.mark.parametrize("binding", ["alias", "module"])
@pytest.mark.parametrize("changed_json", [False, True])
def test_fixture_probe_observes_clock_without_optional_cli_flags(
        checks, monkeypatch, binding, changed_json):
    """Exercise only the fixture block with generated modules, never the actual R6 package."""
    package = ModuleType("scheduling_agent")
    package.__path__ = []
    cli = ModuleType("scheduling_agent.cli")
    fixtures = ModuleType("scheduling_agent.fixtures")
    domain = ModuleType("scheduling_agent.domain")
    calls = []

    class Snapshot:
        __module__ = domain.__name__

        def __init__(self, now_ms):
            self.now_ms = now_ms

        def model_dump(self, *, mode):
            assert mode == "json"
            return {"now_ms": self.now_ms}

        @classmethod
        def model_validate(cls, value):
            return cls(value["now_ms"])

        def __eq__(self, other):
            return isinstance(other, Snapshot) and self.now_ms == other.now_ms

    def demo_snapshots(now_ms):
        calls.append(now_ms)
        return {"normal": Snapshot(now_ms)}

    fixtures.demo_snapshots = demo_snapshots
    domain.Snapshot = Snapshot
    if binding == "alias":
        cli.generate = demo_snapshots
    else:
        cli.fixtures = fixtures
    package.cli, package.fixtures, package.domain = cli, fixtures, domain
    for module in (package, cli, fixtures, domain):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    def invoke(args):
        if args == ["--help"]:
            return "fixture"
        assert args == ["fixture", "--case", "normal"]
        generate = cli.generate if binding == "alias" else cli.fixtures.demo_snapshots
        exported = generate(1_800_000_000_000)["normal"].model_dump(mode="json")
        if changed_json:
            exported["now_ms"] += 1
        return json.dumps(exported)

    fixture_block = checks[1].PROBE.split('help_text = invoke(["--help"])', 1)[1].split(
        "from fastapi.testclient import TestClient", 1)[0]
    namespace = {"contextlib": contextlib, "json": json, "invoke": invoke,
                 "manifest": {"case": "normal"}, "help_text": invoke(["--help"])}
    if changed_json:
        with pytest.raises(AssertionError, match="fixture JSON"):
            exec(compile(fixture_block, "<generated-fixture-probe>", "exec"), namespace)
    else:
        exec(compile(fixture_block, "<generated-fixture-probe>", "exec"), namespace)
    assert calls == [1_800_000_000_000]
    assert fixtures.demo_snapshots is demo_snapshots
    if binding == "alias":
        assert cli.generate is demo_snapshots


def test_install_main_never_proceeds_after_failed_release(checks, tmp_path, monkeypatch, capsys):
    _, install = checks
    monkeypatch.setattr(install, "source_candidates", lambda _: ({}, ["blocked"]))
    monkeypatch.setattr(install, "probe_install", lambda *args: pytest.fail("install attempted"))
    assert install.main(["--root", str(tmp_path.resolve()), "--wheel", "dist/reviewed.whl",
                         "--wheelhouse", str(tmp_path.resolve())]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_source_resources_api_and_cli_without_inference(tmp_path, monkeypatch):
    """Needs finalized R6 modules; not a substitute for the source-free installed probe."""
    from fastapi.testclient import TestClient

    from scheduling_agent import cli, fixtures
    from scheduling_agent.api import create_app
    from scheduling_agent.provider import SyntheticProvider

    def denied(*args, **kwargs):
        pytest.fail("resource and fixture checks must not invoke a model or start a process")

    class NoInference(SyntheticProvider):
        decide = denied
        session = denied

    monkeypatch.setattr("subprocess.Popen", denied)
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.getaddrinfo", denied)
    monkeypatch.setattr("socket.socket.bind", denied)
    resources = {"/": "templates/index.html", "/static/app.js": "static/app.js",
                 "/static/style.css": "static/style.css"}
    for resource in resources.values():
        assert files("scheduling_agent").joinpath(resource).read_bytes()
    provider = NoInference()
    with TestClient(create_app(tmp_path.resolve() / "data", provider),
                    base_url="http://127.0.0.1") as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert isinstance(response.json(), dict)
        for route, resource in resources.items():
            response = client.get(route)
            assert response.status_code == 200
            assert response.content == files("scheduling_agent").joinpath(resource).read_bytes()

    def invoke(args):
        monkeypatch.setattr(sys, "argv", ["production-scheduling", *args])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            try:
                cli.main()
            except SystemExit as exc:
                assert exc.code in (0, None)
        return output.getvalue()

    assert "fixture" in invoke(["--help"])
    demo_snapshots = fixtures.demo_snapshots
    observed = []

    def record_snapshots(now_ms):
        assert isinstance(now_ms, int) and not isinstance(now_ms, bool) and now_ms >= 0
        snapshots = demo_snapshots(now_ms)
        observed.append(snapshots["normal"])
        return snapshots

    for name, value in list(vars(cli).items()):
        if value is demo_snapshots:
            monkeypatch.setattr(cli, name, record_snapshots)
    monkeypatch.setattr(fixtures, "demo_snapshots", record_snapshots)
    exported = json.loads(invoke(["fixture", "--case", "normal"]))
    assert observed, "fixture must use the public demo_snapshots(now_ms) generator"
    snapshot = next((item for item in observed if item.model_dump(mode="json") == exported), None)
    assert snapshot is not None, "fixture JSON must equal a generated normal snapshot"
    assert type(snapshot).__module__ == "scheduling_agent.domain"
    assert type(snapshot).model_validate(exported) == snapshot
    assert exported == snapshot.model_dump(mode="json")


@pytest.mark.parametrize("requires_python", [">=3.12,<3.13", "<3.13,>=3.12", " <3.13, >=3.12 "])
def test_wheel_requires_python_order_only_normalization(checks, tmp_path, requires_python):
    release, _ = checks
    payload = fixture_payload(release)
    source = fixture_source(payload)
    payload[META + "/METADATA"] = payload[META + "/METADATA"].replace(
        b">=3.12,<3.13", requires_python.encode())
    path = tmp_path.resolve() / WHEEL_NAME
    expected = write_wheel(path, payload)
    assert release.inspect_wheel(path, source) == expected


@pytest.mark.parametrize("requires_python", [">=3.11,<3.13", ">=3.12,<3.14", ">=3.12",
                                             ">3.12,<3.13", ">=3.12,<=3.13"])
def test_wheel_requires_python_may_not_widen_or_change_bounds(checks, tmp_path, requires_python):
    release, _ = checks
    payload = fixture_payload(release)
    source = fixture_source(payload)
    payload[META + "/METADATA"] = payload[META + "/METADATA"].replace(
        b">=3.12,<3.13", requires_python.encode())
    path = tmp_path.resolve() / WHEEL_NAME
    write_wheel(path, payload)
    with pytest.raises(release.ReleaseError, match="artifact/source identity mismatch"):
        release.inspect_wheel(path, source)


@pytest.mark.parametrize("name", [WHEEL_NAME, "probe-manifest.json", "venv", "unreviewed.txt"])
@pytest.mark.parametrize("symlink", [False, True])
def test_fresh_install_directory_rejects_existing_state_and_dangling_links(
        checks, tmp_path, monkeypatch, name, symlink):
    install, wheel, source, root, wheelhouse, work = install_fixture(checks, tmp_path)
    outside = tmp_path.resolve() / "must-not-be-created"
    candidate = work / name
    if symlink:
        candidate.symlink_to(outside)
    else:
        candidate.write_bytes(b"must not be overwritten")
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **kw: pytest.fail("install attempted"))
    with pytest.raises(checks[0].ReleaseError, match="fresh empty temporary directory"):
        install.probe_install(wheel, source, root, wheelhouse, work)
    assert not outside.exists()
    assert candidate.is_symlink() if symlink else candidate.read_bytes() == b"must not be overwritten"


def test_modified_wheel_never_reaches_install(checks, tmp_path, monkeypatch):
    install, wheel, source, root, wheelhouse, work = install_fixture(checks, tmp_path)
    write_wheel(wheel, fixture_payload(checks[0]), tamper=True)
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **kw: pytest.fail("install attempted"))
    with pytest.raises(checks[0].ReleaseError, match="RECORD"):
        install.probe_install(wheel, source, root, wheelhouse, work)


def test_unknown_public_case_never_reaches_install(checks, tmp_path, monkeypatch):
    install, wheel, source, root, wheelhouse, work = install_fixture(checks, tmp_path)
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **kw: pytest.fail("install attempted"))
    with pytest.raises(checks[0].ReleaseError, match="invalid fixture case"):
        install.probe_install(wheel, source, root, wheelhouse, work, "unknown_case")
