"""Hot-reloading development server over a synthetic demo ledger. Never touches var/.

    uv run python scripts/dev.py                      # demo-ledger/, created on first run
    uv run python scripts/dev.py --database PATH      # an existing throwaway ledger
    uv run python scripts/dev.py --no-vite            # backend only

Runs two processes:

* the FastAPI app under ``uvicorn --reload``: saving a file in ``src/fincl``
  restarts it in a second or two;
* the Vite dev server on :5173 with React Fast Refresh, which proxies ``/api``
  to the backend (see ``server.proxy`` in ``frontend/vite.config.ts``).

``fincl serve`` mints a one-use launch secret, session cookie and CSRF token per
process, so every restart would log the browser out. Here they are pinned to
fixed values instead, and ``/dev-login`` hands out the cookie, so one visit to
http://127.0.0.1:5173/dev-login survives every reload. That is only acceptable
for a loopback server over a synthetic ledger, so a ledger under ``var/`` is
refused.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
DEFAULT_DATABASE = REPO / "demo-ledger" / "demo.sqlite3"  # git-ignored
HOST = "127.0.0.1"
VITE_PORT = 5173
DEV_SESSION = "fincl-dev-session"
DEV_CSRF = "fincl-dev-csrf"
_DATABASE_ENV = "FINCL_DEV_DATABASE"
_PORT_ENV = "FINCL_DEV_PORT"


def app_factory() -> Any:
    """Build the app in uvicorn's (re)loaded worker; config arrives via the environment."""
    from fastapi.responses import RedirectResponse

    from fincl.api import create_app
    from fincl.refresh import refresh_ledger

    database = Path(os.environ[_DATABASE_ENV])
    port = int(os.environ[_PORT_ENV])
    # Like `fincl serve`: reproject on start, so a projection change you just
    # saved is applied to the demo ledger on the reload it triggers.
    refresh_ledger(database)
    app = create_app(database, port=port, demo=True)
    app.state.session = DEV_SESSION
    app.state.csrf = DEV_CSRF
    app.state.bootstrapped = True

    @app.get("/dev-login")
    def dev_login() -> RedirectResponse:
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            f"fincl_session_{port}", DEV_SESSION, httponly=True, samesite="strict"
        )
        return response

    return app


def _resolve_database(requested: Path | None) -> Path:
    database = (requested or DEFAULT_DATABASE).expanduser().resolve()
    if database.is_relative_to(REPO / "var"):
        sys.exit("Refusing a ledger under var/: the dev server pins its session token.")
    if database.exists():
        return database
    if requested is not None:
        sys.exit(f"No ledger at {database}. Omit --database to build the demo.")
    sys.path.insert(0, str(SCRIPTS))
    from create_demo import create_demo

    print(f"Building the demo ledger in {database.parent} …")
    return create_demo(database.parent).resolve()


def _node_path() -> str:
    """A PATH on which `npm` (and the `node` its shebang needs) can be found.

    nvm only adds Node to PATH in shells that source it, so a terminal that did
    not falls back to nvm's newest installed version.
    """
    path = os.environ.get("PATH", "")
    if shutil.which("npm"):
        return path
    nvm = Path(os.environ.get("NVM_DIR", Path.home() / ".nvm")) / "versions" / "node"
    installed = [bin_dir for bin_dir in nvm.glob("v*/bin") if (bin_dir / "npm").exists()]

    def version(bin_dir: Path) -> tuple[int, ...]:
        return tuple(int(part) for part in bin_dir.parent.name[1:].split(".") if part.isdigit())

    if not installed:
        sys.exit("npm was not found on PATH or under ~/.nvm. Install Node, or pass --no-vite.")
    newest = max(installed, key=version)
    print(f"npm is not on PATH; using nvm's {newest.parent.name}.")
    return f"{newest}{os.pathsep}{path}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, help=f"default: {DEFAULT_DATABASE}")
    parser.add_argument("--port", type=int, default=8765, help="backend port (default 8765)")
    parser.add_argument("--no-vite", action="store_true", help="run the backend only")
    args = parser.parse_args()

    import uvicorn

    node_path = None
    if not args.no_vite:
        node_path = _node_path()
        if not (REPO / "frontend" / "node_modules").exists():
            sys.exit("frontend/node_modules is missing. Run: cd frontend && npm ci")

    database = _resolve_database(args.database)
    os.environ[_DATABASE_ENV] = str(database)
    os.environ[_PORT_ENV] = str(args.port)

    vite: subprocess.Popen[bytes] | None = None
    if node_path is not None:
        vite = subprocess.Popen(
            ["npm", "run", "dev", "--", "--port", str(VITE_PORT), "--strictPort"],
            cwd=REPO / "frontend",
            env={**os.environ, "PATH": node_path, "FINCL_API_PORT": str(args.port)},
            # Its own process group, so stopping it also stops the vite that npm spawned.
            start_new_session=True,
        )
        print(f"\n  Open once: http://{HOST}:{VITE_PORT}/dev-login   ({database})\n")
    try:
        uvicorn.run(
            "dev:app_factory",
            factory=True,
            app_dir=str(SCRIPTS),
            host=HOST,
            port=args.port,
            reload=True,
            reload_dirs=[str(REPO / "src" / "fincl")],
            access_log=False,
        )
    finally:
        if vite is not None:
            os.killpg(vite.pid, signal.SIGTERM)
            vite.wait()


if __name__ == "__main__":
    main()
