"""Offline installation gate for an explicitly reviewed scheduling wheel, never a build command.

Use a new venv outside the source tree and an explicit, reviewed local wheelhouse.
Source-free fresh-environment evidence is NOT evidence of a new OS/host install.
No runtime configuration is read and no actual inference or Java is needed.
"""
from __future__ import annotations

import argparse
import configparser
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

from check_release import (
    DISTRIBUTION,
    ENTRY_POINT,
    RESOURCES,
    ReleaseError,
    checked_root,
    explicit_archive,
    inspect_wheel,
    project_identity,
    read_regular,
    regular_file,
    source_candidates,
)

INSTALL = r'''
import runpy
import sys

def no_external_effects(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind", "subprocess.Popen", "os.system"}:
        raise RuntimeError("offline installer forbids network and child processes")
sys.addaudithook(no_external_effects)
sys.argv = ["pip", *sys.argv[1:]]
runpy.run_module("pip", run_name="__main__")
'''

PROBE = r'''
import contextlib
import hashlib
import importlib
import importlib.metadata
import importlib.resources
import io
import json
import pathlib
import runpy
import sys
import sysconfig

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text("utf-8"))
root = pathlib.Path(manifest["source_root"])
work = pathlib.Path.cwd().resolve()
prefix = pathlib.Path(sys.prefix).resolve()
site = pathlib.Path(sysconfig.get_path("purelib")).resolve()
assert sys.flags.isolated and sys.prefix != sys.base_prefix
assert sys.flags.optimize == 0, "probe assertions must be active"
assert prefix.is_relative_to(work) and site.is_relative_to(prefix)
assert not work.is_relative_to(root)
assert all(not pathlib.Path(p).resolve().is_relative_to(root) for p in sys.path if p)
assert sysconfig.get_path("purelib") == sysconfig.get_path("platlib")

# In-process TestClient only. Even an accidental provider call cannot start a model or connect.
def no_external_effects(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind", "subprocess.Popen", "os.system"}:
        raise RuntimeError("installation probe forbids network and subprocesses")
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = pathlib.Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if path.is_relative_to(root):
            raise RuntimeError("installation probe forbids reading the source tree")
sys.addaudithook(no_external_effects)

matches = [dist for dist in importlib.metadata.distributions()
           if dist.metadata["Name"] == manifest["distribution"]]
assert len(matches) == 1
dist = matches[0]
assert dist.version == manifest["version"]
assert pathlib.Path(dist.locate_file("")).resolve() == site
recorded = {str(path) for path in dist.files}
for name, expected in manifest["hashes"].items():
    assert name in recorded
    path = pathlib.Path(dist.locate_file(name))
    assert path.resolve().is_relative_to(site) and not path.is_symlink()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected

for name in manifest["modules"]:
    module = importlib.import_module("scheduling_agent" if name == "__init__" else "scheduling_agent." + name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(site / "scheduling_agent")
entry, = [ep for ep in dist.entry_points if ep.group == "console_scripts"]
assert entry.name == "production-scheduling" and entry.value == manifest["entry_point"]
entry.load()
console = pathlib.Path(sysconfig.get_path("scripts")) / "production-scheduling"
assert console.is_file() and not console.is_symlink()
launcher = console.read_text("utf-8")
# pip uses a /bin/sh trampoline for long/spaced interpreter paths; it is also valid Python.
assert launcher.splitlines()[0] in ("#!" + sys.executable, "#!/bin/sh")
assert sys.executable in launcher

# Execute the actual installer-generated console script under isolated Python, not a CliRunner.
def invoke(args):
    sys.argv = [str(console), *args]
    with contextlib.redirect_stdout(io.StringIO()) as output:
        try:
            runpy.run_path(str(console), run_name="__main__")
        except SystemExit as exc:
            assert exc.code in (0, None)
    return output.getvalue()
help_text = invoke(["--help"])
assert "fixture" in help_text
from unittest.mock import patch
import scheduling_agent.cli as cli
import scheduling_agent.fixtures as fixtures

# Observe the real generator's input instead of assuming a CLI clock implementation,
# a --now-ms flag, a default case, or the as-yet-unfinalized domain class name.
demo_snapshots = fixtures.demo_snapshots
observed = []
def record_snapshots(now_ms):
    assert isinstance(now_ms, int) and not isinstance(now_ms, bool) and now_ms >= 0
    snapshots = demo_snapshots(now_ms)
    observed.append(snapshots["normal"])
    return snapshots
with contextlib.ExitStack() as patches:
    for name, value in list(vars(cli).items()):
        if value is demo_snapshots:
            patches.enter_context(patch.object(cli, name, record_snapshots))
    patches.enter_context(patch.object(fixtures, "demo_snapshots", record_snapshots))
    exported = json.loads(invoke(["fixture", "--case", manifest["case"]]))
assert observed, "fixture must use the public demo_snapshots(now_ms) generator"
snapshot = next((item for item in observed if item.model_dump(mode="json") == exported), None)
assert snapshot is not None, "fixture JSON must equal a generated normal snapshot"
assert type(snapshot).__module__ == "scheduling_agent.domain"
assert type(snapshot).model_validate(exported) == snapshot
assert exported == snapshot.model_dump(mode="json")

from fastapi.testclient import TestClient
from scheduling_agent.api import create_app
from scheduling_agent.provider import SyntheticProvider
class NoInference(SyntheticProvider):
    def decide(self, *args, **kwargs):
        raise AssertionError("health/assets must not request inference")
    def session(self, *args, **kwargs):
        raise AssertionError("health/assets must not open a model session")

provider = NoInference()
app = create_app(work / "probe-data", provider)
with TestClient(app, base_url="http://127.0.0.1") as client:
    response = client.get("/health")
    assert response.status_code == 200
    assert isinstance(response.json(), dict)
    for resource in manifest["resources"]:
        route = "/" if resource == "templates/index.html" else "/" + resource
        expected = importlib.resources.files("scheduling_agent").joinpath(resource).read_bytes()
        assert expected
        response = client.get(route)
        assert response.status_code == 200 and response.content == expected
        content_type = "text/html" if route == "/" else (
            "application/javascript" if route.endswith(".js") else "text/css")
        assert response.headers["content-type"].startswith(content_type)
for name, module in list(sys.modules.items()):
    if name == "scheduling_agent" or name.startswith("scheduling_agent."):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(site / "scheduling_agent")
print(json.dumps({"source_free": True, "fresh_venv": True, "new_os": False,
                  "actual_distribution": True, "modules": True, "console_help": True,
                  "fixture_json": True, "health": True, "static_templates": True,
                  "inference_checked": False, "network_used": False}))
'''


