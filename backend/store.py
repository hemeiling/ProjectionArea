"""In-process document store.

CONSTITUTION.md §35: uploaded engineering drawings are proprietary. They are
written to a private temporary directory, never served back verbatim, deleted
on request, and swept after a TTL. Nothing is sent anywhere else.

§27: this is an in-process dictionary, not a database or a queue. It is the
right amount of infrastructure for a single-user desktop-style tool, and the
interface is small enough to swap later without touching the engine.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import fitz

from backend.config import DOCUMENT_TTL_SECONDS
from backend.pipeline import PreparedPage, prepare_page
from backend.progress import NULL_PROGRESS, Progress


@dataclass
class StoredDocument:
    """One uploaded drawing plus its per-page analysis cache.

    Either a PDF (``doc`` is a ``fitz.Document``) or a DXF (``cad`` is a
    :class:`~backend.cad.dxf.CadDrawing`). The two sources reach the same engine
    through different adapters (§37), so the store carries whichever it opened
    and the API branches on :attr:`kind`.
    """

    id: str
    file_name: str
    path: str
    doc: Optional[fitz.Document]
    created_at: float
    last_used: float
    pages: Dict[int, PreparedPage] = field(default_factory=dict)
    cad: Any = None
    #: Set when the source was a DWG converted locally to DXF. The source stays
    #: a DWG — this records how it got here, not what it became.
    conversion: Any = None

    @property
    def kind(self) -> str:
        if self.conversion is not None:
            return "dwg"
        return "dxf" if self.cad is not None else "pdf"

    def close(self) -> None:
        if self.doc is None:
            return
        try:
            self.doc.close()
        except Exception:
            pass


#: Prefix for every store directory, so orphans can be recognised.
STORE_PREFIX = "projected-area-"

#: Name of the marker naming the process that owns a store directory.
OWNER_MARKER = ".owner-pid"


class DocumentStore:
    """Thread-safe store of uploaded documents with TTL sweeping."""

    def __init__(self, ttl_seconds: int = DOCUMENT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._documents: Dict[str, StoredDocument] = {}
        self._root = tempfile.mkdtemp(prefix=STORE_PREFIX)
        self._claim()
        sweep_orphaned_stores()

    @property
    def root(self) -> str:
        return self._root

    def _claim(self) -> None:
        """Record which process owns this directory.

        :meth:`shutdown` removes the directory on a normal stop, but a process
        that is killed outright — ``SIGKILL``, an out-of-memory kill, a crash —
        never runs it, and what it leaves behind is customer geometry (§35). The
        marker lets the next start recognise that directory as abandoned.
        """
        try:
            with open(os.path.join(self._root, OWNER_MARKER), "w") as handle:
                handle.write(str(os.getpid()))
        except OSError:
            pass  # a store that cannot mark itself still works

    # ── spooling ────────────────────────────────────────────────────────────
    #
    # A production DWG is 93 MB and its converted DXF is larger still. Reading
    # one into a ``bytes`` before doing anything with it costs that much memory
    # for the whole life of the job, on top of the copy that has to be on disk
    # for the converter to read — on a 512 MB instance that is the difference
    # between working and being killed. So an upload is streamed straight to a
    # file in this store's directory, and every ingest below can start from a
    # path instead of a buffer.

    def spool_path(self, suffix: str) -> str:
        """A fresh path in the store's directory for an incoming upload.

        The file lands on the same filesystem the document will live on, so
        adopting it afterwards is a rename rather than a copy.
        """
        self.sweep()
        os.makedirs(self._root, exist_ok=True)
        return os.path.join(self._root, f"incoming-{uuid.uuid4().hex}{suffix}")

    def _adopt(self, spooled: str, suffix: str) -> Tuple[str, str]:
        """Move a spooled upload to its document path. Returns (id, path)."""
        os.makedirs(self._root, exist_ok=True)
        document_id = uuid.uuid4().hex[:16]
        path = os.path.join(self._root, f"{document_id}{suffix}")
        os.replace(spooled, path)
        return document_id, path

    def _register(self, stored: "StoredDocument") -> "StoredDocument":
        with self._lock:
            self._documents[stored.id] = stored
        return stored

    @staticmethod
    def discard(path: str) -> None:
        """Delete a spooled or abandoned file, ignoring an already-gone one."""
        try:
            os.unlink(path)
        except OSError:
            pass

    # ── ingest ──────────────────────────────────────────────────────────────
    #
    # Each format has a path-based ``adopt_*`` that takes a file already on disk
    # — what the upload endpoint streams — and an ``add_*`` that takes bytes, for
    # the demo catalogue and the tests, where the drawings are small and a buffer
    # is the natural thing to have.

    def adopt_cad(
        self, spooled: str, file_name: str, progress: Progress = NULL_PROGRESS
    ) -> StoredDocument:
        """Take over a spooled DXF and read it through the CAD adapter.

        Args:
            spooled: A file from :meth:`spool_path`. Consumed either way: it
                becomes the document, or it is deleted.

        Raises:
            ValueError: If the file is not a readable DXF.
        """
        from backend.cad.dxf import DxfReadError, load_dxf

        document_id, path = self._adopt(spooled, ".dxf")
        try:
            drawing = load_dxf(path, progress=progress)
        except DxfReadError as error:
            self.discard(path)
            raise ValueError(str(error)) from error
        except Exception as error:
            self.discard(path)
            raise ValueError(f"Not a readable DXF: {error}") from error
        if not drawing.primitives:
            self.discard(path)
            raise ValueError("The DXF contains no readable geometry in model space")

        now = time.time()
        return self._register(StoredDocument(
            id=document_id, file_name=file_name, path=path, doc=None,
            created_at=now, last_used=now, cad=drawing,
        ))

    def adopt_dwg(self, spooled: str, file_name: str, on_stage: Any = None,
                  progress: Progress = NULL_PROGRESS) -> StoredDocument:
        """Take over a spooled DWG, convert it locally, and read the result.

        Raises:
            DwgConversionUnavailable: No converter is available here.
            ValueError: The file is not a DWG, or produced nothing readable.
        """
        from backend.cad.dwg import DwgConversionFailed, load_dwg

        document_id, path = self._adopt(spooled, ".dwg")
        try:
            drawing, conversion = load_dwg(
                path, file_name=file_name, on_stage=on_stage, progress=progress,
            )
        except DwgConversionFailed as error:
            self.discard(path)
            raise ValueError(str(error)) from error
        except Exception:
            self.discard(path)
            raise
        if not drawing.primitives:
            self.discard(path)
            raise ValueError(
                "The DWG converted successfully but carries no readable geometry "
                "in model space."
            )

        now = time.time()
        return self._register(StoredDocument(
            id=document_id, file_name=file_name, path=path, doc=None,
            created_at=now, last_used=now, cad=drawing, conversion=conversion,
        ))

    def adopt_pdf(self, spooled: str, file_name: str) -> StoredDocument:
        """Take over a spooled PDF and open it.

        Raises:
            ValueError: If the file is not a readable PDF.
        """
        document_id, path = self._adopt(spooled, ".pdf")
        try:
            doc = fitz.open(path)
        except Exception as error:
            self.discard(path)
            raise ValueError(f"Not a readable PDF: {error}") from error
        if doc.page_count == 0:
            doc.close()
            self.discard(path)
            raise ValueError("PDF contains no pages")

        now = time.time()
        return self._register(StoredDocument(
            id=document_id, file_name=file_name, path=path, doc=doc,
            created_at=now, last_used=now,
        ))

    def _spool_bytes(self, data: bytes, suffix: str) -> str:
        path = self.spool_path(suffix)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def add_cad(
        self, data: bytes, file_name: str, progress: Progress = NULL_PROGRESS
    ) -> StoredDocument:
        """Persist a DXF held in memory. See :meth:`adopt_cad`."""
        return self.adopt_cad(self._spool_bytes(data, ".dxf"), file_name, progress)

    def add_dwg(self, data: bytes, file_name: str, on_stage: Any = None,
                progress: Progress = NULL_PROGRESS) -> StoredDocument:
        """Persist a DWG held in memory. See :meth:`adopt_dwg`."""
        return self.adopt_dwg(
            self._spool_bytes(data, ".dwg"), file_name, on_stage, progress)

    def add(self, data: bytes, file_name: str) -> StoredDocument:
        """Persist a PDF held in memory. See :meth:`adopt_pdf`."""
        return self.adopt_pdf(self._spool_bytes(data, ".pdf"), file_name)

    def get(self, document_id: str) -> StoredDocument:
        """Look up a document, refreshing its TTL.

        Raises:
            KeyError: If the document is unknown or has been swept.
        """
        with self._lock:
            stored = self._documents.get(document_id)
            if stored is None:
                raise KeyError(document_id)
            stored.last_used = time.time()
            return stored

    def prepared_page(
        self, document_id: str, page_number: int, progress: Progress = NULL_PROGRESS
    ) -> Tuple[StoredDocument, PreparedPage]:
        """Return the cached analysis for a page, computing it on first use."""
        stored = self.get(document_id)
        with self._lock:
            page = stored.pages.get(page_number)
            if page is None:
                if stored.cad is not None:
                    # Any CAD source — a DXF read directly, or a DWG converted
                    # locally. Branching on `kind` would miss the DWG, whose
                    # kind is "dwg" precisely so the UI keeps saying DWG.
                    from backend.cad.pipeline import prepare_cad_page

                    page = prepare_cad_page(stored.cad)
                else:
                    page = prepare_page(stored.doc, page_number, progress)
                stored.pages[page_number] = page
            return stored, page

    def remove(self, document_id: str) -> bool:
        """Delete a document and its file. Returns True if it existed."""
        with self._lock:
            stored = self._documents.pop(document_id, None)
        if stored is None:
            return False
        stored.close()
        try:
            os.unlink(stored.path)
        except OSError:
            pass
        return True

    def sweep(self) -> int:
        """Delete documents idle for longer than the TTL."""
        cutoff = time.time() - self._ttl
        with self._lock:
            stale = [d.id for d in self._documents.values() if d.last_used < cutoff]
        return sum(1 for document_id in stale if self.remove(document_id))

    def shutdown(self) -> None:
        with self._lock:
            ids = list(self._documents)
        for document_id in ids:
            self.remove(document_id)
        shutil.rmtree(self._root, ignore_errors=True)


def _process_is_alive(pid: int) -> bool:
    """Whether a process id is still running, without signalling it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, and owned by someone else
    except OSError:
        return True  # unknown, so assume alive and leave it alone
    return pid > 0


def sweep_orphaned_stores() -> int:
    """Delete store directories whose owning process is gone.

    Uploaded drawings are proprietary, so a directory left by a killed process
    is a privacy problem and not merely litter — and on a small instance being
    killed part way through a large drawing is a *likely* ending, not an exotic
    one. Called on startup, when whatever went wrong has already happened.

    Deliberately conservative: a directory is removed only when it carries an
    owner marker naming a process that no longer exists. A directory with no
    marker, or one whose owner is alive, is left alone — deleting the files of a
    running instance would be a far worse failure than leaving a stale folder.

    Returns:
        How many directories were removed.
    """
    parent = tempfile.gettempdir()
    removed = 0
    try:
        names = os.listdir(parent)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(STORE_PREFIX):
            continue
        candidate = os.path.join(parent, name)
        marker = os.path.join(candidate, OWNER_MARKER)
        try:
            with open(marker) as handle:
                owner = int(handle.read().strip())
        except (OSError, ValueError):
            continue  # unmarked: not ours to judge
        if _process_is_alive(owner):
            continue
        shutil.rmtree(candidate, ignore_errors=True)
        removed += 1
    return removed


STORE = DocumentStore()
