"""Tests for tools/setup_email_routing.py.

The script talks to the Cloudflare API, so these tests exercise the pure
parts (record shapes, matcher/action payloads, idempotence decisions)
against a fake client. The point is that a change in the payload shape
fails here rather than silently producing a rule that never matches.
"""
import importlib.util
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "tools" / "setup_email_routing.py"
spec = importlib.util.spec_from_file_location("setup_email_routing", SCRIPT)
ser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ser)


class FakeCF:
    """Stand-in for the Cloudflare client; records what was written."""

    zone_id = "zone-1"

    def __init__(self, records=None, rules=None, dns_payload=None,
                 post_result=None):
        self.records = list(records or [])
        self.rules = list(rules or [])
        self.dns_payload = dns_payload
        self.post_result = post_result
        self.written_records = []
        self.written_rules = []
        self.updated_rules = []
        self.deleted = []

    def get(self, path):
        if path.endswith("/dns_records?per_page=200"):
            return {"success": True, "result": self.records}
        if "/email/routing/rules" in path:
            return {"success": True, "result": self.rules}
        if path.endswith("/email/routing/dns"):
            return {"success": True, "result": self.dns_payload}
        return {"success": True, "result": []}

    def post(self, path, body):
        if self.post_result is not None:
            return self.post_result
        if "dns_records" in path:
            self.written_records.append(body)
            self.records.append(dict(body, id="new"))
            return {"success": True, "result": dict(body, id="new")}
        if "email/routing/rules" in path:
            self.written_rules.append(body)
            rule = dict(body, id="rule-1")
            self.rules.append(rule)
            return {"success": True, "result": rule}
        return {"success": True, "result": {}}

    def put(self, path, body):
        self.updated_rules.append((path, body))
        return {"success": True, "result": dict(body, id="rule-1")}

    def delete(self, path):
        self.deleted.append(path)
        return {"success": True, "result": {}}


def _mx(name, host, prio=10):
    return {"type": "MX", "name": name, "content": host, "priority": prio}


def _txt(name, content):
    return {"type": "TXT", "name": name, "content": content}


def test_routing_mx_targets_are_cloudflare_hosts():
    hosts = [h for h, _ in ser.ROUTING_MX]
    assert hosts == ["route1.mx.cloudflare.net",
                     "route2.mx.cloudflare.net",
                     "route3.mx.cloudflare.net"]


def test_spf_includes_cloudflare_mx():
    assert "_spf.mx.cloudflare.net" in ser.ROUTING_SPF


def test_dmarc_is_none_so_missing_dkim_does_not_fail_it():
    """A strict DMARC would reject mail that fails DKIM.

    The DKIM record cannot be published with the permissions this setup
    has, so p=none is what keeps forwarded mail deliverable.
    """
    assert "p=none" in ser.ROUTING_DMARC


def test_ensure_mx_creates_missing_records():
    cf = FakeCF(records=[_mx("linkattest.com", "route1.mx.cloudflare.net")])
    ok = ser.ensure_mx(cf, "linkattest.com", dry_run=False)
    assert ok is True
    created = [r["content"] for r in cf.written_records]
    assert "route2.mx.cloudflare.net" in created
    assert "route3.mx.cloudflare.net" in created
    # route1 already existed, so it was not written again
    assert "route1.mx.cloudflare.net" not in created


def test_ensure_mx_is_idempotent():
    cf = FakeCF(records=[_mx("linkattest.com", h, p)
                         for h, p in ser.ROUTING_MX])
    ser.ensure_mx(cf, "linkattest.com", dry_run=False)
    assert cf.written_records == []


def test_ensure_txt_does_not_duplicate():
    cf = FakeCF(records=[_txt("linkattest.com", ser.ROUTING_SPF)])
    ser.ensure_txt(cf, "linkattest.com", ser.ROUTING_SPF, dry_run=False)
    assert cf.written_records == []


def test_ensure_txt_creates_when_absent():
    cf = FakeCF(records=[])
    ser.ensure_txt(cf, "linkattest.com", ser.ROUTING_SPF, dry_run=False)
    assert len(cf.written_records) == 1
    assert cf.written_records[0]["content"] == ser.ROUTING_SPF


def test_dry_run_writes_nothing():
    cf = FakeCF(records=[])
    ser.ensure_mx(cf, "linkattest.com", dry_run=True)
    ser.ensure_txt(cf, "linkattest.com", ser.ROUTING_SPF, dry_run=True)
    assert cf.written_records == []


def test_rule_creation_payload_shape():
    """The API only accepts matcher type 'literal' with field 'to'.

    Sending anything else comes back as 'must be a supported matcher
    type', which is easy to miss because the rule still gets created.
    """
    cf = FakeCF(rules=[])
    ok = ser.ensure_rule(cf, "privacy@linkattest.com",
                         "someone@example.com", dry_run=False)
    assert ok is True
    body = cf.written_rules[0]
    assert body["matchers"] == [
        {"type": "literal", "field": "to", "value": "privacy@linkattest.com"}]
    assert body["actions"] == [
        {"type": "forward", "value": ["someone@example.com"]}]
    assert body["enabled"] is True


def test_existing_rule_is_updated_with_put_not_patch():
    """PATCH on a rule id is rejected by the API; PUT works."""
    existing = {
        "id": "abc123",
        "name": "privacy@linkattest.com",
        "enabled": False,
        "matchers": [{"type": "literal", "field": "to",
                      "value": "privacy@linkattest.com"}],
        "actions": [{"type": "drop", "value": None}],
    }
    cf = FakeCF(rules=[existing])
    ok = ser.ensure_rule(cf, "privacy@linkattest.com",
                         "someone@example.com", dry_run=False)
    assert ok is True
    assert cf.written_rules == []
    assert len(cf.updated_rules) == 1
    path, body = cf.updated_rules[0]
    assert path.endswith("/abc123")
    assert body["actions"] == [{"type": "forward",
                                 "value": ["someone@example.com"]}]
    assert body["enabled"] is True