def clean_environment(temporary: Path) -> dict[str, str]:
    return {"HOME": str(temporary), "TMPDIR": str(temporary), "TMP": str(temporary),
            "TEMP": str(temporary), "XDG_CACHE_HOME": str(temporary), "PATH": "/usr/bin:/bin",
            "LC_ALL": "C", "NO_COLOR": "1", "TERM": "dumb",
            "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1"}


def _run(command: list[str], temporary: Path, stage: str, timeout: int = 120):
    result = subprocess.run(command, cwd=temporary, env=clean_environment(temporary),
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=timeout, check=False)
    if result.returncode:
        raise ReleaseError(f"{stage} failed; subprocess output withheld")
    return result


def validate_wheelhouse(path: Path, root: Path) -> Path:
    path = checked_root(path)
    if path.is_relative_to(root):
        raise ReleaseError("wheelhouse must be explicit and outside the source tree")
    count, total = 0, 0
    for item in path.iterdir():
        count += 1
        if count > 256 or not item.name.endswith(".whl"):
            raise ReleaseError("wheelhouse must contain only bounded regular wheel files")
        info = regular_file(path, item.name).lstat()
        total += info.st_size
        if info.st_size > 128 * 1024 * 1024 or total > 512 * 1024 * 1024:
            raise ReleaseError("wheelhouse size limit")
    if not count:
        raise ReleaseError("wheelhouse is empty; dependencies are never downloaded")
    return path


