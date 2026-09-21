"""The measurement path is deterministic, and stays that way.

CONSTITUTION.md §7: AI may help interpret what a drawing *means* — which view is
which, what a label says, which candidate reading is plausible. It may never
produce a measurement. §3: never fabricate a number.

An audit answers "is there AI in here" for the day it was run. These tests answer
it for every day after, which is the part that matters: the first AI call added to
this codebase should fail a test, not pass review.

What is forbidden is narrow and specific: no model decides a dimension, a scale, a
unit, which geometry counts, a polygon boundary, an area, or the confidence in a
number. And no uploaded drawing may leave this application for a third party.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Every client library that would mean a model is being called.
AI_PACKAGES = frozenset({
    "anthropic", "openai", "azure", "google", "google_genai", "generativeai",
    "genai", "vertexai", "mistralai", "cohere", "ollama", "litellm", "langchain",
    "langchain_core", "llama_index", "transformers", "torch", "tensorflow",
    "sentence_transformers", "tiktoken", "replicate", "huggingface_hub", "boto3",
})

#: Model families and provider names. A model id in the source is the giveaway
#: that something is being asked rather than computed.
AI_MARKERS = (
    "anthropic", "claude-3", "claude-4", "claude-5", "claude-opus", "claude-sonnet",
    "claude-haiku", "gpt-3", "gpt-4", "gpt-5", "o1-preview", "o3-", "davinci",
    "gemini-1", "gemini-2", "gemini-pro", "gemini-flash", "llama-", "mistral-",
    "command-r", "text-embedding", "generativeai", "openai.com", "anthropic.com",
    "generativelanguage.googleapis.com", "api.openai", "bedrock-runtime",
)

#: Modules that would let the backend talk to anything other than its database.
#: psycopg reaches PostgreSQL and is expected; nothing else should reach anywhere.
NETWORK_MODULES = frozenset({
    "requests", "httpx", "aiohttp", "urllib.request", "urllib3", "http.client",
    "websockets", "grpc", "socket",
})

#: The only third-party origins the browser page may contact. PDF.js renders the
#: uploaded PDF *in the browser*; it is a script download, and no drawing content
#: is sent to it. Anything else appearing here is a finding.
ALLOWED_FRONTEND_ORIGINS = frozenset({
    "https://cdnjs.cloudflare.com",   # pdf.js and its worker
    "http://www.w3.org",              # the SVG namespace, not a request
})


def _tracked(*paths: str) -> list:
    """Files git tracks under the given paths — what actually ships."""
    out = subprocess.run(
        ["git", "ls-files", "--", *paths],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def _python_files(*paths: str) -> list:
    return [f for f in _tracked(*paths) if f.endswith(".py")]


def _imports(path: str) -> set:
    """Every module name imported by a file, top-level and inside functions."""
    with open(os.path.join(PROJECT_ROOT, path), encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


# ── no model is called ───────────────────────────────────────────────────────


def test_the_backend_imports_no_ai_client():
    """Including inside functions: a deferred import is still a call."""
    offenders = []
    for path in _python_files("backend"):
        for module in _imports(path):
            root = module.split(".")[0]
            if root in AI_PACKAGES:
                offenders.append(f"{path} imports {module}")
    assert not offenders, "AI client libraries in the measurement path:\n" + "\n".join(offenders)


def test_no_provider_or_model_name_appears_in_the_runtime():
    offenders = []
    for path in _tracked("backend", "frontend", "run.py"):
        full = os.path.join(PROJECT_ROOT, path)
        try:
            with open(full, encoding="utf-8") as handle:
                content = handle.read().lower()
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        for marker in AI_MARKERS:
            if marker in content:
                offenders.append(f"{path} mentions {marker!r}")
    assert not offenders, "\n".join(offenders)


def test_no_ai_client_is_a_declared_dependency():
    with open(os.path.join(PROJECT_ROOT, "requirements.txt"), encoding="utf-8") as handle:
        lines = [
            line.split("#")[0].strip().lower()
            for line in handle if line.split("#")[0].strip()
        ]
    for line in lines:
        name = line.split(">")[0].split("=")[0].split("[")[0].strip()
        assert name.replace("-", "_") not in AI_PACKAGES, f"{name} is an AI client"


def test_no_ai_client_is_installed_in_this_environment():
    """Not proof about production, but it catches a dependency added by hand and
    then relied on — which is how an undeclared import reaches a deployment."""
    import importlib.util

    present = [
        name for name in sorted(AI_PACKAGES)
        if importlib.util.find_spec(name) is not None
    ]
    assert not present, f"AI clients importable here: {present}"


# ── nothing can leave the application ───────────────────────────────────────


def test_the_backend_reaches_nothing_but_its_own_database():
    """The only outbound connection this application may make is to PostgreSQL.

    An uploaded drawing is proprietary (§35). The way it would leak is an HTTP
    client appearing in a module that handles geometry, so no HTTP client may
    appear in the backend at all. ``backend/db`` reaches PostgreSQL through psycopg,
    which is not in this list.
    """
    offenders = []
    for path in _python_files("backend"):
        for module in _imports(path):
            if module in NETWORK_MODULES or module.split(".")[0] in NETWORK_MODULES:
                offenders.append(f"{path} imports {module}")
    assert not offenders, (
        "the backend can reach the network from:\n" + "\n".join(offenders)
    )


def test_the_browser_page_contacts_no_unexpected_origin():
    """The page may fetch PDF.js. It may not post a drawing anywhere."""
    import re

    offenders = []
    for path in _tracked("frontend"):
        full = os.path.join(PROJECT_ROOT, path)
        try:
            with open(full, encoding="utf-8") as handle:
                content = handle.read()
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        for match in re.finditer(r"https?://[a-zA-Z0-9.\-]+", content):
            origin = match.group(0)
            if origin not in ALLOWED_FRONTEND_ORIGINS and "localhost" not in origin:
                offenders.append(f"{path}: {origin}")
    assert not offenders, "unexpected origins in the page:\n" + "\n".join(offenders)


def test_the_api_the_page_calls_is_its_own_origin():
    with open(os.path.join(PROJECT_ROOT, "frontend/app.js"), encoding="utf-8") as handle:
        source = handle.read()
    assert "const API = location.origin;" in source, (
        "the page must call its own origin, so no request can carry a drawing "
        "to a third party"
    )


def test_the_only_process_the_backend_launches_is_the_cad_converter():
    """A subprocess is another way out. There is exactly one, and it is local."""
    import re

    launches = []
    for path in _python_files("backend"):
        with open(os.path.join(PROJECT_ROOT, path), encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if re.search(r"subprocess\.(run|Popen|call|check_\w+)", line):
                    launches.append(f"{path}:{number}")
    assert all(place.startswith("backend/cad/dwg.py") for place in launches), (
        f"processes are launched outside the CAD converter: {launches}"
    )


# ── no AI credential is needed, and none is read ─────────────────────────────


def test_no_ai_credential_is_read_anywhere_in_the_runtime():
    """The keys may exist in a developer's .env. Nothing reads them."""
    offenders = []
    for path in _python_files("backend", "run.py"):
        with open(os.path.join(PROJECT_ROOT, path), encoding="utf-8") as handle:
            content = handle.read()
        for key in ("CLAUDE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
                    "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
            if key in content:
                offenders.append(f"{path} references {key}")
    assert not offenders, "\n".join(offenders)


def test_the_application_measures_a_drawing_with_no_ai_credential_present(
    monkeypatch, drawings
):
    """The end-to-end guarantee: a real measurement, with every AI variable
    removed from the environment."""
    import time

    from fastapi.testclient import TestClient

    for key in ("CLAUDE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(key, raising=False)

    from backend.main import app

    truth = drawings["plate_with_holes"]
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        with open(truth["path"], "rb") as handle:
            blob = handle.read()
        job = client.post(
            "/api/analyse", files={"file": ("plate.pdf", blob, "application/pdf")},
        ).json()
        deadline = time.time() + 120
        while time.time() < deadline:
            snapshot = client.get(f"/api/jobs/{job['job_id']}").json()
            if snapshot["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert snapshot["state"] == "done", snapshot.get("error")

    area = snapshot["result"]["area"]
    union = next(f for f in area["footprint_interpretations"]
                 if f["type"] == "geometry_union")
    assert union["area_mm2"] == pytest.approx(truth["net_area_mm2"], rel=0.02), (
        "the measurement is arithmetic, and it is right, with no model involved"
    )


# ── the numbers come from geometry, not from a model ────────────────────────


def test_confidence_is_computed_from_measured_components():
    """§10: no arbitrary percentages, and nothing inferred. Every component is a
    number the pipeline measured, combined by a weighted geometric mean."""
    from backend.confidence import model as confidence_model

    source = open(confidence_model.__file__, encoding="utf-8").read().lower()
    for marker in AI_MARKERS:
        assert marker not in source
    for module in _imports(os.path.relpath(confidence_model.__file__, PROJECT_ROOT)):
        assert module.split(".")[0] not in AI_PACKAGES
    assert "geometric mean" in source, "the documented method"


@pytest.mark.parametrize("module_path", [
    "backend/area/projected.py",
    "backend/geometry/polygons.py",
    "backend/calibration/scale.py",
    "backend/pdf/primitives.py",
    "backend/cad/dxf.py",
    "backend/confidence/model.py",
])
def test_each_module_that_decides_a_number_is_deterministic(module_path):
    """The specific list from the brief: dimensions, scale, units, which geometry
    is included, polygon boundaries, area, and confidence. None may import a
    model client, reach the network, or launch a process."""
    full = os.path.join(PROJECT_ROOT, module_path)
    if not os.path.exists(full):
        pytest.skip(f"{module_path} does not exist")
    imports = _imports(module_path)
    for module in imports:
        root = module.split(".")[0]
        assert root not in AI_PACKAGES, f"{module_path} imports {module}"
        assert root not in NETWORK_MODULES, f"{module_path} imports {module}"
    with open(full, encoding="utf-8") as handle:
        content = handle.read()
    assert "subprocess" not in content, f"{module_path} launches a process"


def test_the_baseline_oracle_is_deterministic_too():
    """The correctness baselines are the reference future work is judged against.
    If anything inferential reached them, the reference itself would be unstable."""
    imports = _imports("tools/baseline.py")
    for module in imports:
        assert module.split(".")[0] not in AI_PACKAGES
        assert module.split(".")[0] not in NETWORK_MODULES
