"""Generated boundary fixtures only; Git responses are mocked, never executed."""
from __future__ import annotations

import gzip
import importlib
import io
import os
import stat
import subprocess
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release(monkeypatch):
    monkeypatch.syspath_prepend(str(PROJECT / "scripts"))
    return importlib.import_module("check_release")


@pytest.fixture(autouse=True)
def no_git_or_child_processes(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("boundary fixtures must not execute Git or child processes")

    monkeypatch.setattr(subprocess, "run", denied)


def mock_git_candidates(release, monkeypatch, root, names, ignored=()):
    """Explicit fixture listing only; never discover the surrounding filesystem."""
    def git(base, *args, data=None):
        assert base == root or base.is_relative_to(root)
        if args == ("rev-parse", "--show-toplevel"):
            output, code = str(root).encode() + b"\n", 0
        elif args == ("ls-files", "--cached", "--others", "--exclude-standard", "-z"):
            assert base == root
            output, code = b"\0".join(name.encode() for name in names) + b"\0", 0
        else:
            assert args == ("check-ignore", "--no-index", "--stdin", "-z")
            assert data == b"\0".join(name.encode() for name in sorted(set(names))) + b"\0"
            output = b"\0".join(name.encode() for name in ignored)
            output, code = (output + b"\0", 0) if ignored else (b"", 1)
        return subprocess.CompletedProcess(["git", *args], code, stdout=output, stderr=b"")

    monkeypatch.setattr(release, "_git", git)


@pytest.mark.parametrize("name", [
    "README.md", "THIRD_PARTY_NOTICES.md", ".gitignore", "pyproject.toml", "uv.lock",
    "scripts/check_release.py",
    "scripts/check_install.py", "scripts/run_acceptance.py", "tests/test_release.py",
    "tests/conftest.py", "docs/runtime.md", "src/scheduling_agent/solver.py",
    "src/scheduling_agent/templates/index.html", "src/scheduling_agent/static/app.js",
])
def test_allowlist(release, name):
    assert release.allowed_path(name)


@pytest.mark.parametrize("name", [
    "runtime/config.json", "models/model.gguf", ".env", "secret.json", "LICENSE",
    "THIRD_PARTY_NOTICES", "THIRD_PARTY_NOTICES.txt", "third_party_notices",
    "docs/THIRD_PARTY_NOTICES", "scheduling_agent/THIRD_PARTY_NOTICES",
    "docs/notes.txt", "src/scheduling_agent/local.so", "src/scheduling_agent/local.jar",
    "src/scheduling_agent/__pycache__/api.pyc", "src/scheduling_agent/private/data.py",
    "other_project/module.py", "../README.md", "/README.md", "docs/../README.md",
    "docs\\private.md", "docs//runtime.md", "docs/x\n.md", "docs/./runtime.md",
])
def test_reject_nonpublic_paths(release, name):
    try:
        assert not release.allowed_path(name)
    except release.ReleaseError:
        pass


@pytest.mark.parametrize("value", [
    "sk-" + "a" * 32,
    "ghp_" + "b" * 36,
    "AKIA" + "A" * 16,
    "-----BEGIN " + "PRIVATE KEY-----",
    "-----BEGIN " + "RSA PRIVATE KEY-----",
    "-----BEGIN " + "EC PRIVATE KEY-----",
    "-----BEGIN " + "DSA PRIVATE KEY-----",
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----",
    "token = '" + "fixture-sensitive-" * 3 + "'",
    "https://" + "user:password@example.invalid/path",
    "http://127.0.0.1/?" + "token=x",
    "Bearer " + "fixture-sensitive-" * 3,
    "eyJ" + "a" * 20 + "." + "b" * 24 + "." + "c" * 24,
    "/" + "Users/fixture/private.json",
    "file:///" + "home/fixture/private.json",
    "%252F" + "Users%252Ffixture%252Fprivate.json",
    "C:" + "\\Users\\fixture\\private.json",
    "~" + "/private.json",
    "https://" + "cdn." + "jsdelivr.net/npm/tool",
    '<script src="' + "https://assets.example.invalid/tool.js" + '"></script>',
    '@import "' + "//assets.example.invalid/style.css" + '";',
    "fetch('" + "https://assets.example.invalid/data" + "')",
])
def test_rules_have_no_test_exceptions_or_payload_output(release, value):
    for name in ("README.md", "THIRD_PARTY_NOTICES.md", "docs/runtime.md", "tests/test_provider.py",
                 "scripts/check_release.py"):
        findings = release.scan_content(name, value.encode())
        assert findings and all(value not in issue for issue in findings)


def test_public_reference_links_and_self_scan(release):
    assert not release.scan_content("docs/dependencies.md", b"https://github.com/ggml-org/llama.cpp")
    for name in ("scripts/check_release.py", "scripts/check_install.py", "tests/test_release.py",
                 "tests/test_package.py"):
        assert not release.scan_content(name, (PROJECT / name).read_bytes())


@pytest.mark.parametrize("content", [b"\x00binary", b"\xff", b"x" * (2 * 1024 * 1024 + 1)])
def test_binary_and_size_limits(release, content):
    assert release.scan_content("README.md", content)


@pytest.mark.parametrize("kind", ["symlink", "parent-link", "hardlink", "fifo"])
def test_regular_files_no_links_or_devices(release, tmp_path, kind):
    root = tmp_path.resolve()
    target = root / "target"
    target.mkdir()
    (target / "README.md").write_text("public fixture")
    if kind == "parent-link":
        (root / "docs").symlink_to(target, target_is_directory=True)
        name = "docs/README.md"
    else:
        name = "README.md"
        if kind == "symlink":
            (root / name).symlink_to(target / name)
        elif kind == "hardlink":
            os.link(target / name, root / name)
        else:
            os.mkfifo(root / name)
    with pytest.raises(release.ReleaseError):
        release.read_regular(root, name)


def test_never_read_ignored_unapproved_or_parent_runtime(release, tmp_path, monkeypatch):
    root = tmp_path.resolve() / "project"
    root.mkdir()
    (root / ".gitignore").write_text("runtime/\n.env\n")
    (root / "README.md").write_text("public fixture")
    (root / "runtime").mkdir()
    private = root / "runtime" / "config.json"
    private.write_text("must not be read")
    (root / ".env").write_text("must not be read")
    (root / "private.json").write_text("must not be read")
    (root.parent / "runtime.json").write_text("must not be read")
    mock_git_candidates(release, monkeypatch, root,
                        [".gitignore", "README.md", "private.json", "runtime/config.json"],
                        ignored=["runtime/config.json"])
    original = release.read_regular
    opened = []

    def guard(base, name, *args):
        assert base == root and name in {".gitignore", "README.md"}
        opened.append(name)
        return original(base, name, *args)

    monkeypatch.setattr(release, "read_regular", guard)
    files, issues = release.source_candidates(root)
    assert set(files) == set(opened) == {".gitignore", "README.md"}
    assert any("ignored-but-tracked" in issue for issue in issues)
    assert any("outside-public-allowlist" in issue for issue in issues)
    assert len(issues) == 2
    with pytest.raises(release.ReleaseError, match="exact Git project root"):
        release.source_candidates(root / "runtime")


def test_git_environment_cannot_redirect_candidate_root(release, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("GIT_DIR", "untrusted-override")
    monkeypatch.setenv("GIT_INDEX_FILE", "untrusted-index")
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    release._git(tmp_path, "ls-files")
    assert not any(key.startswith("GIT_") for key in calls[0][1]["env"])


@pytest.mark.parametrize("name", [
    "src/scheduling_agent/client.key", "src/scheduling_agent/client.PEM",
    "src/scheduling_agent/runtime/config.py", "src/scheduling_agent/.scheduling-data/state.py",
    "runtime/service.key", "runtime/service.pem",
])
def test_key_pem_runtime_candidates_rejected_before_open(release, tmp_path, monkeypatch, name):
    root = tmp_path.resolve()
    mock_git_candidates(release, monkeypatch, root, [name])
    monkeypatch.setattr(release, "read_regular", lambda *args: pytest.fail("private candidate read"))
    files, issues = release.source_candidates(root)
    assert not files and issues == ["candidate outside-public-allowlist; not-read"]
    assert not release.allowed_path(name)
    assert not release.allowed_path(name.removeprefix("src/"), wheel=True)


@pytest.mark.parametrize("name", [
    "../escape.py", "models/model.gguf", "scheduling_agent/local.so",
    "scheduling_agent/client.key", "scheduling_agent/client.PEM",
    "scheduling_agent/runtime/config.py", "scheduling_agent/.scheduling-data/state.py",
])
def test_archive_rejects_unapproved_members(release, tmp_path, name):
    path = tmp_path.resolve() / "fixture.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, b"not read")
    with pytest.raises(release.ReleaseError):
        release.read_archive(path)


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFDIR])
def test_zip_member_must_be_regular(release, tmp_path, mode):
    path = tmp_path.resolve() / "fixture.whl"
    info = zipfile.ZipInfo("scheduling_agent/api.py")
    info.create_system = 3
    info.external_attr = (mode | 0o644) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, "fixture")
    with pytest.raises(release.ReleaseError, match="non-regular"):
        release.read_archive(path)


