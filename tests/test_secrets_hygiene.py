"""Secrets must never reach version control.

CONSTITUTION.md §35: this repository handles proprietary engineering data, and
the same care applies to credentials. `.env` holds live API keys; a single
`git add -A` is all it takes to publish them, and a key that reaches a commit
has to be rotated rather than deleted. These tests are the guard rail.

Nothing here prints a secret value — the assertions are about *shape* and
*tracking status* only.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Files that must never be tracked, even if one appears on disk.
FORBIDDEN_PATHS = (".env", ".env.local", ".env.production", "secrets.json", "credentials.json")

#: Credential shapes, matched against tracked file contents. Deliberately broad:
#: a false positive costs one review, a false negative costs a key rotation.
SECRET_PATTERNS = (
    (re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{20,}"), "Anthropic API key"),
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), "OpenAI-style API key"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{30,}"), "Google API key"),
    (re.compile(r"AQ\.[A-Za-z0-9_\-]{30,}"), "Google/Gemini short-lived key"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "GitHub personal access token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key"),
)

#: This file necessarily contains the patterns themselves.
SELF = os.path.basename(__file__)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
    ).stdout


def _is_git_repo() -> bool:
    return os.path.isdir(os.path.join(PROJECT_ROOT, ".git"))


requires_git = pytest.mark.skipif(not _is_git_repo(), reason="not a git repository")


@requires_git
@pytest.mark.parametrize("name", FORBIDDEN_PATHS)
def test_credential_files_are_ignored(name):
    """`.env` and friends must be ignored whether or not they exist yet.

    Asserting the ignore rule rather than the file's absence means the guard
    holds on a machine where no `.env` has been created yet.
    """
    result = subprocess.run(
        ["git", "check-ignore", "-q", name],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"{name} is not covered by .gitignore"


@requires_git
@pytest.mark.parametrize("name", FORBIDDEN_PATHS)
def test_credential_files_are_not_tracked(name):
    tracked = _git("ls-files", "--", name).strip()
    assert not tracked, f"{name} is tracked by git and must be removed from the index"


@requires_git
def test_no_tracked_file_contains_anything_shaped_like_a_credential():
    """Scan every tracked text file for credential shapes.

    Runs over the working tree rather than history: history is checked once by
    :func:`test_git_history_never_contained_a_credential_file`, and what matters
    day to day is that nothing new is about to be committed.
    """
    offenders = []
    for relative in _git("ls-files").splitlines():
        if not relative or os.path.basename(relative) == SELF:
            continue
        absolute = os.path.join(PROJECT_ROOT, relative)
        try:
            with open(absolute, "r", encoding="utf-8", errors="ignore") as handle:
                content = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(content):
                # Report the file and the kind, never the matched text.
                offenders.append(f"{relative}: looks like a {label}")

    assert not offenders, "credential-shaped strings in tracked files: " + "; ".join(offenders)


@requires_git
def test_git_history_never_contained_a_credential_file():
    """A key in any past commit still needs rotating, so check every commit."""
    names = set(FORBIDDEN_PATHS)
    seen = set()
    for line in _git("log", "--all", "--pretty=format:", "--name-only").splitlines():
        candidate = line.strip()
        if candidate in names:
            seen.add(candidate)
    assert not seen, f"credential files present in git history: {sorted(seen)} — rotate those keys"


def test_env_is_never_read_into_a_calculation_record():
    """§ reproducibility: audit records may name a provider, never a secret.

    Guards the rule before the AI layer exists, so the first adapter written
    cannot quietly serialise a key into a result.
    """
    suspicious = []
    for root, dirs, files in os.walk(os.path.join(PROJECT_ROOT, "backend")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, "r", encoding="utf-8") as handle:
                content = handle.read()
            for marker in ("API_KEY", "api_key", "SECRET", "TOKEN"):
                if marker in content and "as_dict" in content:
                    # Only a heuristic; tighten it if the AI layer lands here.
                    suspicious.append(f"{os.path.relpath(path, PROJECT_ROOT)} mentions {marker}")
    assert not suspicious, (
        "a module that serialises results also references credentials: "
        + "; ".join(suspicious)
    )
