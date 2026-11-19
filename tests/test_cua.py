"""CUA form-fill and success-detection regressions.

French directory targets (pcastuces) use Nom / E-mail / commentaire; the old
name heuristic left `Nom` blank and English-only success hints missed French
confirmation pages. Both produced false `submitted` rows.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from osched import cua  # noqa: E402

CTX = {
    "title": "AZ Games",
    "sender_name": "Jane Doe",
    "description": "Play free browser games.",
    "email": "jane@example.com",
    "url": "https://azgames.example/",
}


class FakeInput:
    def __init__(self, name, typ="text", visible=True):
        self._name, self._type, self._visible = name, typ, visible
        self.value = ""

    def get_attribute(self, key):
        return {"name": self._name, "type": self._type}.get(key)

    def is_visible(self):
        return self._visible

    def fill(self, value):
        self.value = value


class FakePage:
    def __init__(self, inputs):
        self._inputs = inputs

    def query_selector_all(self, sel):
        return self._inputs if sel == "form input, form textarea" else []


def test_french_field_names_are_filled():
    nom = FakeInput("Nom")
    email = FakeInput("E-mail")
    url = FakeInput("URL")
    comment = FakeInput("commentaire")
    page = FakePage([nom, email, url, comment])

    assert cua._fill_form(page, CTX, "https://dir.example") is True
    assert nom.value == "Jane Doe"          # not left blank
    assert email.value == "jane@example.com"
    assert url.value == "https://azgames.example/"
    assert comment.value == "Play free browser games."


def test_domain_as_sender_name_uses_site_label():
    ctx = dict(CTX, sender_name="***REMOVED-PRIVATE-DOMAIN***",
               site_name="AZ Games")
    nom = FakeInput("Nom")
    cua._fill_form(FakePage([nom]), ctx, "https://dir.example")
    assert nom.value == "AZ Games"
    assert "." not in nom.value   # never a bare domain in a name field


def test_hidden_fields_skipped():
    hidden = FakeInput("Nom", visible=False)
    page = FakePage([hidden])
    assert cua._fill_form(page, CTX, "https://dir.example") is False
    assert hidden.value == ""


def test_french_success_hint_detected():
    assert cua._looks_successful_type("Merci, votre site a été ajouté") is True
    assert cua._looks_successful_type(
        "Votre message nous a bien été transmis") is True
    assert cua._looks_successful_type("Just a moment...") is False
    # bare "merci" appears in errors — must NOT count as success
    assert cua._looks_successful_type("Merci de préciser votre requête") is False


def test_pick_form_uses_submit_form_not_search():
    """The site search form (single q) must not win over the submission form."""
    class F:
        def __init__(self, names):
            self._in = [FakeInput(n) for n in names]

        def query_selector_all(self, sel):
            return self._in if sel == "input, textarea" else []

    search = F(["q"])
    submit = F(["Nom", "E-mail", "URL", "commentaire"])

    class P:
        def query_selector_all(self, sel):
            return [search, submit] if sel == "form" else []

    assert cua._pick_form(P()) is submit


def test_classic_directory_class_field_filled_with_category():
    """cam111 /url-submit: name+siteurl+email+class, all JS-required.
    Leaving `class` blank trips the page's own "Bad Category!" validator."""
    ctx = dict(CTX, category="Games")
    name = FakeInput("name")
    url = FakeInput("siteurl")
    email = FakeInput("email")
    klass = FakeInput("class")
    page = FakePage([name, url, email, klass])

    assert cua._fill_form(page, ctx, "http://cam111.com/url-submit") is True
    assert klass.value == "Games"
    assert url.value == "https://azgames.example/"
    assert name.value == "Jane Doe"