@pytest.mark.parametrize("kind", ["duplicate", "case", "private-directory", "size", "count"])
def test_zip_bounds(release, tmp_path, monkeypatch, kind):
    path = tmp_path.resolve() / "fixture.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("scheduling_agent/__init__.py", b"fixture")
        if kind == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate"):
                archive.writestr("scheduling_agent/__init__.py", b"")
        elif kind == "case":
            archive.writestr("scheduling_agent/__INIT__.py", b"")
        elif kind == "private-directory":
            archive.writestr("models/", b"")
        elif kind == "size":
            monkeypatch.setattr(release, "MAX_FILE_BYTES", 1)
        else:
            monkeypatch.setattr(release, "MAX_FILES", 0)
    with pytest.raises(release.ReleaseError):
        release.read_archive(path)


def source_fixture():
    return {"README.md": b"original scheduling fixture", "scripts/check_release.py": b"# gate\n",
            "pyproject.toml": b'[project]\nname="production-scheduling-agent"\nversion="0.1.0"\n'
            b'requires-python=">=3.12,<3.13"\n[project.scripts]\n'
            b'production-scheduling="scheduling_agent.cli:main"\n'}


def write_sdist(path, files, link=False):
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo("production_scheduling_agent-0.1.0/" + name)
            if link:
                info.type = tarfile.LNKTYPE
                info.linkname = "README.md"
            else:
                info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def identity_metadata(requires_python):
    return BytesParser().parsebytes(
        ("Metadata-Version: 2.3\nName: production-scheduling-agent\nVersion: 0.1.0\n"
         f"Requires-Python: {requires_python}\n").encode()
    )


