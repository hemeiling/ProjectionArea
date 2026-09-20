# Projected Area Analyzer — deployment image.
#
# Why a container rather than a platform's native Python build: this application
# has a system-level CAD dependency. Reading a DWG means running GNU LibreDWG's
# dwg2dxf, and no Debian or Ubuntu release packages LibreDWG at all (checked
# against packages.debian.org — no source or binary package of that name exists
# in any suite). A native build would therefore have to compile it inside the
# platform's build step, where the toolchain is whatever the base image happens
# to provide and a failure surfaces as a deploy that silently lost DWG support.
# Here the converter is built once, from a release tarball whose hash is checked,
# and the result is part of the image.
#
# Two stages: the compiler and the ~11 MB of LibreDWG sources stay in the
# builder, and the runtime gets one statically linked binary.
#
# Licence note: LibreDWG is GPL-3.0. It is invoked as a separate process and
# never linked into this application, which is aggregation rather than a derived
# work. The binary and its licence are shipped intact.

# ── stage 1: build the converter ─────────────────────────────────────────────
FROM debian:bookworm-slim AS converter

ARG LIBREDWG_VERSION=0.14
# Published at ftp.gnu.org; verified so a corrupted or substituted tarball fails
# the build rather than producing a converter nobody inspected.
ARG LIBREDWG_SHA256=62ebb73b984f865960f20ed26619ea5f8789d5e3fd088fa40a2598384da81275

RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
        ca-certificates \
        curl \
        pkg-config \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN curl -fsSL -o libredwg.tar.xz \
        "https://ftp.gnu.org/gnu/libredwg/libredwg-${LIBREDWG_VERSION}.tar.xz" \
    && echo "${LIBREDWG_SHA256}  libredwg.tar.xz" | sha256sum -c - \
    && tar -xf libredwg.tar.xz \
    && rm libredwg.tar.xz

# The release tarball ships a pre-generated ./configure, so no autotools are
# needed. Bindings, the Python module and the documentation are all disabled:
# this image needs one command-line tool. --disable-shared makes dwg2dxf carry
# libredwg itself, so the runtime stage needs no library path and cannot be
# broken by copying the binary without its .so.
WORKDIR /build/libredwg-${LIBREDWG_VERSION}
RUN ./configure \
        --prefix=/opt/libredwg \
        --disable-shared \
        --enable-static \
        --disable-bindings \
        --disable-python \
        --disable-docs \
        --disable-werror \
    && make -j"$(nproc)" \
    && make install \
    && strip /opt/libredwg/bin/dwg2dxf \
    && install -D -m 644 COPYING /opt/libredwg/share/licences/libredwg/COPYING \
    && /opt/libredwg/bin/dwg2dxf --version

# ── stage 2: the application ─────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS app

# PYTHONUNBUFFERED so log lines reach the platform's collector as they happen
# rather than when a buffer fills — during a four-minute DWG parse that is the
# difference between visible progress and apparent silence.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/libredwg/bin:${PATH}"

# libgomp1 is OpenMP's runtime, which the numpy and OpenCV wheels expect to find
# on the system. Everything else those wheels need they carry themselves — this
# is why the image installs opencv-python-headless and never the GUI build.
RUN apt-get update && apt-get install --no-install-recommends -y \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=converter /opt/libredwg/bin/dwg2dxf /opt/libredwg/bin/dwg2dxf
COPY --from=converter /opt/libredwg/share/licences/libredwg/COPYING \
                     /opt/libredwg/share/licences/libredwg/COPYING

WORKDIR /app

# Dependencies first, so a code change does not reinstall PyMuPDF and Shapely.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY cad-area-meter.html run.py ./

# Uploaded drawings are proprietary and the filesystem is ephemeral, so nothing
# is written outside the per-process temporary directory the store creates. The
# application runs as a non-root user that owns none of its own code.
RUN useradd --create-home --shell /usr/sbin/nologin analyst \
    && chown -R analyst:analyst /app
USER analyst

# Fails the build if the image cannot do the thing it exists for.
RUN python -c "\
from backend.cad.dwg import find_converter; \
c = find_converter(); \
assert c is not None, 'no DWG converter on PATH in the image'; \
print('DWG converter:', c.tool, c.version)"

EXPOSE 8000

# The platform assigns the port; ${PORT:-8000} keeps `docker run` usable without
# one. Shell form, because the variable has to be expanded at start time.
CMD python -m uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
