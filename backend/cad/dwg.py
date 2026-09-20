"""Read a DWG by converting it locally to DXF first.

CONSTITUTION.md §35 (proprietary drawings never leave this machine) and §37 (the
source-adapter seam). There is no pure-Python DWG reader and writing an AC1015
parser is not a reasonable thing to do, so this adapter delegates to a **local**
converter and then hands the result to the DXF adapter that already exists:

    DWG -> local converter -> temporary DXF -> backend.cad.dxf -> primitives

Nothing is uploaded anywhere. The converter runs as a subprocess on this
machine, in an isolated temporary directory that is deleted afterwards.

**The source stays a DWG.** Conversion is an implementation detail, not a change
of provenance: :class:`DwgConversion` records the original file name, its SHA-256,
the DWG version read from its signature, the tool and version that converted it,
the intermediate DXF's hash and size, the converter's own warnings and how long
it took. The rest of the application — and the UI — keep saying DWG.

On the converter
----------------
LibreDWG's ``dwg2dxf`` is the default: GPL, a GNU project, runs offline, and is
invoked as a separate process rather than linked, so its licence does not reach
into this code. ODA's File Converter is supported as an alternative for anyone
who already has it. Both are located at runtime; neither is bundled.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.cad.dxf import CadDrawing, DxfReadError, load_dxf

#: ``AC10xx`` release codes, from the DWG file signature.
DWG_VERSIONS: Dict[str, str] = {
    "AC1006": "R10",
    "AC1009": "R11/R12",
    "AC1012": "R13",
    "AC1014": "R14",
    "AC1015": "AutoCAD 2000/2000i/2002",
    "AC1018": "AutoCAD 2004/2005/2006",
    "AC1021": "AutoCAD 2007/2008/2009",
    "AC1024": "AutoCAD 2010/2011/2012",
    "AC1027": "AutoCAD 2013–2017",
    "AC1032": "AutoCAD 2018+",
}

#: Where a locally built converter is looked for, before falling back to PATH.
_SEARCH_PATHS: Tuple[str, ...] = (
    os.path.expanduser("~/.local/libredwg/bin/dwg2dxf"),
    os.path.expanduser("~/.local/bin/dwg2dxf"),
    "/usr/local/bin/dwg2dxf",
    "/opt/homebrew/bin/dwg2dxf",
    "/usr/bin/dwg2dxf",
)
_ODA_PATHS: Tuple[str, ...] = (
    "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
    "/usr/bin/ODAFileConverter",
    os.path.expanduser("~/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"),
)

#: A conversion that has not finished by now is not going to.
CONVERSION_TIMEOUT_SECONDS = 900


class DwgConversionUnavailable(RuntimeError):
    """No local converter is installed.

    Carries the exact setup instruction rather than a generic failure, because
    "install something" is not an actionable error message (§31).
    """

    def __init__(self) -> None:
        super().__init__(
            "DWG support requires the local CAD conversion component. "
            "Install it with: .venv/bin/python -m tools.install_dwg_support"
        )
        self.setup_command = ".venv/bin/python -m tools.install_dwg_support"
        self.component = "LibreDWG dwg2dxf"


class DwgConversionFailed(RuntimeError):
    """The converter ran but did not produce a usable DXF."""


@dataclass
class DwgConversion:
    """Everything about how a DWG became measurable geometry.

    Kept so a result can be traced back to the file an engineer holds, and so a
    conversion problem can be told apart from a measurement problem.
    """

    source_name: str
    source_sha256: str
    source_bytes: int
    dwg_signature: str
    dwg_version: str
    tool: str
    tool_version: str
    tool_path: str
    dxf_sha256: str = ""
    dxf_bytes: int = 0
    duration_seconds: float = 0.0
    warnings: List[str] = field(default_factory=list)
    metadata_warnings: List[str] = field(default_factory=list)

    #: The source is a DWG whatever the pipeline did internally.
    source_type: str = "dwg"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "source_bytes": self.source_bytes,
            "dwg_signature": self.dwg_signature,
            "dwg_version": self.dwg_version,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "tool_path": self.tool_path,
            "intermediate_dxf_sha256": self.dxf_sha256,
            "intermediate_dxf_bytes": self.dxf_bytes,
            "duration_seconds": round(self.duration_seconds, 2),
            "warnings": list(self.warnings),
            "metadata_warnings": list(self.metadata_warnings),
            "path": "DWG → local DXF → CAD geometry",
        }


def dwg_signature(data: bytes) -> Optional[str]:
    """The ``AC10xx`` code at the head of a DWG, or ``None`` if absent."""
    head = data[:6]
    if len(head) == 6 and head[:2] == b"AC" and head[2:].isdigit():
        return head.decode("ascii")
    return None


def is_dwg(data: bytes) -> bool:
    return dwg_signature(data) is not None


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Converter:
    """A located local converter."""

    tool: str
    path: str
    version: str


def find_converter() -> Optional[Converter]:
    """The best local converter available, or ``None``.

    Looked up every call rather than cached at import, so installing the
    component does not require restarting the server.
    """
    for candidate in _SEARCH_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return Converter("libredwg", candidate, _dwg2dxf_version(candidate))
    found = shutil.which("dwg2dxf")
    if found:
        return Converter("libredwg", found, _dwg2dxf_version(found))
    for candidate in _ODA_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return Converter("oda_file_converter", candidate, "unknown")
    found = shutil.which("ODAFileConverter")
    if found:
        return Converter("oda_file_converter", found, "unknown")
    return None


def _dwg2dxf_version(path: str) -> str:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        text = (out.stdout or out.stderr or "").strip().splitlines()
        if text:
            match = re.search(r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)", text[0])
            return match.group(1) if match else text[0][:40]
    except Exception:
        pass
    return "unknown"


def converter_status() -> Dict[str, Any]:
    """Whether DWG support is available, for the UI and for diagnostics."""
    converter = find_converter()
    if converter is None:
        return {
            "available": False,
            "component": "LibreDWG dwg2dxf",
            "reason": "No local DWG converter was found on this machine.",
            "setup_command": ".venv/bin/python -m tools.install_dwg_support",
            "searched": list(_SEARCH_PATHS),
        }
    return {
        "available": True,
        "tool": converter.tool,
        "version": converter.version,
        "path": converter.path,
    }


def _run_libredwg(tool: Converter, source: str, target: str) -> List[str]:
    """Convert with ``dwg2dxf``; returns the converter's own warnings."""
    result = subprocess.run(
        [tool.path, "-y", "-o", target, source],
        capture_output=True, text=True, timeout=CONVERSION_TIMEOUT_SECONDS,
    )
    stderr = result.stderr or ""
    warnings = [
        line.strip() for line in stderr.splitlines()
        if line.strip().lower().startswith(("warning", "error"))
    ]
    if result.returncode != 0 and not os.path.exists(target):
        detail = stderr.strip().splitlines()[-3:] or [f"exit code {result.returncode}"]
        raise DwgConversionFailed(
            "The local converter could not read this DWG: " + " / ".join(detail)
        )
    return warnings


