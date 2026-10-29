"""Explicit hash-verified generation-only CPU runtime; no ambient discovery or BGE loading."""
from __future__ import annotations

import fcntl
import hashlib
import os
import socket
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from pydantic import Field, ValidationError, model_validator

from .provider import (
    PROTOCOL,
    LocalProvider,
    ProviderError,
    ProviderReply,
    StrictModel,
    _json_object,
    _request_preflight,
    _source_digest,
    digest,
)

GENERATION_ALIAS = "qwen3-4b-instruct-2507"
STARTUP_TIMEOUT = 90.0
MAX_CONFIG_BYTES = 64_000
MAX_HEALTH_BYTES = 4096


class RuntimeFile(StrictModel):
    path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelFile(RuntimeFile):
    bytes: int = Field(gt=0, strict=True)


class RuntimeConfig(StrictModel):
    """Accept explicit generation-only or legacy three-role configuration.

    Extra legacy role descriptors are validated but never opened, hashed, launched or fingerprinted.
    No environment lookup, default path, credentials, downloads or recursive configuration.
    """

    binary: RuntimeFile
    libraries: list[RuntimeFile] = Field(min_length=1, max_length=30)
    models: dict[str, ModelFile]
    lock_path: str = Field(min_length=1, max_length=4096)
    port: int = Field(default=18080, ge=1024, le=65535, strict=True)

    @model_validator(mode="after")
    def runtime_contract(self):
        if set(self.models) not in ({"generation"}, {"generation", "embedding", "rerank"}):
            raise ValueError("Generation-only or exactly three legacy roles required")
        paths = [self.binary.path, self.lock_path, *(record.path for record in self.libraries),
                 *(record.path for record in self.models.values())]
        if any(not Path(path).is_absolute() or ".." in Path(path).parts
               or any(ord(char) < 32 or ord(char) == 127 for char in path) for path in paths):
            raise ValueError("Absolute runtime paths without traversal or controls required")
        if len({str(Path(path)) for path in paths}) != len(paths):
            raise ValueError("Runtime paths must be distinct")
        if any(Path(record.path).parent != Path(self.binary.path).parent
               for record in self.libraries):
            raise ValueError("Libraries must be beside the configured binary")
        return self


@dataclass
class _RuntimeSession:
    resources: ExitStack = field(default_factory=ExitStack)
    process: object | None = None
    closed: bool = False
    failed: bool = False
    owner: int | None = None


