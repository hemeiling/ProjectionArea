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

    @property
    def kind(self) -> str:
        return "dxf" if self.cad is not None else "pdf"

    def close(self) -> None:
        if self.doc is None:
            return
        try:
            self.doc.close()
        except Exception:
            pass


class DocumentStore:
    """Thread-safe store of uploaded documents with TTL sweeping."""

    def __init__(self, ttl_seconds: int = DOCUMENT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._documents: Dict[str, StoredDocument] = {}
        self._root = tempfile.mkdtemp(prefix="projected-area-")

    @property
    def root(self) -> str:
        return self._root

    def add_cad(self, data: bytes, file_name: str) -> StoredDocument:
        """Persist a DXF upload and read it through the CAD adapter.

        Raises:
            ValueError: If the bytes are not a readable DXF.
        """
        from backend.cad.dxf import DxfReadError, load_dxf

        self.sweep()
        os.makedirs(self._root, exist_ok=True)
        document_id = uuid.uuid4().hex[:16]
        path = os.path.join(self._root, f"{document_id}.dxf")
        with open(path, "wb") as handle:
            handle.write(data)
        try:
            drawing = load_dxf(path)
        except DxfReadError as error:
            os.unlink(path)
            raise ValueError(str(error)) from error
        except Exception as error:
            os.unlink(path)
            raise ValueError(f"Not a readable DXF: {error}") from error
        if not drawing.primitives:
            os.unlink(path)
            raise ValueError("The DXF contains no readable geometry in model space")

        now = time.time()
        stored = StoredDocument(
            id=document_id, file_name=file_name, path=path, doc=None,
            created_at=now, last_used=now, cad=drawing,
        )
        with self._lock:
            self._documents[document_id] = stored
        return stored

    def add(self, data: bytes, file_name: str) -> StoredDocument:
        """Persist an upload and open it.

        Raises:
            ValueError: If the bytes are not a readable PDF.
        """
        self.sweep()
        # The OS temp cleaner (or a test tearing the app down) can remove the
        # directory underneath us; recreate rather than fail the upload.
        os.makedirs(self._root, exist_ok=True)
        document_id = uuid.uuid4().hex[:16]
        path = os.path.join(self._root, f"{document_id}.pdf")
        with open(path, "wb") as handle:
            handle.write(data)
        try:
            doc = fitz.open(path)
        except Exception as error:
            os.unlink(path)
            raise ValueError(f"Not a readable PDF: {error}") from error
        if doc.page_count == 0:
            doc.close()
            os.unlink(path)
            raise ValueError("PDF contains no pages")

        now = time.time()
        stored = StoredDocument(
            id=document_id, file_name=file_name, path=path, doc=doc, created_at=now, last_used=now
        )
        with self._lock:
            self._documents[document_id] = stored
        return stored

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

    def prepared_page(self, document_id: str, page_number: int) -> Tuple[StoredDocument, PreparedPage]:
        """Return the cached analysis for a page, computing it on first use."""
        stored = self.get(document_id)
        with self._lock:
            page = stored.pages.get(page_number)
            if page is None:
                if stored.kind == "dxf":
                    from backend.cad.pipeline import prepare_cad_page

                    page = prepare_cad_page(stored.cad)
                else:
                    page = prepare_page(stored.doc, page_number)
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


STORE = DocumentStore()
