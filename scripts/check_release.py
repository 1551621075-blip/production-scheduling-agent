"""Bounded public-candidate and artifact gate; never discover private runtime files.

This is a heuristic, not a complete secret detector, provenance or license audit.
All candidates, including this script and rejection tests, follow identical rules.
No suppression, build, extraction, network access or model execution is provided.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import csv
import gzip
import hashlib
import html
import io
import os
import re
import stat
import subprocess
import tarfile
import tomllib
import zipfile
import zlib
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 2000
ROOT_FILES = {".gitignore", "README.md", "THIRD_PARTY_NOTICES.md", "pyproject.toml", "uv.lock"}
# Planned R6 minimum, not a frozen/exhaustive manifest. Source parity covers additions;
# the installation probe imports every reviewed top-level module, including new ones.
MODULES = {
    "__init__", "errors", "domain", "fixtures", "compiler", "solver", "validator",
    "simulation", "storage", "service", "actions", "agent", "provider", "managed_provider",
    "receiver", "schemas", "api", "cli",
}
RESOURCES = {"templates/index.html", "static/app.js", "static/style.css"}
METADATA_FILES = {"METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt"}
DISTRIBUTION = "production-scheduling-agent"
ENTRY_POINT = "scheduling_agent.cli:main"
FORBIDDEN_PARTS = {
    ".git", ".venv", "venv", "runtime", "models", ".scheduling-data", "__pycache__",
    ".pytest_cache", ".ruff_cache", "node_modules", "private", "secrets", ".ssh",
    ".aws", ".config", "browser", "profiles", "dist", "build",
}
FORBIDDEN_SUFFIXES = {
    ".gguf", ".safetensors", ".bin", ".onnx", ".pt", ".pth", ".pkl", ".pickle",
    ".db", ".log", ".pem", ".key", ".dylib", ".so", ".dll", ".exe", ".pyc", ".jar",
}
RULES = {
    "personal-path": re.compile(
        r"(?:file://)?/(?:Users|home)/[^\s\"'<>`)]+|/(?:root)/[^\s\"'<>`)]+"
        r"|[A-Za-z]:[\\/]+Users[\\/]+[^\s\"'<>`)]+|[~]/[^\s\"'<>`)]+", re.I
    ),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "service-token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
        r"|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,})\b"
    ),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    "bearer-literal": re.compile(r"\bBearer\s+[A-Za-z0-9_+./=-]{20,}", re.I),
    "credential-url": re.compile(r"https?://[^\s/\"'<>]+@[^\s\"'<>),]+", re.I),
    "credential-query": re.compile(
        r"[?&](?:token|access_token|api_key|password|secret)=[^\s\"'<>),&]+", re.I
    ),
    "literal-credential": re.compile(
        r"[\"']?(?:api[_-]?key|access[_-]?token|password|secret|token)[\"']?"
        r"\s*[:=]\s*[\"'][A-Za-z0-9_+./=-]{16,}[\"']", re.I
    ),
    "external-cdn": re.compile(
        r"\b(?:cdn\.jsdelivr\.net|unpkg\.com|cdnjs\.cloudflare\.com|"
        r"fonts\.(?:googleapis|gstatic)\.com|esm\.sh)\b", re.I
    ),
    "external-web-resource": re.compile(
        r"<(?:script|link|img|iframe|source|audio|video)\b[^>]*\b(?:src|href)"
        r"\s*=\s*[\"']?\s*(?:https?:)?//"
        r"|\b(?:fetch|import|importScripts|WebSocket|EventSource)\s*\(\s*[\"'](?:https?:)?//"
        r"|\b(?:import|from)\s*[\"'](?:https?:)?//"
        r"|(?:@import\s*|url\(\s*)[\"']?(?:https?:)?//", re.I
    ),
}


class ReleaseError(ValueError):
    """A sanitized release gate failure; never include a matched payload."""


def safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or "\\" in name or ":" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ReleaseError("unsafe relative member name")
    return path


def allowed_path(name: str, *, wheel: bool = False) -> bool:
    path = safe_name(name)
    parts = path.parts
    lower = {part.lower() for part in parts}
    if (lower & FORBIDDEN_PARTS or any(part.startswith(".env") for part in lower)
            or path.suffix.lower() in FORBIDDEN_SUFFIXES
            or any(".sqlite" in part for part in lower)):
        return False
    prefix = "scheduling_agent/" if wheel else "src/scheduling_agent/"
    if name.startswith(prefix):
        relative = name[len(prefix):]
        return (relative in RESOURCES
                or "/" not in relative and re.fullmatch(r"[a-z_][a-z0-9_]*\.py", relative)
                is not None)
    if wheel:
        return (len(parts) == 2 and re.fullmatch(
            r"production_scheduling_agent-[A-Za-z0-9_.!+]+\.dist-info", parts[0]
        ) is not None and parts[1] in METADATA_FILES)
    return (name in ROOT_FILES or len(parts) == 2 and (
        parts[0] == "docs" and re.fullmatch(r"[a-z0-9_-]+\.md", parts[1]) is not None
        or parts[0] == "scripts" and re.fullmatch(r"[a-z0-9_]+\.(?:py|mjs)", parts[1]) is not None
        or parts[0] == "tests" and re.fullmatch(r"(?:test_[a-z0-9_]+|conftest)\.py", parts[1])
        is not None
    ))


def scan_content(name: str, content: bytes) -> list[str]:
    if len(content) > MAX_FILE_BYTES:
        return [f"{name}: file-size-limit"]
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        return [f"{name}: non-text-payload"]
    if "\x00" in text:
        return [f"{name}: non-text-payload"]
    text = html.unescape(unquote(unquote(text)))
    return [f"{name}: {rule}" for rule, pattern in RULES.items() if pattern.search(text)]


def checked_root(root: Path) -> Path:
    root = root.absolute()
    if ".." in root.parts:
        raise ReleaseError("root traversal is forbidden")
    for parent in (*reversed(root.parents), root):
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ReleaseError("root and parents must be directories without symlinks")
    return root


def regular_file(root: Path, name: str) -> Path:
    path = checked_root(root)
    parts = safe_name(name).parts
    for index, part in enumerate(parts):
        path /= part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseError("symlink-not-allowed")
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise ReleaseError("non-directory-parent")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReleaseError("single-link regular-file-required")
    return path


def read_regular(root: Path, name: str, limit: int = MAX_FILE_BYTES) -> bytes:
    path = regular_file(root, name)
    before = path.lstat()
    if before.st_size > limit:
        raise ReleaseError("file-size-limit; not-read")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ReleaseError("file changed before reading")
        content = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    regular_file(root, name)
    if (len(content) > limit or len(content) != before.st_size
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or path.lstat() != after):
        raise ReleaseError("file changed while reading")
    return content


def _git(root: Path, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    # Ignore inherited GIT_DIR/worktree/index overrides. No submodules, hooks or content filters.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(root), *args], input=data, env=environment,
                          capture_output=True, check=False, timeout=30)


def source_candidates(root: Path) -> tuple[dict[str, bytes], list[str]]:
    root = checked_root(root)
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode or Path(top.stdout.decode().strip()) != root:
        raise ReleaseError("root must be the exact Git project root")
    listing = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if listing.returncode or len(listing.stdout) > MAX_FILE_BYTES:
        raise ReleaseError("cannot enumerate bounded public candidate paths")
    names = sorted({item.decode("utf-8") for item in listing.stdout.split(b"\0") if item})
    if not names or len(names) > MAX_FILES:
        raise ReleaseError("empty candidate list or file-count-limit")
    ignored_result = _git(root, "check-ignore", "--no-index", "--stdin", "-z",
                          data=b"\0".join(name.encode() for name in names) + b"\0")
    if ignored_result.returncode not in {0, 1}:
        raise ReleaseError("cannot establish ignored-file boundary")
    ignored = set(ignored_result.stdout.decode().split("\0"))
    files, issues, seen, total = {}, [], set(), 0
    for name in names:
        try:
            safe_name(name)
            path_findings = scan_content("candidate-path", name.encode())
            if path_findings:
                issues.extend(path_findings)
                continue
            if name.casefold() in seen:
                raise ReleaseError("case-colliding candidate")
            seen.add(name.casefold())
            if name in ignored:
                issues.append("ignored-but-tracked candidate; not-read")
                continue
            if not allowed_path(name):
                # Unapproved filenames can themselves contain private information.
                issues.append("candidate outside-public-allowlist; not-read")
                continue
            path = regular_file(root, name)
            total += path.lstat().st_size
            if total > MAX_TOTAL_BYTES:
                raise ReleaseError("total-size-limit; not-read")
            content = read_regular(root, name)
            files[name] = content
            issues.extend(scan_content(name, content))
        except ReleaseError as exc:
            issues.append(str(exc))
        except OSError:
            issues.append("candidate unreadable; no payload printed")
    return files, issues


def scan_tree(root: Path) -> tuple[int, list[str]]:
    files, issues = source_candidates(root)
    return len(files), issues


def _allowed_directory(name: str, wheel: bool) -> bool:
    directories = ({"scheduling_agent", "scheduling_agent/static", "scheduling_agent/templates"}
                   if wheel else {
                       "", "src", "src/scheduling_agent", "src/scheduling_agent/static",
                       "src/scheduling_agent/templates", "docs", "tests", "scripts",
                   })
    return name in directories or bool(wheel and re.fullmatch(
        r"production_scheduling_agent-[A-Za-z0-9_.!+]+\.dist-info", name
    ))


def read_archive(path: Path) -> dict[str, bytes]:
    """Read a bounded in-memory snapshot; never extract, follow links or execute anything."""
    path = path.absolute()
    root = checked_root(path.parent)
    content = read_regular(root, path.name, MAX_TOTAL_BYTES)
    wheel = path.name.endswith(".whl")
    if not wheel and not path.name.endswith(".tar.gz"):
        raise ReleaseError("only .whl and .tar.gz archives are supported")
    result, seen, total, roots = {}, set(), 0, set()
    buffer = io.BytesIO(content)
    if not wheel:
        # Bound the complete tar stream too, including ignored PAX headers/padding.
        with gzip.GzipFile(fileobj=buffer) as compressed:
            expanded = compressed.read(MAX_TOTAL_BYTES + 1)
        if len(expanded) > MAX_TOTAL_BYTES:
            raise ReleaseError("archive expanded-stream-size-limit")
        buffer = io.BytesIO(expanded)
    with (zipfile.ZipFile(buffer) if wheel else tarfile.open(fileobj=buffer, mode="r:")) as arc:
        if wheel and arc.comment:
            raise ReleaseError("unreviewed ZIP archive comment")
        for index, member in enumerate(arc.infolist() if wheel else arc):
            if index >= MAX_FILES:
                raise ReleaseError("archive file-count-limit")
            raw_name = member.filename if wheel else member.name
            directory = member.is_dir() if wheel else member.isdir()
            name = raw_name[:-1] if directory and raw_name.endswith("/") else raw_name
            parts = safe_name(name).parts
            if scan_content("archive-member-name", name.encode()):
                raise ReleaseError("archive member name failed content boundary")
            if name.casefold() in seen:
                raise ReleaseError("duplicate or case-colliding archive member")
            seen.add(name.casefold())
            if wheel:
                mode = stat.S_IFMT(member.external_attr >> 16)
                if mode not in {0, stat.S_IFDIR if directory else stat.S_IFREG}:
                    raise ReleaseError("archive non-regular member")
                if member.flag_bits & 1:
                    raise ReleaseError("encrypted archive member")
                if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ReleaseError("unsupported wheel compression")
                if member.extra or member.comment:
                    raise ReleaseError("unreviewed ZIP member metadata")
                size = member.file_size
            else:
                if not (member.isfile() or directory) or member.issparse():
                    raise ReleaseError("archive links/devices/sparse files are forbidden")
                # Scan ownership/PAX text as well; it is outside the normal file payload.
                metadata = {"uname": member.uname, "gname": member.gname, **member.pax_headers}
                for key, value in metadata.items():
                    if scan_content("archive-header", (key + "=" + value).encode()):
                        raise ReleaseError("archive metadata failed content boundary")
                size = member.size
                roots.add(parts[0])
                if (len(roots) != 1 or parts[0] != path.name.removesuffix(".tar.gz")
                        or not re.fullmatch(
                            r"production[-_]scheduling[-_]agent-[A-Za-z0-9_.!+]+", parts[0]
                        )):
                    raise ReleaseError("unexpected sdist root")
                name = "/".join(parts[1:])
            if directory:
                if size != 0 or not _allowed_directory(name, wheel):
                    raise ReleaseError("archive directory outside public allowlist")
                continue
            if not (name == "PKG-INFO" and not wheel) and not allowed_path(name, wheel=wheel):
                raise ReleaseError("archive member outside-public-allowlist; not-read")
            total += size
            if size < 0 or size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ReleaseError("archive uncompressed-size-limit")
            stream = arc.open(member) if wheel else arc.extractfile(member)
            if stream is None:
                raise ReleaseError("archive unreadable member")
            with stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) != size:
                raise ReleaseError("archive member size mismatch")
            findings = scan_content(name, data)
            if findings:
                raise ReleaseError("; ".join(findings))
            result[name] = data
    if not result:
        raise ReleaseError("empty archive")
    return result


def project_identity(source: dict[str, bytes]) -> tuple[str, str]:
    try:
        project = tomllib.loads(source["pyproject.toml"].decode())["project"]
        if (project["name"] != DISTRIBUTION
                or project["scripts"] != {"production-scheduling": ENTRY_POINT}):
            raise ValueError
        return project["version"], project["requires-python"]
    except (KeyError, ValueError, TypeError):
        raise ReleaseError("invalid source distribution identity") from None


def _requires_python_clauses(value: str) -> list[str]:
    if not isinstance(value, str):
        raise ReleaseError("invalid Requires-Python clauses")
    clauses = [clause.strip() for clause in value.split(",")]
    if not all(clauses):
        raise ReleaseError("invalid Requires-Python clauses")
    # Normalize only clause order and surrounding whitespace, not operators or bounds.
    return sorted(clauses)


def _identity(metadata, source: dict[str, bytes] | None) -> str:
    for key in ("Name", "Version", "Requires-Python"):
        if len(metadata.get_all(key, [])) != 1:
            raise ReleaseError("missing or duplicate distribution identity")
    if metadata["Name"] != DISTRIBUTION or not metadata["Requires-Python"]:
        raise ReleaseError("unexpected distribution identity")
    version = metadata["Version"]
    if not re.fullmatch(r"[A-Za-z0-9_.!+]+", version):
        raise ReleaseError("invalid distribution version")
    requires_python = _requires_python_clauses(metadata["Requires-Python"])
    if source is not None:
        source_version, source_python = project_identity(source)
        if version != source_version or requires_python != _requires_python_clauses(source_python):
            raise ReleaseError("artifact/source identity mismatch")
    return version


def inspect_wheel(path: Path, source: dict[str, bytes] | None = None) -> dict[str, bytes]:
    payload = read_archive(path)
    metadata_names = [name for name in payload if name.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        raise ReleaseError("exactly one metadata directory is required")
    directory = metadata_names[0].rsplit("/", 1)[0]
    version = _identity(BytesParser().parsebytes(payload[metadata_names[0]]), source)
    if directory != f"production_scheduling_agent-{version}.dist-info" or any(
        ".dist-info/" in name and not name.startswith(directory + "/") for name in payload
    ):
        raise ReleaseError("metadata directory identity mismatch")
    if path.name != f"production_scheduling_agent-{version}-py3-none-any.whl":
        raise ReleaseError("wheel filename must match the pure Python distribution identity")
    wheel = BytesParser().parsebytes(payload.get(directory + "/WHEEL", b""))
    if (wheel.get_all("Root-Is-Purelib") != ["true"]
            or wheel.get_all("Wheel-Version") != ["1.0"]
            or wheel.get_all("Tag") != ["py3-none-any"]):
        raise ReleaseError("only pure Python py3-none-any Wheel-Version 1.0 is supported")
    entries = configparser.ConfigParser(interpolation=None)
    entries.optionxform = str
    entries.read_string(payload.get(directory + "/entry_points.txt", b"").decode())
    if (entries.defaults() or entries.sections() != ["console_scripts"]
            or dict(entries["console_scripts"]) != {"production-scheduling": ENTRY_POINT}):
        raise ReleaseError("unexpected console entry point")
    if not {f"scheduling_agent/{module}.py" for module in MODULES} <= payload.keys():
        raise ReleaseError("wheel is missing a required production scheduling module")
    if any(not payload.get("scheduling_agent/" + resource) for resource in RESOURCES):
        raise ReleaseError("wheel is missing a template/JS/CSS resource")
    record_name = directory + "/RECORD"
    if record_name not in payload:
        raise ReleaseError("wheel RECORD is missing")
    records = {}
    for row in csv.reader(io.StringIO(payload[record_name].decode()), strict=True):
        if len(row) != 3 or row[0] in records:
            raise ReleaseError("malformed or duplicate RECORD row")
        safe_name(row[0])
        records[row[0]] = row[1:]
    if records.keys() != payload.keys() or records[record_name] != ["", ""]:
        raise ReleaseError("RECORD does not cover exactly the wheel payload")
    for name, data in payload.items():
        if name != record_name:
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
            if records[name] != ["sha256=" + digest, str(len(data))]:
                raise ReleaseError("RECORD hash/size mismatch")
    if source is not None:
        expected = {name.removeprefix("src/"): data for name, data in source.items()
                    if name.startswith("src/scheduling_agent/")}
        actual = {name: data for name, data in payload.items()
                  if name.startswith("scheduling_agent/")}
        if actual != expected:
            raise ReleaseError("wheel/source package parity mismatch")
    return payload


def inspect_sdist(path: Path, source: dict[str, bytes]) -> dict[str, bytes]:
    payload = read_archive(path)
    if "PKG-INFO" not in payload:
        raise ReleaseError("sdist PKG-INFO is missing")
    version = _identity(BytesParser().parsebytes(payload["PKG-INFO"]), source)
    if path.name not in {f"production_scheduling_agent-{version}.tar.gz",
                         f"production-scheduling-agent-{version}.tar.gz"}:
        raise ReleaseError("sdist filename identity mismatch")
    if {name: data for name, data in payload.items() if name != "PKG-INFO"} != source:
        raise ReleaseError("sdist must equal exact source candidates after removing PKG-INFO")
    return payload


def explicit_archive(root: Path, supplied: Path) -> Path:
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ReleaseError("archive must be directly under project dist") from None
    if len(relative.parts) != 2 or relative.parts[0] != "dist":
        raise ReleaseError("archive must be directly under project dist")
    return regular_file(root, relative.as_posix())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--archive", type=Path, action="append", default=[],
                        help="Explicit reviewed dist/*.whl or dist/*.tar.gz; never auto-discovered")
    args = parser.parse_args(argv)
    try:
        root = checked_root(args.root)
        source, issues = source_candidates(root)
        if issues:
            for issue in issues:
                print(issue)
            print(f"FAIL: {len(source)} public files read; archives not read")
            return 1
        for supplied in args.archive:
            path = explicit_archive(root, supplied)
            if path.name.endswith(".whl"):
                inspect_wheel(path, source)
            elif path.name.endswith(".tar.gz"):
                inspect_sdist(path, source)
            else:
                raise ReleaseError("unsupported archive type")
        print(f"PASS: {len(source)} public files; {len(args.archive)} explicit archives")
        print("Bounded heuristic only; not a complete secret/license audit or installation test.")
        return 0
    except (ReleaseError, OSError, ValueError, tarfile.TarError, zipfile.BadZipFile,
            subprocess.SubprocessError, configparser.Error, csv.Error, EOFError, zlib.error):
        print("FAIL: release boundary/structure/parity check failed; no payload printed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