@pytest.mark.parametrize("requires_python", [">=3.12,<3.13", "<3.13,>=3.12", "<3.13, >=3.12"])
def test_requires_python_clause_order_accepted(release, tmp_path, requires_python):
    source = source_fixture()
    metadata = identity_metadata(requires_python)
    # Wheel METADATA and sdist PKG-INFO share this identity gate.
    assert release._identity(metadata, source) == "0.1.0"
    files = {**source, "PKG-INFO": metadata.as_bytes()}
    path = tmp_path.resolve() / "production_scheduling_agent-0.1.0.tar.gz"
    write_sdist(path, files)
    assert release.inspect_sdist(path, source) == files


@pytest.mark.parametrize("requires_python", [
    ">=3.11,<3.13", ">=3.12,<3.14", ">3.12,<3.13", ">=3.12,<=3.13", ">=3.12",
    ">=3.12,<3.13,<3.13",
])
def test_requires_python_changed_clauses_rejected(release, requires_python):
    with pytest.raises(release.ReleaseError, match="artifact/source identity mismatch"):
        release._identity(identity_metadata(requires_python), source_fixture())


@pytest.mark.parametrize("requires_python", [
    "", " ", ",>=3.12,<3.13", ">=3.12,<3.13,", ">=3.12,,<3.13", ">=3.12, ,<3.13",
])
@pytest.mark.parametrize("location", ["artifact", "source", "both", "without-source"])
def test_requires_python_empty_clauses_rejected(release, requires_python, location):
    source = source_fixture()
    if location in {"source", "both"}:
        source["pyproject.toml"] = source["pyproject.toml"].replace(
            b">=3.12,<3.13", requires_python.encode()
        )
    if location == "without-source":
        source = None
    metadata = identity_metadata(
        ">=3.12,<3.13" if location == "source" else requires_python
    )
    with pytest.raises(release.ReleaseError, match="Requires-Python|identity"):
        release._identity(metadata, source)


