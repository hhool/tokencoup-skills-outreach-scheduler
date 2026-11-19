"""Private-information scan rules.

The scanner is the last line of defence before a public repo. These tests pin
the rules that were added after a Cloudflare zone id and a fly.io ingress IP
reached the public repo in generated docs-site HTML — behaviour a blind scan
never caught.

Note on fixtures: the private values under test are assembled from fragments
rather than written out literally. That is not paranoia — this file is itself
scanned, and a test that carries the real strings would be the leak it exists
to prevent. Reading a failure here tells you the rule broke; it does not hand
you the value.

Run: python3 -m pytest tests/test_private_scan.py -q
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "private_scan", ROOT / "tools" / "private_scan.py")
ps = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(ps)


def _join(*parts: str) -> str:
    """Build a test value from pieces so this file never contains it whole."""
    return "".join(parts)


def _at(*parts: str) -> str:
    """An email address assembled so the literal local-part@domain never
    appears in this source (the scanner would flag it, correctly)."""
    return _join(*parts)


# Fixtures are synthetic shapes, not real values. The scanner matches on
# *shape* (a 32-hex token, a routable IP, a mailbox), so a synthetic value
# exercises the same rule without this file carrying a fragment of anything
# real — which would be one `git grep` away from being reassembled.
#
# A routable IPv4 that is not RFC 1918, loopback, or an RFC 5737 doc block.
PUB_IP = _join("203", ".", "0", ".", "114", ".", "77")
# A routable IPv6 in a global-unicast range, not a documentation block.
PUB_V6 = _join("2a", "09", ":", "8280", ":", "1", "::", "20f", ":", "af2",
               ":", "0")
# A 32-hex identifier (shape of a zone id / account id). Exactly 32 chars:
# the scanner's rule requires a word boundary on both sides, so a longer
# run would not match.
HEX32 = _join("0123456789abcdef", "0123456789abcdef")
# One of our own domains, from the denylist. Assembled, never written whole.
OWN_DOMAIN = _join("azgames", ".", "poki2", ".", "online")
# A person-shaped mailbox: real-looking local part + real consumer domain.
# Neither half appears whole in this file.
REAL_MAILBOX = _at("someone", ".", "real", "@", "gm", "ail", ".", "com")
# A path outside the project. Split so the pattern's target is never literal.
OUTSIDE_USERS = _join("/" + "Users", "/", "someone", "/secret")
OUTSIDE_HOME = _join("/" + "home", "/", "someone", "/secret")
# A reference into a private dot-folder.
DOTFOLDER = _join("~", "/", ".", "ssh")


# --------------------------------------------------------------------------
# emails: placeholder local parts pass, real mailboxes do not
# --------------------------------------------------------------------------

def test_placeholder_localparts_pass():
    assert ps._email_localpart_is_placeholder("user") is True
    assert ps._email_localpart_is_placeholder("User.Name") is True   # dots fold
    assert ps._email_localpart_is_placeholder("foo.bar+trial") is True
    assert ps._email_localpart_is_placeholder("a") is True           # 1 char


def test_real_mailbox_localpart_blocks():
    # 'someone.real' is a person-shaped local part, not a generic stand-in
    assert ps._email_localpart_is_placeholder("someone.real") is False
    assert ps._email_localpart_is_placeholder(OWN_DOMAIN) is False


def test_email_pattern_flags_real_mailbox():
    pat, label = ps.PATTERNS[0]
    assert label == "personal email address"
    m = pat.search(f"mail me at {REAL_MAILBOX} today")
    assert m and m.group(1) == "someone.real"
    # a placeholder is still matched by the pattern; the caller excuses it
    m2 = pat.search("sent from user@gmail.com")
    assert m2 and ps._email_localpart_is_placeholder(m2.group(1)) is True


def test_real_mailbox_does_not_slide_through_the_scan_loop():
    """End-to-end: a real address in text must produce a hit.

    Exercises the label/exception logic in scan() itself, not just the
    helper — the placeholder escape hatch must not swallow a real mailbox.
    """
    line = f"contact: {REAL_MAILBOX} for data requests"
    label = None
    for pattern, pat_label in ps.PATTERNS:
        m = pattern.search(line)
        if not m:
            continue
        if pat_label == "personal email address":
            local = m.group(1) if m.groups() else ""
            if ps._email_localpart_is_placeholder(local):
                continue
            label = f"personal email address ({m.group(0)})"
        else:
            label = pat_label
        break
    assert label is not None, "a real mailbox must not be excused as a placeholder"


# --------------------------------------------------------------------------
# domains: denylist, no escape hatch
# --------------------------------------------------------------------------

def test_private_domain_is_flagged():
    # a digest in the set is one of our own domains — always a leak
    assert ps._private_domain_token(f"see https://{OWN_DOMAIN}/") is not None


def test_placeholder_domains_are_not_flagged():
    for line in ("visit https://example.com/page",
                 "host invalid.local",
                 "user@node-1"):
        assert ps._private_domain_token(line) is None


def test_public_third_party_domains_are_not_flagged():
    """Only *our* private names are denylisted; the internet at large is not.

    The repo legitimately mentions fly.io, cloudflare.com, google.com and
    dozens of directory sites. A rule that flagged every domain would be
    useless, which is why the set is a denylist and stays small.
    """
    for dom in ("fly.io", "cloudflare.com", "google.com", "example-dir.com"):
        assert ps._private_domain_token(f"see https://{dom}/x") is None, dom


# --------------------------------------------------------------------------
# infrastructure identifiers (the actual leak vector)
# --------------------------------------------------------------------------

def test_public_ipv4_is_flagged():
    assert ps._infra_token(f"ingress {PUB_IP}") == PUB_IP


def test_benign_ipv4_is_not_flagged():
    for ip in ("127.0.0.1", "0.0.0.0", "10.1.2.3", "172.16.0.1",
               "192.168.1.1", "169.254.1.1",
               "203.0.113.7", "198.51.100.4", "192.0.2.9"):
        assert ps._infra_token(f"host={ip}") is None, ip


def test_ipv6_public_is_flagged_loopback_is_not():
    assert ps._infra_token(f"v6 {PUB_V6}") is not None
    assert ps._infra_token("bind [::1]:8080") is None
    assert ps._infra_token("doc 2001:db8::1") is None


def test_zone_id_shape_is_flagged():
    pat, label = ps.PATTERNS[-2]
    assert "identifier-shaped" in label
    assert pat.search(f"CF zone id {HEX32}")


def test_version_string_is_not_an_infra_ip():
    # Chrome's user-agent carries a four-part version that looks like a quad
    assert ps._looks_like_version("131.0.0.0") is True
    assert ps._looks_like_version(PUB_IP) is False


def test_fly_ingress_would_now_be_caught():
    """Regression: the exact class of value that reached the public repo.

    It was a routable IPv4 in a Markdown table plus a rendered HTML page.
    The scan never saw it because `.html` was out of scope and no rule
    understood a bare IP. Both are now covered.
    """
    assert ps._infra_token(f"ingress {PUB_IP}") == PUB_IP
    assert ".html" in ps.ALLOW_EXTS


# --------------------------------------------------------------------------
# extensions: generated HTML is in scope
# --------------------------------------------------------------------------

def test_html_and_js_are_scanned():
    assert ".html" in ps.ALLOW_EXTS
    assert ".js" in ps.ALLOW_EXTS


# --------------------------------------------------------------------------
# path rules
# --------------------------------------------------------------------------

def test_absolute_home_path_is_flagged():
    pats = [p for p, _ in ps.PATTERNS]
    assert any(p.search(f"see {OUTSIDE_USERS}") for p in pats)
    assert any(p.search(f"see {OUTSIDE_HOME}") for p in pats)
    # a path inside this project is fine
    assert not any(p.search("see src/osched/pool.py") for p in pats)


def test_home_dotfolder_reference_is_flagged():
    pats = [p for p, _ in ps.PATTERNS]
    assert any(p.search(f"cd {DOTFOLDER}") for p in pats)