def _run_oda(tool: Converter, source: str, target: str) -> List[str]:
    """Convert with ODA File Converter, which works on directories."""
    in_dir = os.path.join(os.path.dirname(target), "oda-in")
    out_dir = os.path.join(os.path.dirname(target), "oda-out")
    os.makedirs(in_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    staged = os.path.join(in_dir, os.path.basename(source))
    shutil.copy2(source, staged)

    result = subprocess.run(
        [tool.path, in_dir, out_dir, "ACAD2018", "DXF", "0", "1"],
        capture_output=True, text=True, timeout=CONVERSION_TIMEOUT_SECONDS,
    )
    produced = [n for n in os.listdir(out_dir) if n.lower().endswith(".dxf")]
    if not produced:
        raise DwgConversionFailed(
            "ODA File Converter produced no DXF: "
            + ((result.stderr or result.stdout or "").strip()[:200] or "no output")
        )
    shutil.move(os.path.join(out_dir, produced[0]), target)
    return []


#: Metadata the CAD adapter depends on. If conversion strips one of these the
#: result is still usable, but the user is told rather than left to wonder.
_CRITICAL_METADATA = (
    ("layers", lambda d: len([layer for layer in d.info.layers if layer.entity_count])),
    ("blocks", lambda d: len([b for b in d.info.blocks if b.insert_count])),
    ("geometry", lambda d: len(d.primitives)),
)


def _check_metadata(drawing: CadDrawing) -> List[str]:
    """Warn about CAD information that did not survive the conversion."""
    warnings: List[str] = []
    if not drawing.primitives:
        warnings.append(
            "The conversion produced no geometry at all. The DXF was written but "
            "carries nothing measurable."
        )
    if not [layer for layer in drawing.info.layers if layer.entity_count]:
        warnings.append(
            "No layer carries geometry after conversion. Layer names are the main "
            "advantage of the CAD path, so check the converted file before relying "
            "on any per-layer reading."
        )
    if not drawing.info.units_declared:
        warnings.append(
            "The drawing does not declare its units ($INSUNITS = 0), so no scale can "
            "be read from the header. This is a property of the drawing, not of the "
            "conversion."
        )
    if drawing.info.unsupported_counts:
        total = sum(drawing.info.unsupported_counts.values())
        kinds = ", ".join(sorted(drawing.info.unsupported_counts)[:4])
        warnings.append(
            f"{total:,} entity(ies) could not be read from the converted DXF ({kinds}). "
            "Their geometry is not included in any measurement."
        )
    return warnings


def load_dwg(
    path: str,
    file_name: Optional[str] = None,
    keep_dxf_in: Optional[str] = None,
    on_stage: Optional[Any] = None,
) -> Tuple[CadDrawing, DwgConversion]:
    """Convert a DWG locally and read it through the DXF adapter.

    Args:
        path: The DWG on disk.
        file_name: Original name to record, when ``path`` is a temporary copy.
        keep_dxf_in: Directory to retain the intermediate DXF in, for debugging.
            Left unset, the temporary workspace is deleted after reading.
        on_stage: ``f(stage, detail)`` called as work completes. Conversion and
            parsing have very different costs — seconds against minutes on a real
            drawing — so they are reported separately rather than as one opaque
            wait.

    Returns:
        ``(drawing, conversion)`` — the geometry, and how it got here.

    Raises:
        DwgConversionUnavailable: No local converter is installed.
        DwgConversionFailed: The converter ran but produced nothing usable.
        DxfReadError: The produced DXF could not be read.
    """
    name = file_name or os.path.basename(path)
    with open(path, "rb") as handle:
        head = handle.read(8)
    signature = dwg_signature(head)
    if signature is None:
        raise DwgConversionFailed(
            f"{name} does not carry a DWG signature; its first bytes are "
            f"{head[:4].hex()} rather than an AC10xx code."
        )

    converter = find_converter()
    if converter is None:
        raise DwgConversionUnavailable()

    conversion = DwgConversion(
        source_name=name,
        source_sha256=_sha256(path),
        source_bytes=os.path.getsize(path),
        dwg_signature=signature,
        dwg_version=DWG_VERSIONS.get(signature, "unknown release"),
        tool=converter.tool,
        tool_version=converter.version,
        tool_path=converter.path,
    )

    workspace = tempfile.mkdtemp(prefix="dwg-convert-")
    target = os.path.join(workspace, os.path.splitext(os.path.basename(name))[0] + ".dxf")
    started = time.time()
    try:
        if converter.tool == "libredwg":
            conversion.warnings = _run_libredwg(converter, path, target)
        else:
            conversion.warnings = _run_oda(converter, path, target)

        if not os.path.exists(target) or os.path.getsize(target) == 0:
            raise DwgConversionFailed(
                "The converter reported success but produced no DXF content."
            )
        conversion.duration_seconds = time.time() - started
        conversion.dxf_bytes = os.path.getsize(target)
        conversion.dxf_sha256 = _sha256(target)
        if on_stage:
            on_stage(
                "converted",
                f"{converter.tool} {converter.version} · {conversion.duration_seconds:.1f}s "
                f"· {conversion.dxf_bytes / 1e6:.0f} MB DXF",
            )
            on_stage("reading", "reading CAD geometry")

        try:
            drawing = load_dxf(target)
        except DxfReadError as error:
            # The converter wrote a file that is not readable DXF. From here that
            # is a conversion failure, not a mysterious parse error: the user
            # supplied a DWG and what came back cannot be used.
            raise DwgConversionFailed(
                f"The DWG was converted but the result could not be read: {error}"
            ) from error
        conversion.metadata_warnings = _check_metadata(drawing)

        if keep_dxf_in:
            os.makedirs(keep_dxf_in, exist_ok=True)
            shutil.copy2(target, os.path.join(keep_dxf_in, os.path.basename(target)))
        return drawing, conversion
    finally:
        # §35: the intermediate carries the same proprietary geometry as the
        # source, so it does not outlive the read.
        shutil.rmtree(workspace, ignore_errors=True)