def probe_install(wheel: Path, source: dict[str, bytes], root: Path, wheelhouse: Path,
                  temporary: Path, case: str | None = None) -> dict:
    root = checked_root(root)
    temporary = checked_root(temporary)
    if temporary.is_relative_to(root):
        raise ReleaseError("fresh environment must be outside the source tree")
    if any(temporary.iterdir()):
        raise ReleaseError("fresh empty temporary directory required")
    if case not in {None, "normal"}:
        raise ReleaseError("invalid fixture case")
    wheelhouse = validate_wheelhouse(wheelhouse, root)
    # Snapshot first: install exactly the bytes that passed the artifact/source parity gate.
    staged = temporary / wheel.name
    with staged.open("xb") as stream:
        stream.write(read_regular(checked_root(wheel.absolute().parent), wheel.name,
                                  32 * 1024 * 1024))
    payload = inspect_wheel(staged, source)
    version, _ = project_identity(source)
    manifest = {
        "source_root": str(root), "distribution": DISTRIBUTION, "version": version,
        "entry_point": ENTRY_POINT, "case": case or "normal",
        "modules": sorted(Path(name).stem for name in payload
                          if name.startswith("scheduling_agent/") and name.endswith(".py")),
        "resources": sorted(RESOURCES),
        "hashes": {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()
                   if not name.endswith(".dist-info/RECORD")},
    }
    manifest_path = temporary / "probe-manifest.json"
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream)
    environment = temporary / "venv"
    if environment.exists():
        raise ReleaseError("new venv required; reuse and system-site-packages are forbidden")
    _run([sys.executable, "-I", "-m", "venv", "--copies", str(environment)],
         temporary, "venv creation")
    python = environment / "bin" / "python"
    if not python.is_file() or python.is_symlink():
        raise ReleaseError("a copied POSIX venv interpreter is required")
    _run([str(python), "-I", "-c", INSTALL, "--isolated", "--disable-pip-version-check",
          "install", "--no-index", "--find-links", str(wheelhouse), "--only-binary=:all:",
          "--no-compile", "--no-cache-dir", "--no-input", str(staged)], temporary,
         "offline wheel installation")
    _run([str(python), "-I", "-c", INSTALL, "--isolated", "--disable-pip-version-check", "check"],
         temporary, "installed dependency consistency")
    result = _run([str(python), "-I", "-B", "-c", PROBE, str(manifest_path)],
                  temporary, "isolated installed-distribution probe", timeout=90)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise ReleaseError("probe returned invalid JSON") from None
    expected = {"source_free": True, "fresh_venv": True, "new_os": False,
                "actual_distribution": True, "modules": True, "console_help": True,
                "fixture_json": True, "health": True, "static_templates": True,
                "inference_checked": False, "network_used": False}
    if report != expected:
        raise ReleaseError("probe did not establish all installation checks")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--wheel", required=True, type=Path, help="Explicit reviewed dist/*.whl")
    parser.add_argument("--wheelhouse", required=True, type=Path,
                        help="Reviewed, flat offline dependency wheels outside the source tree")
    parser.add_argument("--case", choices=("normal",),
                        help="Public CLI fixture case; always passes --case normal explicitly")
    args = parser.parse_args(argv)
    try:
        root = checked_root(args.root)
        source, issues = source_candidates(root)
        if issues:
            raise ReleaseError("source release gate failed; no installation attempted")
        wheel = explicit_archive(root, args.wheel)
        inspect_wheel(wheel, source)
        # Ignore ambient TMPDIR: do not create even temporary state inside the source tree.
        temp_base = checked_root(Path("/tmp").resolve())
        if temp_base.is_relative_to(root):
            raise ReleaseError("system temporary directory is inside the source tree")
        with tempfile.TemporaryDirectory(prefix="scheduling-install-", dir=temp_base) as directory:
            temporary = Path(directory)
            result = probe_install(wheel, source, root, args.wheelhouse, temporary, args.case)
        print(json.dumps(result, sort_keys=True))
        print("PASS: source-free fresh venv; not a new OS; no actual inference")
        return 0
    except (ReleaseError, OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile,
            configparser.Error, csv.Error, EOFError, zlib.error):
        print("FAIL: release/install/probe gate failed; no payload printed; no download or fallback")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
