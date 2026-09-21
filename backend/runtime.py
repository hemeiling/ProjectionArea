"""Where the application is running, and what that environment allows.

CONSTITUTION.md §14 (no scattered magic values) applied to deployment rather
than to geometry: the port, the upload ceiling and the converter hint are read
here once, so no module has to guess at its surroundings.

This is deliberately separate from :mod:`backend.config`, which holds
*engineering* tolerances. A tolerance is a property of drawings; a port number
is a property of a host. Mixing them would mean a deployment change touching the
file that defines what counts as a closed contour.

Nothing here is required. Every value has a default that works on a laptop with
no environment set at all, which is also what makes the deployed service start
cleanly with no configuration beyond ``PORT``.
"""

from __future__ import annotations

import os
import time
from typing import Optional

#: Set by Render, Heroku, Fly and most other hosts. Absent locally, where
#: ``run.py`` picks a free port instead.
PORT_ENV = "PORT"

#: Optional explicit path to the DWG converter, for a host that installs it
#: somewhere unusual. Never required: a ``dwg2dxf`` on ``PATH`` is found without
#: it, which is the case in this project's container image.
CONVERTER_ENV = "LIBREDWG_BIN"

#: Largest upload accepted, in bytes. The real production drawings run to 93 MB
#: of DWG, and LibreDWG expands one of those into roughly 120 MB of DXF, so a
#: conventional 10 or 32 MB ceiling would reject exactly the files this tool
#: exists to measure. Overridable because the right answer depends on the host's
#: disk and memory, not on the drawings.
MAX_UPLOAD_ENV = "MAX_UPLOAD_MB"
DEFAULT_MAX_UPLOAD_MB = 200

#: Bytes per read when streaming an upload to disk. Large enough that a 93 MB
#: file is ~93 reads, small enough that this is never a meaningful allocation.
UPLOAD_CHUNK_BYTES = 1 << 20  # 1 MiB


def port(default: int = 8000) -> int:
    """The port to bind, from the host's environment or ``default``.

    A host that sets ``PORT`` to something unparseable is a broken host, but
    refusing to start would be worse than binding the default and logging it.
    """
    raw = os.environ.get(PORT_ENV, "").strip()
    if raw.isdigit() and 0 < int(raw) < 65536:
        return int(raw)
    return default


def bind_host(default: str = "127.0.0.1") -> str:
    """``0.0.0.0`` where a platform port is assigned, loopback otherwise.

    A container's health check and router reach the process from outside its
    network namespace, so binding loopback there makes the service unreachable
    while looking, in the logs, exactly like a working one. Locally the opposite
    is true: binding every interface exposes a laptop's uploads to its network.
    The presence of ``PORT`` is the signal that separates the two.
    """
    if os.environ.get(PORT_ENV, "").strip():
        return "0.0.0.0"  # noqa: S104 — deliberate; see above
    return default


def max_upload_bytes() -> int:
    """The upload ceiling in bytes, from the environment or the default."""
    raw = os.environ.get(MAX_UPLOAD_ENV, "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw) * 1024 * 1024
    return DEFAULT_MAX_UPLOAD_MB * 1024 * 1024


def converter_hint() -> Optional[str]:
    """An explicitly configured converter path, if the host set one."""
    raw = os.environ.get(CONVERTER_ENV, "").strip()
    return raw or None


def is_managed_host() -> bool:
    """True when running on a platform that assigns the port.

    Used only to choose *advice*: telling a container to run a local build
    script is useless, so the DWG-unavailable message differs by environment.
    It never changes what the engine computes.
    """
    return bool(os.environ.get(PORT_ENV, "").strip())


# ── which process is answering ───────────────────────────────────────────────
#
# Jobs live in the memory of the process that started them. During a rolling
# deployment the platform starts a new instance and moves traffic to it while the
# old one is still working — and a job poll then lands on a process that has never
# heard of the job. The hosted 102 run showed exactly this: 200 from the old
# instance through 03:54:35, 404 from the new one at 03:54:36, while the old one
# carried on measuring.
#
# The 404 cannot know *why* it has no record. What it can report is a fact: which
# instance answered and how long it has been running. The client compares that
# with the instance that started the job, and can then say "a different instance
# answered" from evidence rather than guessing at restarts or memory.

_STARTED_AT = time.time()


def instance_token() -> str:
    """An opaque identifier for this process, stable for its lifetime.

    Derived from the platform's instance id where one exists, else the host name,
    with the process id and start time mixed in so a restart on the same host is a
    different instance. Hashed: it is for telling instances *apart*, and a host
    name is infrastructure detail with no business in a browser (§35).
    """
    import hashlib
    import platform

    # platform.node() rather than socket.gethostname(): the same value, without
    # importing the socket module into the backend — which the no-network guard
    # rightly forbids, since it is the thing that proves a drawing has no way out.
    raw = "|".join([
        os.environ.get("RENDER_INSTANCE_ID", "") or platform.node(),
        str(os.getpid()),
        f"{_STARTED_AT:.6f}",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def instance_uptime_seconds() -> float:
    """How long this process has been running."""
    return round(time.time() - _STARTED_AT, 1)
