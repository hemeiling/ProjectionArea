"""The canonical page coordinate space, and the /Rotate normalisation into it.

CONSTITUTION.md §37: PyMuPDF is confined to ``backend/pdf/``, so this module is
where the single coordinate convention the rest of the engine is allowed to
assume gets established.

**Canonical space** is PyMuPDF *display* space: the space of ``page.rect``,
origin top-left, y increasing downwards, units of 1/72 inch, **with the page's
``/Rotate`` entry applied**. That is the space PDF.js paints at ``scale = 1``
and the space ``page.get_pixmap()`` rasterises, so a backend polygon and a
viewer overlay land on the same pixel with no axis flip and no rotation fix-up.

The catch: ``page.get_drawings()`` and ``page.get_text()`` do **not** honour
``/Rotate``. They report unrotated media-box coordinates, while ``page.rect``
reports rotated ones. On a ``/Rotate 90`` A3 sheet that combination is actively
wrong rather than merely inconsistent — the page is believed to be 842 x 1190
while the linework still spans 1190 x 842, so the sheet frame measures 138 % of
the believed page width but only 68 % of its height, fails the "spans most of
the page in both directions" test, keeps the ``profile`` role and is measured as
if it were the part. Region labelling loses the title block the same way, and
the returned geometry no longer lines up with what the viewer draws.

Everything read off a page is therefore mapped through :func:`page_space_matrix`
at this boundary. ``/Rotate`` is always a multiple of 90°, so the mapping is a
rigid rotation with determinant +1: lengths, angles, areas and polygon winding
are all preserved, and an axis-aligned box stays axis-aligned. For an unrotated
page the matrix is the identity and nothing changes at all.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Sequence, Tuple

import fitz

from backend.models import BBox, Point

#: Entries of the identity matrix, within float slop.
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_MATRIX_EPSILON = 1e-9


def page_space_matrix(page: Any) -> fitz.Matrix:
    """The matrix taking ``get_drawings``/``get_text`` output into page space.

    Args:
        page: A ``fitz.Page``.

    Returns:
        ``page.rotation_matrix`` — the identity for an unrotated page.
    """
    return fitz.Matrix(page.rotation_matrix)


def is_identity(matrix: Any) -> bool:
    """True when ``matrix`` leaves coordinates untouched.

    Lets callers skip the whole transform on the overwhelmingly common
    unrotated page, so the normalisation costs nothing there.
    """
    return all(abs(a - b) <= _MATRIX_EPSILON for a, b in zip(tuple(matrix), _IDENTITY))


def map_point(point: Point, matrix: Any) -> Point:
    """Map one ``(x, y)`` tuple into page space."""
    x, y = float(point[0]), float(point[1])
    a, b, c, d, e, f = tuple(matrix)
    return (a * x + c * y + e, b * x + d * y + f)


def map_points(points: Sequence[Point], matrix: Any) -> List[Point]:
    """Map a point sequence into page space, preserving order and winding."""
    a, b, c, d, e, f = tuple(matrix)
    return [(a * x + c * y + e, b * x + d * y + f) for x, y in points]


def map_bbox(bbox: BBox, matrix: Any) -> BBox:
    """Map an axis-aligned box into page space, re-normalised to min/max.

    A 90° rotation swaps which corner is which, so the transformed corners are
    re-bounded rather than assumed to still be ``(lower-left, upper-right)``.
    """
    corners = map_points(
        [(bbox.x0, bbox.y0), (bbox.x1, bbox.y0), (bbox.x1, bbox.y1), (bbox.x0, bbox.y1)],
        matrix,
    )
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return BBox(min(xs), min(ys), max(xs), max(ys))


def map_direction(direction: Tuple[float, float], matrix: Any) -> Tuple[float, float]:
    """Rotate a unit direction vector — the translation column is dropped."""
    a, b, c, d, _e, _f = tuple(matrix)
    x, y = float(direction[0]), float(direction[1])
    return (a * x + c * y, b * x + d * y)