@pytest.mark.parametrize("change", [None, "missing", "extra", "changed", "link"])
def test_sdist_exact_candidates_excluding_pkg_info(release, tmp_path, change):
    source = source_fixture()
    files = {**source, "PKG-INFO": b"Metadata-Version: 2.3\nName: production-scheduling-agent\n"
             b"Version: 0.1.0\nRequires-Python: >=3.12,<3.13\n"}
    if change == "missing":
        del files["scripts/check_release.py"]
    elif change == "extra":
        files["docs/extra.md"] = b"not a source candidate"
    elif change == "changed":
        files["README.md"] += b"changed"
    path = tmp_path.resolve() / "production_scheduling_agent-0.1.0.tar.gz"
    write_sdist(path, files, link=change == "link")
    if change:
        with pytest.raises(release.ReleaseError):
            release.inspect_sdist(path, source)
    else:
        assert release.inspect_sdist(path, source) == files
    assert not (tmp_path / "production_scheduling_agent-0.1.0").exists()


@pytest.mark.parametrize("kind", ["zip-comment", "zip-extra", "tar-pax"])
def test_archive_hidden_metadata_rejected(release, tmp_path, kind):
    if kind == "tar-pax":
        path = tmp_path.resolve() / "production_scheduling_agent-0.1.0.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("production_scheduling_agent-0.1.0/README.md")
            info.pax_headers = {"comment": "/" + "Users/fixture/private.json"}
            archive.addfile(info, io.BytesIO())
    else:
        path = tmp_path.resolve() / "fixture.whl"
        with zipfile.ZipFile(path, "w") as archive:
            info = zipfile.ZipInfo("scheduling_agent/__init__.py")
            if kind == "zip-extra":
                info.extra = b"\x01\x00\x00\x00"
            else:
                archive.comment = b"unreviewed comment"
            archive.writestr(info, b"")
    with pytest.raises(release.ReleaseError):
        release.read_archive(path)


def test_archive_bounds_complete_gzip_stream(release, tmp_path, monkeypatch):
    path = tmp_path.resolve() / "production_scheduling_agent-0.1.0.tar.gz"
    path.write_bytes(gzip.compress(b"x" * 4096))
    monkeypatch.setattr(release, "MAX_TOTAL_BYTES", 1024)
    with pytest.raises(release.ReleaseError, match="expanded-stream"):
        release.read_archive(path)


def test_sdist_root_must_match_filename(release, tmp_path):
    path = tmp_path.resolve() / "production_scheduling_agent-0.2.0.tar.gz"
    write_sdist(path, source_fixture())
    with pytest.raises(release.ReleaseError, match="sdist root"):
        release.read_archive(path)


def test_candidate_count_total_bytes_and_large_file_before_open(release, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / "README.md").write_bytes(b"x" * 64)
    mock_git_candidates(release, monkeypatch, root, ["README.md"])
    monkeypatch.setattr(release, "MAX_FILES", 0)
    with pytest.raises(release.ReleaseError, match="file-count-limit"):
        release.source_candidates(root)
    monkeypatch.setattr(release, "MAX_FILES", 2000)
    monkeypatch.setattr(release, "MAX_TOTAL_BYTES", 32)
    monkeypatch.setattr(release, "read_regular", lambda *args: pytest.fail("oversize file opened"))
    files, issues = release.source_candidates(root)
    assert not files and any("total-size-limit" in issue for issue in issues)


