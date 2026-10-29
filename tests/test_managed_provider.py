"""Managed tests use temporary inert text artifacts and fake processes, never a real runtime."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from scheduling_agent import managed_provider as module
from scheduling_agent import provider as provider_module
from scheduling_agent.managed_provider import (
    GENERATION_ALIAS,
    ManagedLocalProvider,
    ModelFile,
    RuntimeConfig,
)
from scheduling_agent.provider import LocalProvider, ProviderError, digest


@pytest.fixture(autouse=True)
def forbid_real_runtime(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Tests may not launch processes, bind sockets or contact real HTTP")

    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(module.socket, "socket", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)


@pytest.fixture
def runtime(tmp_path):
    # /var is a system symlink on macOS; fixture artifacts use their canonical temporary path.
    root = tmp_path.resolve()
    binary, library, generation = (root / name for name in ("llama-server", "libllama", "model.gguf"))
    binary.write_bytes(b"TEST inert executable placeholder; never executed")
    binary.chmod(0o700)
    library.write_bytes(b"TEST inert library placeholder")
    generation.write_bytes(b"TEST inert model placeholder, not weights")

    def record(path, model=False):
        data = path.read_bytes()
        value = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}
        if model:
            value["bytes"] = len(data)
        return value

    config = {"binary": record(binary), "libraries": [record(library)],
              "models": {"generation": record(generation, True)},
              "lock_path": str(root / "runtime.lock"), "port": 18080}
    path = root / "TEST-config.json"
    path.write_text(json.dumps(config))
    return SimpleNamespace(root=root, config=config, path=path, binary=binary,
                           library=library, generation=generation)


def save(runtime, data):
    runtime.path.write_text(json.dumps(data))
    return ManagedLocalProvider(runtime.path)


def context():
    return {"task_id": "synthetic-task", "context_hash": "a" * 64, "evidence": {},
            "available_tools": ["get_snapshot"]}


def model_result(tokens=4000, tool="get_snapshot"):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
        "tool": tool, "arguments": {},
    })}}], "usage": {"total_tokens": tokens, "prompt_tokens": 3900, "completion_tokens": 100,
                     "prompt_tokens_details": {"cached_tokens": 3800}}}


class FakeProcess:
    def __init__(self, running=True, stubborn=False):
        self.running = running
        self.stubborn = stubborn
        self.terminated = 0
        self.killed = 0
        self.waits = []

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated += 1
        if not self.stubborn:
            self.running = False

    def kill(self):
        self.killed += 1
        self.running = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.running:
            raise subprocess.TimeoutExpired("TEST fake process", timeout)
        return 0


class FakeSocket:
    def __init__(self, occupied=False):
        self.occupied = occupied
        self.bound = []
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def settimeout(self, value):
        self.timeout = value

    def bind(self, address):
        self.bound.append(address)
        if self.occupied:
            raise OSError("TEST port occupied")


def mocked_runtime(monkeypatch, client, process=None, result=None, failure=None):
    process = process or FakeProcess()
    starts, posts, ready = [], [], []
    sock = FakeSocket()
    monkeypatch.setattr(module.socket, "socket", lambda *_: sock)

    def popen(command, **kwargs):
        starts.append((command, kwargs))
        assert os.fstat(kwargs["pass_fds"][0]).st_nlink == 1
        return process

    def post(url, payload):
        posts.append((url, payload))
        if failure is not None:
            raise failure
        return model_result() if result is None else result

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(client, "_wait_ready", lambda child: ready.append(child))
    monkeypatch.setattr(LocalProvider, "_post", staticmethod(post))
    return SimpleNamespace(process=process, starts=starts, posts=posts, ready=ready, socket=sock)


def fake_http(monkeypatch, handler):
    original = httpx.Client
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(module.httpx, "Client", client)
    return options


@pytest.mark.parametrize("tool", ["request_human_input", "submit_schedule"])
def test_managed_uses_authoritative_indexed_fields_and_candidate_wrapper(runtime, monkeypatch, tool):
    from scheduling_agent.domain import ObjectiveBreakdown, Plan, missing_inputs
    from scheduling_agent.fixtures import demo_snapshots

    snapshot = demo_snapshots(1_800_000_000_000)["missing"]
    fields = list(missing_inputs(snapshot))
    issued = {**context(), "available_tools": [tool], "required_input": fields}
    if tool == "request_human_input":
        arguments = {"question": "TEST 请补充缺失字段", "fields": fields[::-1]}
    else:
        plan = Plan(plan_id="TEST-plan", snapshot_hash="a" * 64, problem_hash="b" * 64,
                    objective_hash="c" * 64, slots=(), objective=ObjectiveBreakdown(
                        weighted_tardiness=0, setup_minutes=0, makespan=0, load_spread=0, total=0))
        issued["candidate"] = {"plan_hash": plan.plan_hash, "plan": plan.model_dump(mode="json")}
        issued["required_input"] = []
        issued["evidence"] = {name: {"evidence_id": f"ev-{name}", "tool": name,
            "payload": {}, "hash": digest({})} for name in provider_module.NO_ARGUMENT_TOOLS}
        arguments = {"candidate_hash": plan.plan_hash, "evidence_ids": [
            item["evidence_id"] for item in issued["evidence"].values()], "explanation": "TEST 人工审核"}
    result = model_result(tool=tool)
    result["choices"][0]["message"]["content"] = json.dumps({"tool": tool, "arguments": arguments})
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client, result=result)
    reply = client.decide(issued)
    assert reply.action == {"tool": tool, "arguments": arguments} and reply.tokens == 4000
    assert len(simulated.starts) == len(simulated.posts) == 1 and simulated.process.terminated == 1
    sent = json.loads(simulated.posts[0][1]["messages"][1]["content"])
    if tool == "request_human_input":
        assert sent["required_input"] == ["materials[0].initial_stock", "orders[0].due_min"]
    else:
        assert sent["candidate"]["plan_hash"] == arguments["candidate_hash"]
        assert sent["candidate"]["plan"]["plan_id"] == "TEST-plan"


@pytest.mark.parametrize("field", [
    "materials.a.initial_stock", "orders.a.due_min", "materials[20].initial_stock", "orders[01].due_min",
])
def test_managed_rejects_id_paths_and_noncanonical_indices_before_runtime(runtime, monkeypatch, field):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with pytest.raises(ProviderError) as error:
        client.decide({**context(), "available_tools": ["request_human_input"], "required_input": [field]})
    assert (error.value.code, error.value.tokens, error.value.sent) == ("model_context", 0, False)
    assert not simulated.starts and not simulated.posts and not client._verified


def test_construction_reads_only_explicit_fixture_config_and_source(runtime, monkeypatch):
    opened = []
    original_open, original_os_open = Path.open, os.open

    def path_open(path, *args, **kwargs):
        opened.append(Path(path))
        return original_open(path, *args, **kwargs)

    def os_open(path, *args, **kwargs):
        opened.append(Path(path))
        return original_os_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(os, "open", os_open)
    client = ManagedLocalProvider(runtime.path)
    assert set(opened) == {runtime.path, Path(module.__file__), Path(provider_module.__file__)}
    assert not client._verified and client.generation_model == GENERATION_ALIAS
    assert not client.is_test_provider
    client.close()


def test_legacy_role_descriptors_never_opened_or_fingerprinted(runtime, monkeypatch):
    first = ManagedLocalProvider(runtime.path)
    data = copy.deepcopy(runtime.config)
    for role in ("embedding", "rerank"):
        data["models"][role] = {"path": str(runtime.root / f"absent-{role}.gguf"),
                                "sha256": "b" * 64, "bytes": 99}
    client = save(runtime, data)
    assert client.fingerprint == first.fingerprint
    simulated = mocked_runtime(monkeypatch, client)
    assert client.decide(context()).tokens == 4000
    assert len(simulated.starts) == len(simulated.posts) == 1
    command = simulated.starts[0][0]
    assert "--embedding" not in command and "--rerank" not in command
    assert set(client._verified) == {str(runtime.binary), str(runtime.library), str(runtime.generation)}


@pytest.mark.parametrize("change", ["extra", "credentials", "roles", "partial-legacy", "relative",
    "traversal", "control", "duplicate", "library-dir", "hash", "bytes-bool", "bytes-zero",
    "port-bool", "port-small", "port-large", "no-libraries"])
def test_config_rejects_extra_credentialed_ambiguous_or_unsafe_fields(runtime, change):
    data = copy.deepcopy(runtime.config)
    if change in {"extra", "credentials"}:
        data["cloud_fallback" if change == "extra" else "api_key"] = "TEST-SENSITIVE"
    elif change == "roles":
        data["models"] = {"cloud": data["models"]["generation"]}
    elif change == "partial-legacy":
        data["models"]["embedding"] = {"path": str(runtime.root / "embed"),
                                        "sha256": "a" * 64, "bytes": 3}
    elif change == "relative":
        data["binary"]["path"] = "llama-server"
    elif change == "traversal":
        data["binary"]["path"] = str(runtime.root / ".." / "llama-server")
    elif change == "control":
        data["lock_path"] += "\n"
    elif change == "duplicate":
        data["lock_path"] = data["binary"]["path"]
    elif change == "library-dir":
        data["libraries"][0]["path"] = str(runtime.root / "other" / "lib")
    elif change == "hash":
        data["binary"]["sha256"] = "A" * 64
    elif change in {"bytes-bool", "bytes-zero"}:
        data["models"]["generation"]["bytes"] = True if change == "bytes-bool" else 0
    elif change.startswith("port-"):
        data["port"] = {"port-bool": True, "port-small": 80, "port-large": 65536}[change]
    elif change == "no-libraries":
        data["libraries"] = []
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(data)
    with pytest.raises(ProviderError) as error:
        save(runtime, data)
    assert (error.value.code, error.value.tokens, error.value.sent) == ("runtime_config", 0, False)
    assert "TEST-SENSITIVE" not in str(error.value)


@pytest.mark.parametrize("content", ["{}", "[]", "{bad", "x" * 64001,
                                    '{"binary":{},"binary":{}}'])
def test_invalid_or_oversize_explicit_config(runtime, content):
    runtime.path.write_text(content)
    with pytest.raises(ProviderError) as error:
        ManagedLocalProvider(runtime.path)
    assert error.value.code == "runtime_config" and not error.value.sent


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "missing", "relative", "utf16"])
def test_config_never_follows_links_or_discovers_defaults(runtime, kind):
    if kind == "relative":
        path = Path("nonexistent-TEST-runtime.json")
    elif kind == "missing":
        path = runtime.root / "absent.json"
    else:
        path = runtime.path
        path.unlink()
        if kind == "symlink":
            path.symlink_to(runtime.generation)
        elif kind == "directory":
            path.mkdir()
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            path.write_bytes(json.dumps(runtime.config).encode("utf-16"))
    with pytest.raises(ProviderError) as error:
        ManagedLocalProvider(path)
    assert error.value.code == "runtime_config" and not error.value.sent


def test_fingerprint_engine_and_model_identity_not_sensitive_paths(runtime):
    first = ManagedLocalProvider(runtime.path)
    assert first.fingerprint == "managed-local-cpu-" + digest(first.metadata)
    assert first.metadata["managed_source_sha256"] == hashlib.sha256(
        Path(module.__file__).read_bytes()).hexdigest()
    assert str(runtime.root) not in json.dumps(first.metadata)
    assert first.fingerprint == ManagedLocalProvider(runtime.path).fingerprint
    for role in ("binary", "model", "library"):
        data = copy.deepcopy(runtime.config)
        record = (data["models"]["generation"] if role == "model" else
                  data["libraries"][0] if role == "library" else data["binary"])
        record["sha256"] = "b" * 64
        assert save(runtime, data).fingerprint != first.fingerprint
    data = copy.deepcopy(runtime.config)
    data["models"]["generation"]["path"] = str(runtime.root / "relocated.gguf")
    assert save(runtime, data).fingerprint == first.fingerprint


def test_lazy_scope_reuses_child_and_reaps_at_graph_end(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with client.session():
        assert not simulated.starts and not client._verified
        replies = [client.decide(context()), client.decide(context())]
        assert len(simulated.starts) == len(simulated.ready) == 1
        assert len(simulated.posts) == 2 and sum(reply.tokens for reply in replies) == 8000
        assert simulated.process.terminated == 0
        assert Path(simulated.starts[0][1]["cwd"]).exists()
    assert simulated.process.terminated == 1 and simulated.process.waits == [10]
    assert not Path(simulated.starts[0][1]["cwd"]).exists()
    assert not client._scope_lock.locked() and client._session.get() is None
    assert client._active is None


def test_empty_scope_never_hashes_or_launches(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with client.session():
        pass
    assert not simulated.starts and not simulated.posts and not client._verified


def test_auto_scope_reaps_each_call_and_restarts_only_on_explicit_call(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    for _ in range(2):
        simulated.process.running = True
        assert client.decide(context()).tokens == 4000
        assert client._session.get() is None and not client._scope_lock.locked()
        assert not simulated.process.running
    assert len(simulated.starts) == len(simulated.posts) == 2
    assert simulated.process.terminated == 2


@pytest.mark.parametrize("failure", [RuntimeError("TEST graph ends"), KeyboardInterrupt()])
def test_scope_reaps_on_graph_error_or_cancellation(runtime, monkeypatch, failure):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with pytest.raises(type(failure)), client.session():
        client.decide(context())
        raise failure
    assert simulated.process.terminated == 1 and not client._scope_lock.locked()


def test_close_inside_scope_releases_resources_and_is_permanent_idempotent(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with client.session():
        client.decide(context())
        client.close()
        assert simulated.process.terminated == 1
        with pytest.raises(ProviderError) as error:
            client.decide(context())
        assert error.value.code == "provider_unavailable" and not error.value.sent
        client.close()
    client.close()
    assert simulated.process.terminated == 1
    with pytest.raises(ProviderError), client.session():
        pytest.fail("Closed provider must not acquire a new session")
    assert len(simulated.starts) == len(simulated.posts) == 1


def test_close_before_first_call_prevents_runtime_work(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    client.close()
    with pytest.raises(ProviderError) as error:
        client.decide(context())
    assert not error.value.sent and not simulated.starts and not client._verified


def test_copied_context_threads_share_only_explicit_scope(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with client.session(), ThreadPoolExecutor(max_workers=1) as executor:
        for _ in range(2):
            assert executor.submit(copy_context().run, client.decide, context()).result().tokens == 4000
        with pytest.raises(ProviderError) as error:
            executor.submit(client.decide, context()).result()
        assert error.value.code == "provider_unavailable" and not error.value.sent
    assert len(simulated.starts) == 1 and len(simulated.posts) == 2
    assert simulated.process.terminated == 1


def test_nested_and_stale_context_cannot_start_extra_child(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with client.session():
        with pytest.raises(ProviderError), client.session():
            pytest.fail("Nested session cannot enter")
        stale = copy_context()
        client.decide(context())
    with pytest.raises(ProviderError) as error:
        stale.run(client.decide, context())
    assert error.value.code == "provider_unavailable" and not error.value.sent
    assert len(simulated.starts) == 1


def test_reentrant_busy_call_or_close_does_not_reap_current_request(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)

    def post(_url, _payload):
        for operation in (lambda: client.decide(context()), client.close):
            with pytest.raises(ProviderError) as error:
                operation()
            assert error.value.code == "provider_unavailable" and not error.value.sent
            assert simulated.process.terminated == 0 and simulated.process.running
        return model_result()

    monkeypatch.setattr(LocalProvider, "_post", staticmethod(post))
    with client.session():
        assert client.decide(context()).tokens == 4000
        assert not client._session.get().failed
    assert simulated.process.terminated == 1 and len(simulated.starts) == 1


def test_whole_decision_lock_includes_parsing_and_action_validation(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    validating, release = threading.Event(), threading.Event()
    original = provider_module._validate_action

    def validate(value):
        validating.set()
        assert release.wait(timeout=5), "TEST validation barrier timed out"
        return original(value)

    monkeypatch.setattr(provider_module, "_validate_action", validate)
    with client.session(), ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(copy_context().run, client.decide, context())
        try:
            assert validating.wait(timeout=5)
            with pytest.raises(ProviderError) as error:
                client.decide(context())
            assert error.value.code == "provider_unavailable" and not error.value.sent
            with pytest.raises(ProviderError):
                client.close()
            assert simulated.process.terminated == 0
        finally:
            release.set()
        assert future.result(timeout=5).tokens == 4000
    assert simulated.process.terminated == 1 and len(simulated.posts) == 1


def test_failed_scope_immediate_reap_no_retry(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    failure = ProviderError("provider_unknown", "TEST response lost")
    simulated = mocked_runtime(monkeypatch, client, failure=failure)
    with client.session():
        with pytest.raises(ProviderError) as error:
            client.decide(context())
        assert error.value is failure and simulated.process.terminated == 1
        with pytest.raises(ProviderError) as closed:
            client.decide(context())
        assert closed.value.code == "provider_unavailable" and not closed.value.sent
    assert len(simulated.starts) == len(simulated.posts) == 1


def test_dead_scoped_child_never_restarts_implicitly(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with client.session():
        client.decide(context())
        simulated.process.running = False
        with pytest.raises(ProviderError) as error:
            client.decide(context())
        assert error.value.code == "provider_unavailable" and not error.value.sent
    assert len(simulated.starts) == len(simulated.posts) == 1


def test_integrity_checked_between_calls_and_reaps_on_change(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with client.session():
        client.decide(context())
        runtime.library.write_bytes(b"TEST library changed while resident")
        with pytest.raises(ProviderError) as error:
            client.decide(context())
        assert error.value.code == "runtime_integrity" and not error.value.sent
        assert simulated.process.terminated == 1
    assert len(simulated.posts) == 1


def test_os_lock_retained_between_scoped_calls(runtime, monkeypatch):
    first, second = ManagedLocalProvider(runtime.path), ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, first)
    with first.session():
        first.decide(context())
        with pytest.raises(ProviderError) as error:
            second.decide(context())
        assert error.value.code == "provider_unavailable" and not error.value.sent
    assert len(simulated.starts) == len(simulated.posts) == 1


def test_cpu_command_minimal_environment_no_shell_or_credentials(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    client.decide(context())
    command, options = simulated.starts[0]
    for flag, value in {"--model": str(runtime.generation), "--alias": GENERATION_ALIAS,
                        "--host": "127.0.0.1", "--parallel": "1", "--n-gpu-layers": "0",
                        "--device": "none", "--threads": "4", "--ctx-size": "8192",
                        "--temp": "0", "--seed": "42"}.items():
        assert command[command.index(flag) + 1] == value
    assert "--no-op-offload" in command and "--no-webui" in command
    assert "--embedding" not in command and "--rerank" not in command
    assert set(options["env"]) == {"HOME", "TMPDIR", "XDG_CACHE_HOME", "PATH", "LC_ALL"}
    assert options["env"]["PATH"] == "/usr/bin:/bin"
    assert options["env"]["HOME"] == options["env"]["TMPDIR"] == options["cwd"]
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["close_fds"] and len(options["pass_fds"]) == 1 and "shell" not in options
    assert simulated.socket.bound == [("127.0.0.1", 18080)]
    assert not Path(options["cwd"]).exists()


def test_verify_hash_size_and_identity_cache(runtime):
    client = ManagedLocalProvider(runtime.path)
    client._verify_files()
    assert len(client._verified) == 3
    client._verify_files()
    runtime.generation.write_bytes(b"Z" * runtime.generation.stat().st_size)
    with pytest.raises(ProviderError) as error:
        client._verify(client.config.models["generation"])
    assert error.value.code == "runtime_integrity" and not error.value.sent


@pytest.mark.parametrize("change", ["size", "hash", "symlink", "directory", "missing", "executable"])
def test_file_integrity_fails_before_subprocess(runtime, monkeypatch, change):
    client = ManagedLocalProvider(runtime.path)
    if change == "size":
        runtime.generation.write_bytes(b"short")
    elif change == "hash":
        runtime.library.write_bytes(b"TEST altered library")
    elif change == "symlink":
        target = runtime.root / "actual.gguf"
        runtime.generation.rename(target)
        runtime.generation.symlink_to(target)
    elif change == "directory":
        runtime.generation.unlink()
        runtime.generation.mkdir()
    elif change == "missing":
        runtime.generation.unlink()
    else:
        runtime.binary.chmod(0o600)
    simulated = mocked_runtime(monkeypatch, client)
    with pytest.raises(ProviderError) as error:
        client.decide(context())
    assert (error.value.code, error.value.tokens, error.value.sent) == ("runtime_integrity", 0, False)
    assert not simulated.starts and not simulated.posts and not client._thread_lock.locked()


def test_symlink_parent_cannot_bypass_verification(runtime):
    actual = runtime.root / "actual"
    actual.mkdir()
    target = actual / "model.gguf"
    target.write_bytes(b"TEST placeholder")
    link = runtime.root / "linked-directory"
    link.symlink_to(actual, target_is_directory=True)
    record = ModelFile(path=str(link / "model.gguf"),
                       sha256=hashlib.sha256(target.read_bytes()).hexdigest(), bytes=target.stat().st_size)
    with pytest.raises(ProviderError) as error:
        ManagedLocalProvider(runtime.path)._verify(record)
    assert error.value.code == "runtime_integrity"


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize(("tokens", "sent"), [(0, False), (0, True), (4000, True), (None, True)])
def test_typed_failure_metadata_survives_managed_cleanup(runtime, monkeypatch, scoped, tokens, sent):
    client = ManagedLocalProvider(runtime.path)
    failure = ProviderError("provider_unavailable", "TEST authoritative metadata", tokens=tokens, sent=sent)
    simulated = mocked_runtime(monkeypatch, client, failure=failure)
    with client.session() if scoped else nullcontext():
        with pytest.raises(ProviderError) as error:
            client.decide(context())
        assert error.value is failure and error.value.tokens == tokens and error.value.sent is sent
        assert simulated.process.terminated == 1
    assert len(simulated.starts) == len(simulated.posts) == 1


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize(("kind", "code", "tokens", "sent"), [
    ("http-rejected", "provider_http", 4000, True),
    ("http-ambiguous", "provider_unknown", 4000, True),
    ("http-no-usage", "provider_http", None, True),
    ("invalid-json", "provider_invalid", None, True),
    ("read-timeout", "provider_unknown", None, True),
    ("connect-error", "provider_unavailable", 0, False),
])
def test_mock_http_failure_billing_survives_reap(runtime, monkeypatch, scoped, kind, code, tokens, sent):
    client = ManagedLocalProvider(runtime.path)
    real_post = LocalProvider._post
    simulated = mocked_runtime(monkeypatch, client)
    requests, failures = [], []

    def handler(request):
        requests.append(request)
        if kind == "read-timeout":
            raise httpx.ReadTimeout("TEST lost reply", request=request)
        if kind == "connect-error":
            raise httpx.ConnectError("TEST not connected", request=request)
        if kind == "invalid-json":
            return httpx.Response(200, content=b"{broken")
        status = 500 if kind == "http-ambiguous" else 422
        body = {"error": "TEST failure"}
        if tokens is not None:
            body["usage"] = {"total_tokens": tokens}
        return httpx.Response(status, json=body)

    def capture(url, payload):
        try:
            return real_post(url, payload)
        except ProviderError as failure:
            failures.append(failure)
            raise

    fake_http(monkeypatch, handler)
    monkeypatch.setattr(LocalProvider, "_post", staticmethod(capture))
    with client.session() if scoped else nullcontext():
        with pytest.raises(ProviderError) as error:
            client.decide(context())
        assert len(failures) == 1 and error.value is failures[0]
        assert (error.value.code, error.value.tokens, error.value.sent) == (code, tokens, sent)
        assert simulated.process.terminated == 1
    assert len(requests) == 1 and requests[0].method == "POST"
    assert not client._thread_lock.locked() and not client._scope_lock.locked()


def test_untyped_dispatch_exception_cannot_claim_unsent_zero(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client, failure=ValueError("TEST-SENSITIVE"))
    with pytest.raises(ProviderError) as error:
        client.decide(context())
    assert error.value.code == "provider_unknown" and error.value.tokens is None and error.value.sent
    assert "TEST-SENSITIVE" not in str(error.value) and simulated.process.terminated == 1


def test_invalid_action_reaps_immediately_retaining_usage(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client, result=model_result(tool="invented"))
    with client.session():
        with pytest.raises(ProviderError) as error:
            client.decide(context())
        assert (error.value.code, error.value.tokens, error.value.sent) == ("provider_invalid", 4000, True)
        assert simulated.process.terminated == 1
        with pytest.raises(ProviderError) as closed:
            client.decide(context())
        assert closed.value.tokens == 0 and not closed.value.sent
    assert len(simulated.starts) == len(simulated.posts) == 1


def test_invalid_context_never_hashes_loads_or_dispatches(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with pytest.raises(ProviderError) as error:
        client.decide({**context(), "available_tools": ["run_sql"]})
    assert error.value.code == "model_context" and not error.value.sent
    assert not simulated.starts and not simulated.posts and not client._verified
    assert client._session.get() is None and not client._scope_lock.locked()


@pytest.mark.parametrize("failure_kind", ["typed", "invalid-action", "successful-reply"])
def test_reap_failure_keeps_known_billing_and_blocks_next_call(runtime, monkeypatch, failure_kind):
    client = ManagedLocalProvider(runtime.path)
    child = FakeProcess(stubborn=True)
    monkeypatch.setattr(child, "kill", lambda: None)
    result = model_result(tool="invented" if failure_kind == "invalid-action" else "get_snapshot")
    failure = ProviderError("provider_invalid", "TEST known usage", tokens=4000) \
        if failure_kind == "typed" else None
    simulated = mocked_runtime(monkeypatch, client, process=child, result=result, failure=failure)
    with pytest.raises(ProviderError) as error:
        client.decide(context())
    assert (error.value.code, error.value.tokens, error.value.sent) == ("provider_unknown", 4000, True)
    assert client._cleanup_unknown
    with pytest.raises(ProviderError) as blocked:
        client.decide(context())
    assert (blocked.value.code, blocked.value.tokens, blocked.value.sent) == ("provider_unknown", 0, False)
    assert len(simulated.starts) == len(simulated.posts) == 1
    assert not client._thread_lock.locked() and not client._scope_lock.locked()


def test_explicit_scope_reap_error_does_not_double_bill_returned_replies(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    child = FakeProcess(stubborn=True)
    monkeypatch.setattr(child, "kill", lambda: None)
    simulated = mocked_runtime(monkeypatch, client, process=child)
    with pytest.raises(ProviderError) as error, client.session():
        replies = [client.decide(context()), client.decide(context())]
    assert sum(reply.tokens for reply in replies) == 8000
    assert (error.value.code, error.value.tokens, error.value.sent) == ("provider_unknown", 0, False)
    assert client._cleanup_unknown and len(simulated.posts) == 2


def test_startup_cleanup_failure_still_known_unsent(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    child = FakeProcess(stubborn=True)
    monkeypatch.setattr(child, "kill", lambda: None)
    simulated = mocked_runtime(monkeypatch, client, process=child)

    def fail(_process):
        raise ProviderError("provider_unavailable", "TEST startup failed", sent=False)

    monkeypatch.setattr(client, "_wait_ready", fail)
    with pytest.raises(ProviderError) as error:
        client.decide(context())
    assert (error.value.code, error.value.tokens, error.value.sent) == ("provider_unknown", 0, False)
    assert client._cleanup_unknown and not simulated.posts


def test_stubborn_child_killed_and_reaped(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    child = FakeProcess(stubborn=True)
    mocked_runtime(monkeypatch, client, process=child)
    client.decide(context())
    assert child.terminated == 1 and child.killed == 1 and child.waits == [10, 10]


@pytest.mark.parametrize("kind", ["startup", "popen", "occupied"])
def test_startup_failures_are_unsent_without_inference(runtime, monkeypatch, kind):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)

    def startup(_):
        raise ProviderError("provider_unavailable", "TEST startup failure", sent=False)

    def popen(*_args, **_kwargs):
        raise OSError("TEST-SENSITIVE cannot execute")

    if kind == "startup":
        monkeypatch.setattr(client, "_wait_ready", startup)
    elif kind == "popen":
        monkeypatch.setattr(module.subprocess, "Popen", popen)
    else:
        monkeypatch.setattr(module.socket, "socket", lambda *_: FakeSocket(occupied=True))
    with pytest.raises(ProviderError) as error:
        client.decide(context())
    assert (error.value.code, error.value.tokens, error.value.sent) == ("provider_unavailable", 0, False)
    assert not simulated.posts and not client._thread_lock.locked()
    if kind == "startup":
        assert simulated.process.terminated == 1
    else:
        assert not simulated.starts


def test_external_os_lock_prevents_launch(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with Path(runtime.config["lock_path"]).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ProviderError) as error:
            client.decide(context())
        assert error.value.code == "provider_unavailable" and not error.value.sent
    assert not simulated.starts and not simulated.posts
    client.decide(context())
    assert len(simulated.posts) == 1


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "missing-parent", "fifo"])
def test_unsafe_lock_rejected_without_launch(runtime, monkeypatch, kind):
    path = Path(runtime.config["lock_path"])
    if kind == "symlink":
        path.symlink_to(runtime.generation)
    elif kind == "hardlink":
        os.link(runtime.generation, path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        runtime.config["lock_path"] = str(runtime.root / "absent" / "runtime.lock")
        runtime.path.write_text(json.dumps(runtime.config))
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with pytest.raises(ProviderError) as error:
        client.decide(context())
    assert (error.value.code, error.value.tokens, error.value.sent) == ("runtime_config", 0, False)
    assert not simulated.starts


@pytest.mark.parametrize(("suffix", "model"), [
    ("/v1/embeddings", GENERATION_ALIAS), ("/v1/chat/completions/extra", GENERATION_ALIAS),
    ("/v1/chat/completions", "cloud-model"), ("/v1/chat/completions", "embedding-model"),
    ("/v1/chat/completions", GENERATION_ALIAS),
])
def test_raw_managed_transport_cannot_bypass_decide_owner(runtime, monkeypatch, suffix, model):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)
    with pytest.raises(ProviderError) as error:
        client._post(client.generation_url + suffix, {"model": model})
    assert error.value.code == "runtime_request" and not error.value.sent
    assert not simulated.starts and not simulated.posts


def test_health_bounded_loopback_get_without_proxy_or_redirect(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    replies = iter([httpx.Response(503), httpx.Response(200, json={"status": "ok"})])
    requests = []
    options = fake_http(monkeypatch, lambda request: requests.append(request) or next(replies))
    client._wait_ready(FakeProcess())
    assert len(requests) == 2 and all(request.method == "GET" for request in requests)
    assert all(str(request.url) == "http://127.0.0.1:18080/health" for request in requests)
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert options[0]["timeout"].read == 2 and options[0]["timeout"].connect == 1
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize("status", [302, 401, 500])
def test_health_unexpected_status_no_redirect(runtime, monkeypatch, status):
    requests = []
    fake_http(monkeypatch, lambda request: requests.append(request) or
              httpx.Response(status, headers={"location": "https://outside.invalid"}))
    with pytest.raises(ProviderError) as error:
        ManagedLocalProvider(runtime.path)._wait_ready(FakeProcess())
    assert error.value.code == "provider_unavailable" and not error.value.sent and len(requests) == 1


@pytest.mark.parametrize("status", [200, 503])
def test_health_size_bound_including_loading_body(runtime, monkeypatch, status):
    fake_http(monkeypatch, lambda _: httpx.Response(status, content=b"x" * 4097))
    with pytest.raises(ProviderError) as error:
        ManagedLocalProvider(runtime.path)._wait_ready(FakeProcess())
    assert error.value.code == "provider_unavailable" and not error.value.sent


def test_health_exited_child_never_contacts_server(runtime, monkeypatch):
    fake_http(monkeypatch, lambda _: pytest.fail("No HTTP for an exited child"))
    with pytest.raises(ProviderError) as error:
        ManagedLocalProvider(runtime.path)._wait_ready(FakeProcess(running=False))
    assert error.value.code == "provider_unavailable" and not error.value.sent


def test_health_startup_deadline_known_unsent(runtime, monkeypatch):
    fake_http(monkeypatch, lambda _: pytest.fail("No HTTP after timeout"))
    clock = iter([0, module.STARTUP_TIMEOUT + 1])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    with pytest.raises(ProviderError) as error:
        ManagedLocalProvider(runtime.path)._wait_ready(FakeProcess())
    assert error.value.code == "provider_unavailable" and not error.value.sent


def test_untyped_startup_failure_is_unsent_and_sanitized(runtime, monkeypatch):
    client = ManagedLocalProvider(runtime.path)
    simulated = mocked_runtime(monkeypatch, client)

    def fail(_process):
        raise OSError("TEST-SENSITIVE temporary runtime failure")

    monkeypatch.setattr(client, "_wait_ready", fail)
    with pytest.raises(ProviderError) as error:
        client.decide(context())
    assert (error.value.code, error.value.tokens, error.value.sent) == ("provider_unavailable", 0, False)
    assert "TEST-SENSITIVE" not in str(error.value)
    assert simulated.process.terminated == 1 and not simulated.posts


def test_health_slow_trickle_checked_per_transport_chunk(runtime, monkeypatch):
    clock = [0.0]

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            clock[0] += module.STARTUP_TIMEOUT + 1
            yield b" "
            pytest.fail("Deadline must not wait for a larger buffered chunk")

    fake_http(monkeypatch, lambda _: httpx.Response(200, stream=SlowStream()))
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    with pytest.raises(ProviderError) as error:
        ManagedLocalProvider(runtime.path)._wait_ready(FakeProcess())
    assert error.value.code == "provider_unavailable" and not error.value.sent
