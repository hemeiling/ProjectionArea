#!/usr/bin/env python3
"""Start the Projected Area Analyzer — one command, one URL.

    python run.py

Serves the viewer and the API from the same origin, so there is no CORS hop and
no separate frontend process. Demo drawings are generated up front, so the first
click is instant.

Options::

    python run.py --port 8123      # different port
    python run.py --no-reload      # don't restart on file changes
    python run.py --no-demo        # skip pre-generating demo drawings
    python run.py --open           # open a browser too

This is the *local* entry point. It looks for a free port, enables auto-reload
and prints a clickable URL — all of which are conveniences for a developer and
wrong in production, where the platform assigns the port and expects the process
to bind it or fail. A deployment therefore runs uvicorn directly::

    python -m uvicorn backend.main:app --host 0.0.0.0 --port $PORT

Run this file with ``PORT`` set and it defers to that value and binds every
interface, so the two paths agree when it matters. See docs/DEPLOYMENT.md.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import webbrowser

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from backend import runtime  # noqa: E402  (after sys.path is set)

#: Tried in order when the requested port is busy.
_PORT_ATTEMPTS = 12


def _venv_hint() -> str:
    """Tell the user how to get the dependencies, if they are missing.

    The venv's console scripts hard-code the interpreter path they were installed
    with, so they break when the project directory is renamed; the ``-m`` form
    never does. Hence the advice below.
    """
    candidate = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")
    runner = candidate if os.path.exists(candidate) else "python3"
    return (
        "Dependencies are missing. Set up the environment and use its interpreter:\n\n"
        "    python3 -m venv .venv\n"
        "    .venv/bin/pip install -r requirements-dev.txt\n"
        f"    {runner} run.py\n"
    )


def _check_imports() -> None:
    try:
        import fitz  # noqa: F401
        import shapely  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as error:
        print(f"\n{error}\n\n{_venv_hint()}", file=sys.stderr)
        raise SystemExit(1)


def _free_port(preferred: int) -> int:
    """First free port at or after ``preferred``.

    A stale server from a previous run should not stop this one starting; being
    told the real URL matters more than insisting on 8000.
    """
    for offset in range(_PORT_ATTEMPTS):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(
        f"Ports {preferred}–{preferred + _PORT_ATTEMPTS - 1} are all in use. "
        "Pass --port to choose another."
    )


def _prepare_demos() -> int:
    """Generate the demo drawings so the first click does not wait on them."""
    from backend.demo.catalogue import CATALOGUE, build_all

    build_all()
    return len(CATALOGUE)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="port to serve on (default: $PORT, else 8000 or the next free one)")
    parser.add_argument(
        "--host", default=None,
        help="interface to bind (default: 0.0.0.0 when $PORT is set, else 127.0.0.1)")
    parser.add_argument("--no-reload", action="store_true", help="disable auto-restart")
    parser.add_argument("--reload", action="store_true",
                        help="force auto-restart even where it is off by default")
    parser.add_argument("--no-demo", action="store_true", help="skip demo pre-generation")
    parser.add_argument("--open", action="store_true", help="open a browser window")
    parser.add_argument("--no-open", action="store_true", help="never open a browser")
    args = parser.parse_args(argv)

    _check_imports()

    host = args.host or runtime.bind_host()

    # A platform-assigned port is not a suggestion: if it is taken, something is
    # already wrong, and quietly serving on a different one would leave a
    # healthy-looking process that nothing can reach. Only a local port is nudged
    # to the next free one, because there the alternative is refusing to start
    # over a stale server the developer has forgotten about.
    assigned = runtime.port(0)
    if assigned and args.port is None:
        requested = port = assigned
    else:
        requested = args.port if args.port is not None else 8000
        port = _free_port(requested)

    demo_count = 0
    if not args.no_demo:
        try:
            demo_count = _prepare_demos()
        except Exception as error:  # a broken demo must not stop the server
            print(f"  ! demo drawings unavailable: {error}", file=sys.stderr)

    url = f"http://localhost:{port}/"
    if host not in ("127.0.0.1", "localhost"):
        url = f"http://{host}:{port}/"
    engine = "unknown"
    try:
        from backend.config import ENGINE_VERSION

        engine = ENGINE_VERSION
    except Exception:
        pass

    # Watching the source tree is a developer convenience. On a host that
    # assigns the port it is a second process and a file watcher for no benefit,
    # so it is off there unless explicitly asked for.
    reload = args.reload or (not args.no_reload and not assigned)

    print()
    print("  Projected Area Analyzer is running", f"· engine {engine}")
    print("  " + "-" * 52)
    print()
    print(f"  Open:  {url}")
    print()
    print(f"  API docs: http://localhost:{port}/docs")
    if demo_count:
        print(f"  {demo_count} reference drawings ready on the landing screen")
    if port != requested:
        print(f"  (port {requested} was busy, using {port})")
    print("  Stop with Ctrl+C")
    print()

    if args.open:
        webbrowser.open(url)

    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=host,
        port=port,
        reload=reload,
        reload_dirs=[os.path.join(PROJECT_ROOT, "backend")] if reload else None,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
