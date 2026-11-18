#!/usr/bin/env python3
"""Private-information scan — pre-commit gate (DESIGN.md §9.2 HARD RULE).

Scans files about to be committed for private data:
  * personal emails
  * hardcoded passwords / API keys / secrets
  * known credential strings  (detected via SHA-256 digests only)
  * known private domains     (own sites + competitors; digests only)

This scanner never embeds plaintext credentials or domain names — the digest
sets below are one-way hashes maintained by the operator.
中文：提交前私有信息扫描，命中即拒绝提交。扫描器只存凭据/域名的 SHA256 摘要，绝不存明文。

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

# SHA-256 digests of private domain names (lowercase, no www.). File NAMES and
# content must both stay domain-free; filenames are checked separately.
KNOWN_PRIVATE_DOMAIN_SHA256 = {
    "f7de16800736df61cf5f97bf271e630d795c538681d57ffffd3b2f03defb2c31",
    "ab6c687996af1ffe907d8dc122f3872da9c8797aaea49b95c77f42e2a79d476d",
    "d2341c31a665156c5d36213fb0711138279099cc3bf9ac769782c35f62fe82de",
    "86f6b8452ef689c79bfecbfc152e8e88e2bdbc297dab133e54f2e1cc320d00f7",
    "485177c8c6fabd6b828380bcbc530888c1e312fc4b568d618df1485be5069f65",
    "186b8507e064e3d41a6e1ce8ebfb22d4fbf1e0cac5d508d0b3e6085de403d287",
    "92faa571c58cb69596e67fe0d9b39f67babe208e4d883cc24dbd3607d7055e8a",
    "111e47ee27b925114d83ba7d0ee9af425542ad6ba7636fd3481ac775ee7d710f",
    "cd4bbc0e8322bf9c531b09521895fd3da406c3cfe5a151fc40cfb142d4915ae1",
    "fc3603d552f20baa7352475d7b5005c0dde289fd83e61cd76f2470950a3fddbe",
}

# Generic placeholder domains that are always allowed in code/docs.
DOMAIN_TOKEN_RE = re.compile(
    r"(?<![\w.@-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(?![\w-])",
    re.I,
)

ALLOW_EXTS = {".py", ".sh", ".yaml", ".yml", ".md", ".json", ".toml", ".txt"}
TOKEN_RE = re.compile(r"[A-Za-z0-9]{16,}")


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()


def _known_cred_token(line: str) -> bool:
    return any(_digest(tok) in KNOWN_CRED_SHA256 for tok in TOKEN_RE.findall(line))


def _private_domain_token(line: str) -> str | None:
    for m in DOMAIN_TOKEN_RE.finditer(line):
        dom = m.group(1).lower()
        if dom.removeprefix("www.") in ("example.com", "example.org"):
            continue
        if _digest(dom) in KNOWN_PRIVATE_DOMAIN_SHA256:
            return m.group(1)
    return None


def _check_filename(path: Path) -> str | None:
    for token in DOMAIN_TOKEN_RE.findall(path.name):
        if _digest(token) in KNOWN_PRIVATE_DOMAIN_SHA256:
            return token
    return None


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

    # 1) filename check (rule: file names must be domain-free)
    for path in files:
        dom = _check_filename(path)
        if dom:
            print(f"[PRIVATE] filename contains private domain: {path}")
            hits += 1

    # 2) content check
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
            if label is None:
                dom = _private_domain_token(line)
                if dom:
                    label = f"private domain ({dom})"
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
