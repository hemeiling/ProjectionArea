"""Install the local DWG conversion component.

    .venv/bin/python -m tools.install_dwg_support

Builds GNU LibreDWG from its official release tarball and installs ``dwg2dxf``
into ``~/.local/libredwg``. Nothing is installed system-wide, no administrator
rights are needed, and no account or licence key is involved.

Why this component
------------------
There is no pure-Python DWG reader, and writing an AC1015 parser is not a
reasonable thing to build. LibreDWG is a GNU project under the GPL, it runs
entirely offline, and the application invokes ``dwg2dxf`` as a **separate
process** rather than linking against it — so proprietary drawings never leave
the machine (§35) and the converter's licence does not reach into this code.

Why it is built rather than downloaded
--------------------------------------
No package manager is assumed to exist on the target machine. The release
tarball ships a pre-generated ``configure``, so only a C compiler and ``make``
are required — both present with Xcode Command Line Tools on macOS and with
build-essential on Linux.

The build needs ``pkg-config`` on ``PATH``. LibreDWG only uses it to look for
optional libraries (pcre2, for a text-search tool this application never calls),
but ``configure`` refuses to start without the binary. When it is missing, a
minimal stand-in is placed on ``PATH`` for the build only; it reports every
optional package as absent, which on such a machine is simply true.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

#: Official GNU release. Pinned so a build is reproducible.
LIBREDWG_VERSION = "0.14"
TARBALL_URL = f"https://ftp.gnu.org/gnu/libredwg/libredwg-{LIBREDWG_VERSION}.tar.xz"
PREFIX = os.path.expanduser("~/.local/libredwg")

_PKG_CONFIG_STUB = """#!/bin/sh
# Minimal pkg-config stand-in, used only for this build.
# This machine has no pkg-config and none of LibreDWG's optional libraries, so
# every package query honestly reports "not installed".
case "$1" in
  --version) echo "0.29.2"; exit 0 ;;
  --atleast-pkgconfig-version) exit 0 ;;
  *) exit 1 ;;
esac
"""


def _say(message: str) -> None:
    print(f"  {message}", flush=True)


def _check_toolchain() -> None:
    missing = [tool for tool in ("cc", "make") if shutil.which(tool) is None]
    if missing:
        raise SystemExit(
            f"\nA C toolchain is required and {', '.join(missing)} was not found.\n\n"
            "  macOS: xcode-select --install\n"
            "  Debian/Ubuntu: sudo apt install build-essential\n"
            "  Fedora: sudo dnf groupinstall 'Development Tools'\n"
        )


def _build(workspace: str, prefix: str) -> str:
    archive = os.path.join(workspace, "libredwg.tar.xz")
    _say(f"downloading LibreDWG {LIBREDWG_VERSION} from ftp.gnu.org")
    urllib.request.urlretrieve(TARBALL_URL, archive)

    _say("unpacking")
    with tarfile.open(archive) as tar:
        tar.extractall(workspace)
    source = os.path.join(workspace, f"libredwg-{LIBREDWG_VERSION}")

    env = dict(os.environ)
    if shutil.which("pkg-config") is None:
        stub_dir = os.path.join(workspace, "stub")
        os.makedirs(stub_dir, exist_ok=True)
        stub = os.path.join(stub_dir, "pkg-config")
        with open(stub, "w") as handle:
            handle.write(_PKG_CONFIG_STUB)
        os.chmod(stub, 0o755)
        env["PATH"] = stub_dir + os.pathsep + env.get("PATH", "")
        _say("pkg-config not found; using a build-only stand-in")

    log = os.path.join(workspace, "build.log")
    with open(log, "w") as handle:
        _say("configuring")
        subprocess.run(
            ["./configure", f"--prefix={prefix}", "--disable-bindings",
             "--disable-python", "--disable-docs", "--disable-werror"],
            cwd=source, env=env, stdout=handle, stderr=subprocess.STDOUT, check=True,
        )
        _say("compiling (a few minutes)")
        subprocess.run(
            ["make", f"-j{max(2, (os.cpu_count() or 2))}"],
            cwd=source, env=env, stdout=handle, stderr=subprocess.STDOUT, check=True,
        )
        _say(f"installing into {prefix}")
        subprocess.run(
            ["make", "install"],
            cwd=source, env=env, stdout=handle, stderr=subprocess.STDOUT, check=True,
        )
    return log


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefix", default=PREFIX)
    parser.add_argument("--force", action="store_true", help="rebuild even if present")
    args = parser.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend.cad.dwg import converter_status

    print("\n  DWG conversion component")
    print("  " + "-" * 46)

    status = converter_status()
    if status["available"] and not args.force:
        _say(f"already installed: {status['tool']} {status['version']}")
        _say(f"at {status['path']}")
        print("\n  DWG upload is ready.\n")
        return 0

    _check_toolchain()
    workspace = tempfile.mkdtemp(prefix="libredwg-build-")
    try:
        log = _build(workspace, args.prefix)
    except subprocess.CalledProcessError:
        tail = ""
        candidate = os.path.join(workspace, "build.log")
        if os.path.exists(candidate):
            with open(candidate) as handle:
                tail = "".join(handle.readlines()[-15:])
        print(f"\n  Build failed. Last output:\n\n{tail}\n", file=sys.stderr)
        print(f"  Full log: {candidate}", file=sys.stderr)
        return 1
    else:
        shutil.rmtree(workspace, ignore_errors=True)

    status = converter_status()
    if not status["available"]:
        print("\n  The build finished but dwg2dxf was not found afterwards.\n", file=sys.stderr)
        return 1

    _say(f"installed {status['tool']} {status['version']}")
    _say(f"at {status['path']}")
    print("\n  DWG upload is ready. Restart the app if it is running.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
