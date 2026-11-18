#!/usr/bin/env python3
"""Private-information scan — pre-commit gate (DESIGN.md §9.2 HARD RULE).

Scans files about to be committed for private data: personal emails,
hardcoded passwords/API keys, and known credential strings (detected via
SHA-256 digests only — this scanner never embeds plaintext credentials).

Exit 1 if anything suspicious is found.
中文：提交前私有信息扫描，命中即拒绝提交。本扫描器只存凭据摘要，绝不存明文。

Usage:
  python3 tools/private_scan.py [paths...]     # default: all non-ignored files
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

# Patterns for textual private data. Note: *references* to env-file paths in
# config/docs are fine (a path is not a secret) — we flag assignments/values.
PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@(?:gmail|qq|163|126|outlook|hotmail)\.[a-z]+",
                re.I), "personal email address"),
    (re.compile(r"(?i)(password|passwd|app_password)\s*[=:]\s*['\"][^'\"]{8,}"),
     "hardcoded password"),
    (re.compile(r"(?i)(api[_-]?key|secret|token)\s*[=:]\s*['\"][A-Za-z0-9]{16,}"),
     "hardcoded API key/secret"),
    (re.compile(r"(?i)_APP_PASSWORD\s*=\s*\S+"), "app password assignment"),
]

# SHA-256 digests of known credential strings (plaintext lives only in the
# vault outside this repo). Any repo token hashing to one of these = leak.
KNOWN_CRED_SHA256 = {
    "0cea06caab14415f57ca7b4854d29841f86d65e9a461df6a2d14abb89879bf4f",
    "899cc494c6ea4b7cbd376e60ef6c9f7f885169b2d5b03d66e7d924628487a73a",
    "e623214a68bce9fd515c934ab7745ec7c1efa2b2c4df5b5f894817ca149b11e8",
}

ALLOW_EXTS = {".py", ".sh", ".yaml", ".yml", ".md", ".json", ".toml", ".txt"}
TOKEN_RE = re.compile(r"[A-Za-z0-9]{16,}")


def _known_cred_token(line: str) -> bool:
    for tok in TOKEN_RE.findall(line):
        if hashlib.sha256(tok.encode()).hexdigest() in KNOWN_CRED_SHA256:
            return True
    return False


def tracked_files(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p) for p in paths]
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        # not a git repo yet — fall back to scanning the whole tree
        return [p for p in Path(".").rglob("*") if p.is_file()
                and ".git" not in p.parts and "data" not in p.parts]
    return [Path(p) for p in out.stdout.splitlines()]


def scan(paths: list[str]) -> int:
    files = tracked_files(paths)
    hits = 0
    for path in files:
        if path.suffix.lower() not in ALLOW_EXTS:
            continue
        if any(part in ("data", ".git", "__pycache__") for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            label = None
            for pattern, pat_label in PATTERNS:
                if pattern.search(line):
                    label = pat_label
                    break
            if label is None and _known_cred_token(line):
                label = "known credential string"
            if label:
                print(f"[PRIVATE] {path}:{line_no}: {label}")
                print(f"    {line.strip()[:120]}")
                hits += 1
    if hits:
        print(f"\nFAIL: {hits} private-information hit(s). Commit blocked.")
        return 1
    print("private-scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(scan(sys.argv[1:]))
