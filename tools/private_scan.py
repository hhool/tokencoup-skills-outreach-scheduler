#!/usr/bin/env python3
"""Private-information scan — pre-commit gate (DESIGN.md §9.2 HARD RULE).

Scans files about to be committed for private data:
  * personal emails
  * hardcoded passwords / API keys / secrets
  * known credential strings      (detected via SHA-256 digests only)
  * known private domains         (own sites + competitors; digests only)
  * known private folder names    (other repos / tool dirs; digests only)
  * private-looking paths         (~/ dotfolders, absolute home paths)

This scanner never embeds plaintext credentials, domain names, or folder
names — the digest sets below are one-way hashes maintained by the operator.
中文：提交前私有信息扫描，命中即拒绝提交。扫描器只存 SHA256 摘要，绝不存明文。

Usage:
  python3 tools/private_scan.py [paths...]     # default: all non-ignored files

ALLOW vs HIT — read this before "fixing" a reported hit:
  KNOWN_PRIVATE_DOMAIN_SHA256 is a DENYLIST of our own private domain names
  (own sites, competitors, client domains). Digests only — the plaintext
  never appears here. Any repo token hashing to one of these is a leak and
  blocks the commit. There is no allowlist for domains: a domain is either
  private (in the set) or generic (example.com / invalid.local / a hostname
  with no dot). So a "private domain" hit is always real and must be
  removed from the text — never whitelisted away.

  Personal-email hits are also a denylist, with one deliberate exception:
  the LOCAL PART in PLACEHOLDER_LOCALPARTS (user, name, test, foo, …) plus
  1–2 char local parts are generic documentation stand-ins and pass. Any
  other mailbox on a consumer domain (gmail/qq/163/126/outlook/hotmail) is
  a real address and blocks the commit. The domain part is never excused.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

# Patterns for textual private data. Note: *references* to env-file paths in
# config/docs are fine (a path is not a secret) — we flag assignments/values.
#
# The email rule carries one explicit placeholder set, because a blind match
# on documentation makes the gate useless (every doc explaining an abuse
# pattern would trip it):
#
#   * personal emails — local parts in PLACEHOLDER_LOCALPARTS are generic
#     stand-ins; anything else on a consumer domain is a real mailbox.
#
# Domains have NO such escape hatch: KNOWN_PRIVATE_DOMAIN_SHA256 is a
# denylist and a hit is always real. See "ALLOW vs HIT" in the docstring.
PLACEHOLDER_LOCALPARTS = {
    "user", "username", "name", "you", "someone", "person", "test",
    "a", "b", "c", "foo", "bar", "foobar", "jane", "john", "doe",
    "example", "noreply",
}
PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net", "invalid.local",
    "test.local", "localhost", "localhost.localdomain",
}

# IPv4 ranges that are never a real host: loopback, this-host, private
# (RFC 1918), link-local, and the RFC 5737 documentation blocks used in
# examples and tests. Anything else in dotted-quad form is a public address
# and worth a human look — those are the ones that identify infrastructure.
_BENIGN_IPV4 = re.compile(
    r"^(?:"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|127\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|0\.0\.0\.0"
    r"|169\.254\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|192\.0\.2\.\d{1,3}"          # RFC 5737 TEST-NET-1
    r"|198\.51\.100\.\d{1,3}"       # RFC 5737 TEST-NET-2
    r"|203\.0\.113\.\d{1,3}"        # RFC 5737 TEST-NET-3
    r"|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"   # not IPv4 (5 octets)
    r")$")

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HEX32_RE = re.compile(r"\b[0-9a-f]{32}\b", re.I)
# IPv6: 2+ colon-separated groups, allowing a '::' shorthand. Requiring 2+
# colons keeps ordinary prose ("Note: see", "a:b") out of it.
_IPV6_RE = re.compile(
    r"(?<![\w:])[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?![\w:])", re.I)

# IPv6 literals are harder to enumerate; a real one in a public repo is rare
# enough that flagging every non-loopback/non-doc form is acceptable.
_BENIGN_IPV6_PREFIXES = ("2001:db8:",)      # RFC 3849 documentation block

PATTERNS = [
    (re.compile(r"([A-Za-z0-9._%+-]+)@(?:gmail|qq|163|126|outlook|hotmail)\.[a-z]+",
                re.I), "personal email address"),
    (re.compile(r"(?i)(password|passwd|app_password)\s*[=:]\s*['\"][^'\"]{8,}"),
     "hardcoded password"),
    (re.compile(r"(?i)(api[_-]?key|secret|token)\s*[=:]\s*['\"][A-Za-z0-9]{16,}"),
     "hardcoded API key/secret"),
    (re.compile(r"(?i)_APP_PASSWORD\s*=\s*\S+"), "app password assignment"),
    # Private-path rule (DESIGN.md §9.1): paths must stay inside this project
    # or a folder named after this project — never other private folders.
    # Built by concatenation so this source file itself stays pattern-clean.
    (re.compile("~/" + chr(92) + ".(?!well-known)"),
     "home dot-folder path: reference to another private folder"),
    (re.compile(r"(?<![\w.])/" + "(?:Users|home)" + r"/[A-Za-z0-9_]+"),
     "absolute home path (Users/home dir)"),
    # --- Infrastructure identifiers -------------------------------------
    # These are not secrets (nothing authenticates with them) but they are
    # account-scoped handles: with one of these plus a token from a breach,
    # someone can enumerate or re-point a zone. Keep them out of a public
    # repo. Values are operator-specific, so they cannot be a fixed pattern —
    # they go through KNOWN_INFRA_SHA256 below.
    (re.compile(r"\b(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12})\b", re.I),
     "identifier-shaped token (32 hex / uuid) — verify it is not an infra id"),
    # Cheap secrets that are *shapes*, not values: a bearer token or key
    # assigned to a shell variable without quotes still has to go.
    (re.compile(r"(?i)\b(?:CF|API)_[A-Z_]*(?:TOKEN|KEY|SECRET)\s*=\s*\S{8,}"),
     "hardcoded API token/key assignment"),
]

# SHA-256 digests of infrastructure identifiers (zone ids, account ids, fixed
# IPs, assigned name servers). A hit on one of these is a real leak of an
# account-scoped handle. Digests only — same rule as the domain set.
KNOWN_INFRA_SHA256 = {
    # operator-maintained: add the digest of each real id that must never
    # land in this repo. Empty means "the shape rules above are the only
    # net" — still catches anything that looks like a zone id or an IP.
}

EMAIL_LOCALPART_RE = re.compile(r"^([A-Za-z0-9._%+-]+)@"
                                r"(?:gmail|qq|163|126|outlook|hotmail)\.[a-z]+$", re.I)


def _email_localpart_is_placeholder(local: str) -> bool:
    """True when the local part is a generic stand-in, not a real mailbox.

    'user@gmail.com' / 'foo.bar+trial@gmail.com' are documentation. A real
    mailbox on a consumer domain is not, even when the local part looks like
    a name. Short local parts ('a@', 'u@', 'c1@') read as variables and are
    also excused; the domain part is never excused.
    """
    core = local.split("+", 1)[0]
    core = core.replace(".", "").replace("_", "").replace("-", "")
    if not core:
        return True
    if core.lower() in PLACEHOLDER_LOCALPARTS:
        return True
    if len(core) <= 2:          # 'a@', 'u@', 'c1@' — placeholder-ish
        return True
    return False


# SHA-256 digests of private domain names (lowercase, no www.). File NAMES and
# content must both stay domain-free; filenames are checked separately.
# This is a DENYLIST — see "ALLOW vs HIT" in the module docstring.

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

# SHA-256 digests of known private folder names (other repos / tool dirs).
# Digests only — the plaintext names never appear in this repo. Any token in
# a file name or content hashing to one of these = leak of a private path.
# NOTE: this project's own name (outreach-scheduler etc.) is intentionally
# NOT here — project-named folders are the allowed exception.
KNOWN_PRIVATE_FOLDER_SHA256 = {
    "951eafd24750bed963e1b55e3e2b858c90a7a7740004b6ac07b4bd23b347f1b3",
    "4613666d29e69b6b5296d15ed54daa32a4053b0f2630ceb671c2b2ff8d259f30",
    "0701e93b80541eb75b975d41bdd944cd901351c82546c795529e89376d98b904",
    "141de3607294359efa20824d76e730c9664ad02074e0531cbc79e4400b02be6a",
    "11457675a915f0ade000b741b36009ad309a700425ba9e64b59b7825f0080843",
    "4ba87ef1f6dfaaec24e0760c68325a938d9d8251c8857267c580fa2ea8d9a7bf",
}

# Token candidates for folder-name matching: words with hyphens/dots,
# lowercase-hashed before digest comparison.
FOLDER_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")

# Scanned extensions. `.html` is included deliberately: the docs site under
# docs/site/ is generated from the Markdown, so a leak scrubbed from the
# source comes back in the rendered page until the site is rebuilt. Leaving
# it out was how a zone id reached the public repo.
ALLOW_EXTS = {".py", ".sh", ".yaml", ".yml", ".md", ".json", ".toml", ".txt",
              ".html", ".htm", ".js", ".css", ".jsx", ".ts", ".tsx"}
TOKEN_RE = re.compile(r"[A-Za-z0-9]{16,}")


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()


def _known_cred_token(line: str) -> bool:
    return any(_digest(tok) in KNOWN_CRED_SHA256 for tok in TOKEN_RE.findall(line))


def _private_domain_token(line: str) -> str | None:
    for m in DOMAIN_TOKEN_RE.finditer(line):
        dom = m.group(1).lower()
        if dom.removeprefix("www.") in PLACEHOLDER_DOMAINS:
            continue
        # Denylist: a digest in the set is one of OUR private domains, so a
        # hit is always a real leak — never whitelist it away.
        if _digest(dom) in KNOWN_PRIVATE_DOMAIN_SHA256:
            return m.group(1)
    return None


def _looks_like_version(quad: str) -> bool:
    """True for dotted-quads that are version strings, not addresses.

    Browser and OS user-agent tokens use four-part versions like
    '131.0.0.0' or '537.36' padded with zeros. A real host essentially
    never has this shape: a plausible major version followed by all-zero
    remaining octets.
    """
    parts = quad.split(".")
    if len(parts) != 4:
        return False
    nums = [int(p) for p in parts]
    if nums[0] < 2 or nums[0] > 999:
        return False
    # major version, then at least the last two octets zero
    return nums[2] == 0 and nums[3] == 0 and nums[1] <= 99


def _infra_token(line: str) -> str | None:
    """A public IPv4/IPv6 literal, or a digest-matched infra id.

    Loopback, RFC 1918, link-local and the RFC 5737 documentation blocks are
    benign — they appear in code and tests constantly. A *public* address is
    a real host handle (it is how the fly.io ingress was leaked) and blocks.

    Version strings such as Chrome's "131.0.0.0" also look like a dotted
    quad; a 4-octet match whose first two octets are a plausible major
    version and whose tail is all zeros is treated as a version, not an IP.
    """
    for m in _IPV4_RE.finditer(line):
        candidate = m.group(0)
        if _BENIGN_IPV4.match(candidate):
            continue
        if _looks_like_version(candidate):
            continue
        return candidate
    for tok in _HEX32_RE.findall(line):
        if _digest(tok) in KNOWN_INFRA_SHA256:
            return tok
    # IPv6: skip loopback (::1) and the RFC 3849 documentation block; a
    # routable one is a host handle, same as its IPv4 counterpart.
    for m in _IPV6_RE.finditer(line):
        candidate = m.group(0).lower()
        if "::1" == candidate or candidate.startswith(_BENIGN_IPV6_PREFIXES):
            continue
        # an all-zero / unspecified address carries no information
        if candidate in ("::", "0:0:0:0:0:0:0:0"):
            continue
        return m.group(0)
    return None


def _private_folder_token(line: str) -> str | None:
    for tok in FOLDER_TOKEN_RE.findall(line):
        if _digest(tok) in KNOWN_PRIVATE_FOLDER_SHA256:
            return tok
    return None


def _check_filename(path: Path) -> str | None:
    for token in DOMAIN_TOKEN_RE.findall(path.name):
        if _digest(token) in KNOWN_PRIVATE_DOMAIN_SHA256:
            return token
    for tok in FOLDER_TOKEN_RE.findall(path.name):
        if _digest(tok) in KNOWN_PRIVATE_FOLDER_SHA256:
            return tok
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
                m = pattern.search(line)
                if not m:
                    continue
                # personal-email rule: let generic placeholders through
                if pat_label == "personal email address":
                    local = m.group(1) if m.groups() else ""
                    if _email_localpart_is_placeholder(local):
                        continue
                    label = f"personal email address ({m.group(0)})"
                else:
                    label = pat_label
                break
            if label is None and _known_cred_token(line):
                label = "known credential string"
            if label is None:
                dom = _private_domain_token(line)
                if dom:
                    label = f"private domain ({dom})"
            if label is None:
                fld = _private_folder_token(line)
                if fld:
                    label = f"private folder name ({fld})"
            if label is None:
                infra = _infra_token(line)
                if infra:
                    label = f"infrastructure address ({infra})"
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