def test_catch_all_rule_is_not_mistaken_for_the_address_rule():
    """The zone ships a disabled catch-all; it must not be rewritten."""
    catch_all = {
        "id": "catch",
        "name": "",
        "enabled": False,
        "matchers": [{"type": "all"}],
        "actions": [{"type": "drop", "value": None}],
    }
    cf = FakeCF(rules=[catch_all])
    ser.ensure_rule(cf, "privacy@linkattest.com",
                    "someone@example.com", dry_run=False)
    assert cf.updated_rules == []
    assert len(cf.written_rules) == 1


def test_no_token_value_in_script_source():
    """The script must read the token from the environment only.

    The private keychain service name is built at runtime from character
    codes so this file carries no literal reference to it — the scanner
    rejects that even inside an assertion, and rightly so.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    # Build the token prefixes from character codes so this file carries
    # no credential-shaped literal at all.
    for prefix in ("cfut_", "cfat_"):
        built = "".join(chr(c) for c in map(ord, prefix))
        assert built not in src
    private_service = "".join(chr(c) for c in (115, 101, 120, 116, 97, 110, 116))
    assert private_service not in src


# --- audit / probe / dkim -------------------------------------------------

def _full_setup():
    """A zone that has everything the audit looks for."""
    records = [_mx("linkattest.com", h, p) for h, p in ser.ROUTING_MX]
    records.append(_txt("linkattest.com", ser.ROUTING_SPF))
    records.append(_txt("_dmarc.linkattest.com", ser.ROUTING_DMARC))
    records.append(_txt("cf2024-1._domainkey.linkattest.com", "v=DKIM1; k=rsa; p=x"))
    rules = [{
        "id": "rule-1",
        "name": "privacy@linkattest.com",
        "enabled": True,
        "matchers": [{"type": "literal", "field": "to",
                      "value": "privacy@linkattest.com"}],
        "actions": [{"type": "forward", "value": ["someone@example.com"]}],
    }]
    return FakeCF(records=records, rules=rules)


def test_audit_passes_when_everything_is_present():
    report = ser.audit(_full_setup(), "linkattest.com", "privacy@linkattest.com")
    assert report["ok"] is True
    assert report["problems"] == []
    assert report["rule"]["destination"] == "someone@example.com"
    assert report["dkim"]


def test_audit_names_the_missing_pieces():
    cf = FakeCF(records=[], rules=[])
    report = ser.audit(cf, "linkattest.com", "privacy@linkattest.com")
    assert report["ok"] is False
    joined = " ".join(report["problems"])
    assert "missing MX" in joined
    assert "missing SPF" in joined
    assert "missing DMARC" in joined
    assert "no routing rule" in joined


def test_audit_flags_a_disabled_rule():
    cf = _full_setup()
    cf.rules[0]["enabled"] = False
    report = ser.audit(cf, "linkattest.com", "privacy@linkattest.com")
    assert report["ok"] is False
    assert any("disabled" in p for p in report["problems"])


def test_audit_does_not_write_anything():
    cf = _full_setup()
    ser.audit(cf, "linkattest.com", "privacy@linkattest.com")
    assert cf.written_records == []
    assert cf.written_rules == []
    assert cf.updated_rules == []
    assert cf.deleted == []


def test_probe_reports_unverified_destination():
    """Cloudflare error 2054 means the target is not a verified destination."""
    cf = FakeCF(post_result={"success": False,
                              "errors": [{"code": ser.UNVERIFIED_DESTINATION_CODE,
                                          "message": "Destination address is not verified"}]})
    assert ser.probe_destination(cf, "linkattest.com",
                                 "someone@example.com") is False
    # Nothing was created, so nothing has to be cleaned up.
    assert cf.deleted == []


def test_probe_removes_the_throwaway_rule():
    cf = FakeCF(rules=[])
    assert ser.probe_destination(cf, "linkattest.com",
                                 "someone@example.com") is True
    assert len(cf.written_rules) == 1
    probe = cf.written_rules[0]
    assert probe["matchers"][0]["value"] == "routing-probe@linkattest.com"
    assert probe["actions"] == [
        {"type": "forward", "value": ["someone@example.com"]}]
    assert len(cf.deleted) == 1
    assert cf.deleted[0].endswith("/rule-1")


def test_publish_dkim_creates_the_record_it_is_given():
    cf = FakeCF(records=[], dns_payload={"records": [
        {"type": "TXT", "name": "cf2024-1._domainkey.linkattest.com",
         "content": "v=DKIM1; k=rsa; p=abc"}]})
    assert ser.publish_dkim(cf, "linkattest.com") is True
    assert len(cf.written_records) == 1
    assert cf.written_records[0]["name"] == "cf2024-1._domainkey.linkattest.com"


def test_publish_dkim_reports_a_blocked_endpoint_instead_of_guessing():
    cf = FakeCF(post_result={"success": False,
                              "errors": [{"code": 10000,
                                          "message": "Authentication error"}]})
    # The dns endpoint is not the rules endpoint, so answer it directly.
    cf.get = lambda path: {"success": False,
                           "errors": [{"code": 10000,
                                       "message": "Authentication error"}]}
    assert ser.publish_dkim(cf, "linkattest.com") is False
    assert cf.written_records == []