class ManagedLocalProvider(LocalProvider):
    """One lazy child per explicit session, or per standalone decide().

    session() must surround one graph invocation, never a persisted human pause. A thread lease,
    whole-decision lock and inherited OS lock exclude overlapping children. close() permanently
    closes the adapter. Cleanup uncertainty blocks new work; inference is never retried.
    """

    def __init__(self, config_path: Path):
        try:
            path = Path(config_path)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("Explicit absolute configuration path required")
            self._no_symlinks(path)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CONFIG_BYTES:
                    raise ValueError("Bounded regular configuration required")
                content = stream.read(MAX_CONFIG_BYTES + 1)
                if (len(content) > MAX_CONFIG_BYTES
                        or self._identity(before) != self._identity(path.stat())):
                    raise ValueError("Configuration changed or exceeds byte limit")
            self._no_symlinks(path)
            self.config = RuntimeConfig.model_validate(_json_object(content), strict=True)
        except (OSError, TypeError, ValueError, ValidationError, RecursionError):
            raise ProviderError("runtime_config", "Explicit local runtime configuration is "
                                "unreadable or invalid", sent=False) from None
        super().__init__(f"http://127.0.0.1:{self.config.port}", GENERATION_ALIAS)
        self.metadata = {
            **self.metadata, "provider": "managed-local-cpu",
            "protocol": PROTOCOL + "-managed-cpu-v1-graph-scope",
            "managed_source_sha256": _source_digest(__file__),
            "binary": self.config.binary.sha256,
            "libraries": sorted(record.sha256 for record in self.config.libraries),
            "model_sha256": self.config.models["generation"].sha256,
            "model_bytes": self.config.models["generation"].bytes,
            "context": 8192, "threads": 4, "seed": 42,
        }
        self._fingerprint = "managed-local-cpu-" + digest(self.metadata)
        self._verified: dict[str, tuple] = {}
        self._thread_lock = threading.Lock()
        self._scope_lock = threading.Lock()
        self._session: ContextVar[_RuntimeSession | None] = ContextVar("scheduling_runtime", default=None)
        self._active: _RuntimeSession | None = None
        self._cleanup_unknown = False
        self._closed = False

    def _ensure_open(self):
        if self._cleanup_unknown:
            raise ProviderError("provider_unknown", "Previous process cleanup requires operator "
                                "reconciliation; no new inference was sent", sent=False)
        if self._closed:
            raise ProviderError("provider_unavailable", "Local provider is closed", sent=False)

    @contextmanager
    def session(self):
        self._ensure_open()
        if not self._scope_lock.acquire(blocking=False):
            raise ProviderError("provider_unavailable", "Local generation graph scope is busy",
                                sent=False)
        state = _RuntimeSession()
        token = self._session.set(state)
        failure = None
        try:
            # Synchronize creation with close() without holding the decision lock across yield.
            with self._thread_lock:
                self._ensure_open()
                self._active = state
            yield self
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                # A copied executor context must finish its bounded decision before we reap.
                with self._thread_lock:
                    state.closed = True
                    self._close(state, failure)
            finally:
                self._active = None
                self._session.reset(token)
                self._scope_lock.release()

    def _close(self, state, failure=None):
        try:
            state.resources.close()
        except Exception:
            self._cleanup_unknown = True
            tokens, sent = (failure.tokens, failure.sent) if isinstance(failure, ProviderError) \
                else (0, False)
            # Already returned calls in an explicit session were billed individually; no double bill.
            raise ProviderError("provider_unknown", "Local process cleanup could not be confirmed; "
                                "operator reconciliation required", tokens=tokens, sent=sent) from None

    def close(self):
        """Idempotently release idle owned resources; never terminate an in-flight decision."""
        if not self._thread_lock.acquire(blocking=False):
            raise ProviderError("provider_unavailable", "Local generation decision is busy; "
                                "close after the current call finishes", sent=False)
        try:
            self._closed = True
            if self._active is not None:
                self._active.closed = True
                self._close(self._active)
        finally:
            self._thread_lock.release()

    def decide(self, context: dict) -> ProviderReply:
        self._ensure_open()
        if self._session.get() is None:
            reply = None
            try:
                with self.session():
                    reply = self.decide(context)
                return reply
            except ProviderError as exc:
                if reply is not None:
                    # Auto-scope reap failed before the caller received this successful reply.
                    raise ProviderError(exc.code, exc.message, tokens=reply.tokens, sent=True) from None
                raise
        state = self._session.get()
        if not self._thread_lock.acquire(blocking=False):
            raise ProviderError("provider_unavailable", "Local generation decision is busy", sent=False)
        try:
            self._ensure_open()
            if state.closed or state.failed:
                raise ProviderError("provider_unavailable", "Local generation scope is closed or failed",
                                    sent=False)
            state.owner = threading.get_ident()
            try:
                return super().decide(context)
            except BaseException as exc:
                state.failed = True
                self._close(state, exc)
                raise
            finally:
                state.owner = None
        finally:
            self._thread_lock.release()

    def _post(self, url: str, payload: dict) -> dict:
        # Preflight before any hashing, subprocess creation or health request.
        if (url != self.generation_url + "/v1/chat/completions" or type(payload) is not dict
                or payload.get("model") != GENERATION_ALIAS):
            raise ProviderError("runtime_request", "Request does not match configured generation "
                                "model and endpoint", sent=False)
        _request_preflight(url, payload)
        self._ensure_open()
        state = self._session.get()
        if (state is None or state.closed or state.failed
                or state.owner != threading.get_ident()):
            raise ProviderError("runtime_request", "Managed transport requires an active decide call",
                                sent=False)
        try:
            if state.process is None:
                self._start_runtime(state)
            else:
                if state.process.poll() is not None:
                    raise ProviderError("provider_unavailable", "Scoped model exited before request",
                                        sent=False)
                self._verify_files()
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("provider_unavailable", "Local runtime could not become ready; "
                                "inference was not sent", sent=False) from None
        try:
            return super()._post(url, payload)
        except ProviderError:
            raise
        except Exception:
            # Once dispatch is entered, an untyped exception cannot attest that usage is zero.
            raise ProviderError("provider_unknown", "Inference outcome unknown; no automatic retry") \
                from None

    @staticmethod
    def _identity(record: os.stat_result) -> tuple:
        return (record.st_dev, record.st_ino, record.st_size, record.st_mtime_ns, record.st_ctime_ns)

    @staticmethod
    def _no_symlinks(path: Path):
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("Symlinks are not runtime files or directories")

    def _verify(self, record: RuntimeFile):
        path = Path(record.path)
        try:
            self._no_symlinks(path)
            before = path.stat()
            identity = self._identity(before)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Regular runtime file required")
            if isinstance(record, ModelFile) and before.st_size != record.bytes:
                raise ValueError("Model byte size mismatch")
            if record == self.config.binary and not os.access(path, os.X_OK):
                raise ValueError("Runtime binary is not executable")
            if self._verified.get(record.path) == identity:
                return
            sha = hashlib.sha256()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if identity != self._identity(os.fstat(stream.fileno())):
                    raise ValueError("Runtime identity changed before hashing")
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    sha.update(block)
                if identity != self._identity(os.fstat(stream.fileno())):
                    raise ValueError("Runtime changed during hashing")
            if sha.hexdigest() != record.sha256 or identity != self._identity(path.stat()):
                raise ValueError("Runtime hash mismatch or file changed during hashing")
            self._no_symlinks(path)
            self._verified[record.path] = identity
        except (OSError, ValueError):
            raise ProviderError("runtime_integrity", "Local model or inference engine failed "
                                "file integrity verification", sent=False) from None

    def _verify_files(self):
        for record in [self.config.binary, *self.config.libraries, self.config.models["generation"]]:
            self._verify(record)

    def _command(self) -> list[str]:
        return [
            self.config.binary.path, "--model", self.config.models["generation"].path,
            "--alias", GENERATION_ALIAS, "--host", "127.0.0.1", "--port", str(self.config.port),
            "--parallel", "1", "--threads", "4", "--threads-batch", "4", "--threads-http", "2",
            "--n-gpu-layers", "0", "--device", "none", "--no-op-offload", "--flash-attn", "off",
            "--no-webui", "--no-slots", "--log-disable", "--ctx-size", "8192",
            "--batch-size", "512", "--ubatch-size", "128", "--jinja", "--temp", "0", "--seed", "42",
        ]

    def _open_lock(self):
        path = Path(self.config.lock_path)
        try:
            self._no_symlinks(path)
            if not path.parent.is_dir():
                raise ValueError("Lock parent directory required")
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Lock must be a singly linked regular file")
                self._no_symlinks(path)
                if self._identity(info) != self._identity(path.stat()):
                    raise ValueError("Lock identity changed")
                return os.fdopen(fd, "a+")
            except BaseException:
                os.close(fd)
                raise
        except (OSError, ValueError):
            raise ProviderError("runtime_config", "Local runtime lock is invalid or unavailable",
                                sent=False) from None

    def _wait_ready(self, process):
        deadline = time.monotonic() + STARTUP_TIMEOUT
        try:
            with httpx.Client(timeout=httpx.Timeout(2, connect=1), trust_env=False,
                              follow_redirects=False, headers={"Accept-Encoding": "identity"}) as client:
                while True:
                    if process.poll() is not None:
                        raise ProviderError("provider_unavailable", "Local model exited during loading; "
                                            "inference was not sent", sent=False)
                    if time.monotonic() >= deadline:
                        raise ProviderError("provider_unavailable", "Local model loading timed out; "
                                            "inference was not sent", sent=False)
                    try:
                        with client.stream("GET", self.generation_url + "/health") as response:
                            if response.status_code not in {200, 503}:
                                raise ProviderError("provider_unavailable", "Unexpected local model "
                                                    "health response", sent=False)
                            if response.headers.get("content-encoding", "identity").lower() != "identity":
                                raise ProviderError("provider_unavailable", "Encoded health response "
                                                    "rejected", sent=False)
                            content = bytearray()
                            for chunk in response.iter_bytes():
                                if len(content) + len(chunk) > MAX_HEALTH_BYTES:
                                    raise ProviderError("provider_unavailable", "Local health response "
                                                        "exceeded byte limit", sent=False)
                                if time.monotonic() >= deadline:
                                    raise ProviderError("provider_unavailable", "Local model loading "
                                                        "timed out", sent=False)
                                content.extend(chunk)
                            if time.monotonic() >= deadline:
                                raise ProviderError("provider_unavailable", "Local model loading "
                                                    "timed out", sent=False)
                            if response.status_code == 200 and _json_object(content).get("status") == "ok":
                                if process.poll() is not None:
                                    raise ProviderError("provider_unavailable", "Model process exited "
                                                        "before inference", sent=False)
                                return
                    except (httpx.HTTPError, ValueError, TypeError, RecursionError):
                        pass
                    time.sleep(0.1)
        except httpx.HTTPError:
            raise ProviderError("provider_unavailable", "Local model startup transport failed; "
                                "inference was not sent", sent=False) from None

    @staticmethod
    def _stop(process):
        try:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=10)
            else:
                process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise ProviderError("provider_unknown", "Local process cleanup could not be confirmed; "
                                "operator reconciliation required") from None

    def _start_runtime(self, state: _RuntimeSession):
        lock = state.resources.enter_context(self._open_lock())
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProviderError("provider_unavailable", "Another local runtime holds the OS lock",
                                sent=False) from None
        self._verify_files()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.bind(("127.0.0.1", self.config.port))
        except OSError:
            raise ProviderError("provider_unavailable", "Configured port is occupied; refusing an "
                                "unknown server", sent=False) from None
        temporary = state.resources.enter_context(tempfile.TemporaryDirectory(prefix="scheduling-model-"))
        environment = {"HOME": temporary, "TMPDIR": temporary, "XDG_CACHE_HOME": temporary,
                       "PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        try:
            process = subprocess.Popen(
                self._command(), env=environment, cwd=temporary,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, pass_fds=(lock.fileno(),),
            )
        except OSError:
            raise ProviderError("provider_unavailable", "Cannot start local inference process",
                                sent=False) from None
        state.process = process
        state.resources.callback(self._stop, process)
        self._wait_ready(process)
