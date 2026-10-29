"""Explicit local bootstrap and serving; no implicit provider, inference or training."""

import json
import secrets
import time
from functools import wraps
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from pydantic import ValidationError

from .errors import DomainError
from .fixtures import demo_snapshots
from .service import SchedulingService
from .storage import Store

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False,
                  help="Local production scheduling with original synthetic data only.")
FIXTURE_CASES = ("normal", "infeasible", "missing")


def _output(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _safe(command):
    @wraps(command)
    def wrapped(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except DomainError as exc:
            typer.echo(f"{exc.code}: {exc.message}", err=True)
            raise typer.Exit(1) from None
        except ValidationError:
            typer.echo("validation_error: Invalid fields, types or ranges", err=True)
            raise typer.Exit(2) from None
        except (typer.Exit, typer.Abort):
            raise
        except Exception:
            typer.echo("local_error: Local operation failed; internal details withheld", err=True)
            raise typer.Exit(1) from None
    return wrapped


def _safe_path(data: Path) -> Path:
    path = data.expanduser().absolute()
    if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise DomainError("unsafe_path", "Symlinks and parent traversal are not allowed", 409)
    return path


def _existing_store(data: Path) -> Store:
    path = _safe_path(data)
    if not path.is_dir() or not (path / "data.sqlite3").is_file():
        raise DomainError("not_initialized", "Initialize a new data directory first", 409)
    return Store(path)


@app.command("init")
@_safe
def initialize(data: Path):
    """Create a NEW directory, import original snapshots and show one admin token once."""
    path = _safe_path(data)
    if path.exists():
        raise DomainError("init_exists", "Init requires a new, nonexistent directory", 409)
    if not path.parent.is_dir():
        raise DomainError("init_parent", "The parent directory must already exist", 409)
    snapshots = demo_snapshots(time.time_ns() // 1_000_000)
    plants = sorted({snapshot.plant_id for snapshot in snapshots.values()})
    path.mkdir(mode=0o700, exist_ok=False)
    store = Store(path)
    token = secrets.token_urlsafe(32)
    principal = store.create_user("local-admin", token, "admin", plants)
    service = SchedulingService(store)
    for case, snapshot in snapshots.items():
        service.import_snapshot(principal, snapshot, action_id="init-" + case)
    _output({"user_id": principal.user_id, "role": principal.role, "plants": plants,
             "token": token, "snapshots_imported": len(snapshots),
             "notice": "Token shown once; only its hash is stored. Original synthetic data only. "
                       "No inference, solve, recommendation, approval, publication or execution ran."})


@app.command("fixture")
@_safe
def fixture(case: Annotated[str, typer.Option("--case")] = "normal"):
    """Print one original synthetic Snapshot JSON object without persisting it."""
    if case not in FIXTURE_CASES:
        raise DomainError("unknown_case", "Choose normal, infeasible or missing", 422)
    _output(demo_snapshots(time.time_ns() // 1_000_000)[case])


@app.command("serve")
@_safe
def serve(
    data: Path,
    runtime_config: Annotated[Path | None, typer.Option("--runtime-config")] = None,
    model_url: Annotated[str | None, typer.Option("--model-url")] = None,
    test_mode: Annotated[bool, typer.Option("--test-mode")] = False,
    port: Annotated[int, typer.Option("--port")] = 8006,
):
    """Choose exactly one explicit local provider; bind only 127.0.0.1, never a public host."""
    if sum((runtime_config is not None, model_url is not None, test_mode)) != 1:
        raise DomainError("provider_choice",
                          "Choose exactly one of --runtime-config, --model-url, --test-mode")
    if not 1 <= port <= 65535:
        raise DomainError("listen_address", "Serve requires a port in 1..65535")
    if runtime_config is not None and not runtime_config.is_absolute():
        raise DomainError("runtime_config", "Runtime configuration must be an absolute path")
    # Fail before provider construction; serve never initializes a fresh root.
    store = _existing_store(data)
    from .api import _authority, create_app
    from .provider import LocalProvider, SyntheticProvider

    if model_url is not None:
        try:
            parsed = urlsplit(model_url)
            if (parsed.scheme != "http" or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment or "?" in model_url or "#" in model_url
                    or any(ord(char) <= 32 or ord(char) >= 127 for char in model_url)):
                raise ValueError("Root-only numeric loopback URL required")
            _authority(parsed.netloc, parsed.scheme)
        except ValueError:
            raise DomainError("model_url", "Use an HTTP numeric loopback root URL") from None
    if runtime_config is not None:
        from .managed_provider import ManagedLocalProvider
        provider = ManagedLocalProvider(runtime_config)
    elif model_url is not None:
        provider = LocalProvider(model_url)
    else:
        provider = SyntheticProvider()
    application = None
    try:
        application = create_app(store.root, provider)
        _output({"provider": provider.fingerprint, "synthetic_only": True,
                 "listen": f"http://127.0.0.1:{port}", "inference_checked": False})
        import uvicorn
        uvicorn.run(application, host="127.0.0.1", port=port, access_log=False,
                    proxy_headers=False, server_header=False, log_level="warning")
    finally:
        # The lifespan normally closes it. The same guard also covers a server startup failure.
        if application is None:
            provider.close()
        else:
            application.state.close_provider()


def main(argv=None):
    app(args=argv, prog_name="production-scheduling")


if __name__ == "__main__":
    main()