def test_release_stops_before_archives_on_source_failure(release, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(release, "source_candidates", lambda _: ({}, ["fixture blocked"]))
    monkeypatch.setattr(release, "explicit_archive", lambda *_: pytest.fail("archive read"))
    assert release.main(["--root", str(tmp_path.resolve()), "--archive", "dist/fixture.whl"]) == 1
    assert "archives not read" in capsys.readouterr().out


def test_dynamic_candidates_include_new_public_paths_without_fixed_count(
        release, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / "README.md").write_text("original public fixture")
    names = ["README.md"]
    mock_git_candidates(release, monkeypatch, root, names)
    first, issues = release.source_candidates(root)
    assert first == {"README.md": b"original public fixture"} and not issues
    additions = {"src/scheduling_agent/new_module.py": b"# original public module\n",
                 "docs/new-contract.md": b"original public contract\n",
                 "tests/test_new_module.py": b"# original public test\n"}
    for name, content in additions.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    names.extend(additions)
    second, issues = release.source_candidates(root)
    assert second == {**first, **additions} and not issues
    (root / "docs/new-contract.md").unlink()
    names.remove("docs/new-contract.md")
    third, issues = release.source_candidates(root)
    assert third == {name: data for name, data in second.items() if name != "docs/new-contract.md"}
    assert not issues


def test_secret_candidate_name_is_never_opened_or_printed(release, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    name = "docs/" + "sk-" + "a" * 32 + ".md"
    (root / "docs").mkdir()
    (root / name).write_text("never read")
    mock_git_candidates(release, monkeypatch, root, [name])
    monkeypatch.setattr(release, "read_regular", lambda *_: pytest.fail("candidate opened"))
    files, issues = release.source_candidates(root)
    assert not files and issues == ["candidate-path: service-token"]
    assert all(name not in issue for issue in issues)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "device", "sparse"])
def test_tar_rejects_nonregular_members_without_extraction(release, tmp_path, kind):
    path = tmp_path.resolve() / "production_scheduling_agent-0.1.0.tar.gz"
    info = tarfile.TarInfo("production_scheduling_agent-0.1.0/README.md")
    info.type = {"symlink": tarfile.SYMTYPE, "fifo": tarfile.FIFOTYPE,
                 "device": tarfile.CHRTYPE, "sparse": tarfile.GNUTYPE_SPARSE}[kind]
    if kind == "symlink":
        info.linkname = "../not-read"
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(info)
    with pytest.raises(release.ReleaseError, match="links/devices/sparse"):
        release.read_archive(path)
    assert not (tmp_path / "production_scheduling_agent-0.1.0").exists()


@pytest.mark.parametrize("kind", ["outside", "nested", "traversal", "parent-link", "link"])
def test_explicit_archives_stay_directly_under_safe_dist(release, tmp_path, kind):
    root = tmp_path.resolve() / "project"
    root.mkdir()
    real = tmp_path.resolve() / "archives"
    real.mkdir()
    (real / "reviewed.whl").write_bytes(b"generated fixture")
    if kind == "parent-link":
        (root / "dist").symlink_to(real, target_is_directory=True)
        supplied = Path("dist/reviewed.whl")
    else:
        (root / "dist").mkdir()
        if kind == "link":
            (root / "dist/reviewed.whl").symlink_to(real / "reviewed.whl")
        supplied = {"outside": real / "reviewed.whl", "nested": Path("dist/nested/reviewed.whl"),
                    "traversal": Path("dist/../reviewed.whl"),
                    "link": Path("dist/reviewed.whl")}[kind]
    with pytest.raises(release.ReleaseError):
        release.explicit_archive(root, supplied)


def test_file_byte_limit_is_checked_before_open(release, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / "README.md").write_bytes(b"larger than limit")
    monkeypatch.setattr(release.os, "open", lambda *a, **kw: pytest.fail("oversize file opened"))
    with pytest.raises(release.ReleaseError, match="file-size-limit"):
        release.read_regular(root, "README.md", 1)
