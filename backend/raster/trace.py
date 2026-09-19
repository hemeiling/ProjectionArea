"""Path B — scanned and raster engineering drawings.

CONSTITUTION.md §19 and §30. Only reached when a page carries no usable vector
linework. It renders the page, binarises the ink, and hands the resulting mask
to the *same* nesting-parity reader the vector fallback uses
(:func:`backend.geometry.silhouette.mask_to_geometry`), so a plate with a hole
is interpreted identically whichever path produced the pixels.

Everything this path returns is reported as
:attr:`backend.models.Method.RASTER_TRACE` and scores materially lower
confidence: the linework has been through a scanner and a threshold, and its
true edge is only known to within a pixel and a blur radius.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import cv2
import numpy as np
from shapely.geometry.base import BaseGeometry

from backend.config import RASTER, RasterSettings
from backend.geometry.silhouette import ScanFrame, mask_to_geometry
from backend.models import BBox, Repair


def render_page_mask(
    page, bbox: Optional[BBox], settings: RasterSettings = RASTER
) -> Tuple[np.ndarray, ScanFrame]:
    """Render a page (or a region of it) and binarise the ink.

    Args:
        page: A ``fitz.Page``.
        bbox: Region of interest in PDF units, or ``None`` for the whole page.
        settings: Raster path settings.

    Returns:
        ``(ink_mask, frame)`` where non-zero mask pixels are linework and
        ``frame`` maps pixels back to PDF units.
    """
    import fitz

    zoom = settings.dpi / 72.0
    clip = fitz.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1) if bbox else page.rect
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)

    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if pixmap.n >= 3 else image[:, :, 0]

    block = settings.threshold_block if settings.threshold_block % 2 == 1 else settings.threshold_block + 1
    ink = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, settings.threshold_offset
    )

    frame = ScanFrame(
        origin=(float(clip.x0), float(clip.y0)),
        pixel_size=1.0 / zoom,
        width=pixmap.width,
        height=pixmap.height,
    )
    return ink, frame


def trace_page(
    page,
    bbox: Optional[BBox],
    min_polygon_area: float,
    settings: RasterSettings = RASTER,
) -> Tuple[Optional[BaseGeometry], List[Repair], List[str]]:
    """Recover a silhouette from a rendered raster page.

    Args:
        page: A ``fitz.Page``.
        bbox: Region of interest in PDF units, or ``None`` for the whole page.
        min_polygon_area: Minimum face area in PDF units².
        settings: Raster path settings.

    Returns:
        ``(geometry, repairs, notes)``.
    """
    ink, frame = render_page_mask(page, bbox, settings)
    notes = [
        f"rendered at {settings.dpi} dpi ({frame.width}x{frame.height} px, "
        f"{frame.pixel_size:.4f} PDF units/px)"
    ]
    repairs: List[Repair] = []

    kernel_size = max(settings.close_kernel, 1)
    if kernel_size > 1:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel)
        repairs.append(
            Repair(
                type="morphological_gap_close",
                count=1,
                magnitude_units=round(kernel_size * frame.pixel_size, 4),
                detail=f"closed scan gaps up to {kernel_size} px before tracing",
            )
        )

    page_area_px = float(frame.width * frame.height)
    min_area_px = settings.min_area_fraction * page_area_px
    min_area_units = max(min_polygon_area, min_area_px * frame.pixel_size ** 2)

    geometry = mask_to_geometry(
        ink,
        frame,
        min_polygon_area=min_area_units,
        stroke_inset_pixels=0.5,
        simplify_pixels=1.0,
    )
    if geometry is None:
        notes.append("no enclosed region survived thresholding")
    else:
        notes.append(
            "boundary accuracy is limited by render resolution and threshold; "
            f"one pixel is {frame.pixel_size:.4f} PDF units"
        )
    return geometry, repairs, notes
