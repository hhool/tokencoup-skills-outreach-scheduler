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


class BodyPage:
    """Page stub exposing only content() — enough for rejection detection."""

    def __init__(self, html):
        self._html = html

    def content(self):
        return self._html


def test_validation_error_after_submit_reads_as_rejection():
    """launchingnext: required radio groups left blank re-render the form with
    an inline error at HTTP 200 — that must not be recorded as submitted."""
    page = BodyPage("<html><body><form><input name='startupname'>"
                    "<p class='error'>Please select a valid option.</p>"
                    "</form></body></html>")
    assert cua._rejection_reason(page)


def test_success_copy_never_reads_as_rejection():
    page = BodyPage("<html><body>Thanks, your submission has been received."
                    "</body></html>")
    assert cua._rejection_reason(page) == ""


def test_plain_page_without_signals_is_not_a_rejection():
    page = BodyPage("<html><body><h1>Launching Next</h1><p>Submit your "
                    "project below.</p></body></html>")
    assert cua._rejection_reason(page) == ""


class RadioInput(FakeInput):
    def __init__(self, name, value):
        super().__init__(name, typ="radio")
        self._value = value
        self.checked = False

    def get_attribute(self, key):
        if key == "value":
            return self._value
        return super().get_attribute(key)

    def check(self):
        self.checked = True


class RadioForm:
    def __init__(self, inputs):
        self._in = inputs

    def query_selector_all(self, sel):
        return self._in if sel == "input" else []


def test_honest_radio_option_is_chosen():
    """launchingnext requires funding + marketing budget; the honest answer is
    the 'none'/'$0' option, never an invented figure."""
    funding_none = RadioInput("funding", "0")
    funding_funded = RadioInput("funding", "3")
    budget_zero = RadioInput("marketing_budget", "$0")
    budget_big = RadioInput("marketing_budget", "$5,000 - $15,000")
    form = RadioForm([funding_funded, funding_none, budget_big, budget_zero])

    picked, left = cua._fill_radios(None, form=form)
    assert picked == 2 and left == []
    assert funding_none.checked and budget_zero.checked
    assert not funding_funded.checked and not budget_big.checked


def test_yes_no_group_is_never_guessed():
    """A group without a none/$0/free option must stay blank for a human."""
    yes = RadioInput("are_you_owner", "yes")
    no = RadioInput("are_you_owner", "no")
    form = RadioForm([yes, no])
    picked, left = cua._fill_radios(None, form=form)
    assert picked == 0 and left == ["are_you_owner"]
    assert not yes.checked and not no.checked


class LabelInput(FakeInput):
    """Text input that can report its <label> text, like a real DOM node."""

    def __init__(self, name, label=""):
        super().__init__(name)
        self._label = label

    def evaluate(self, _expr):
        return self._label


def test_arithmetic_gate_is_answered_from_its_label():
    """launchingnext asks 'Quick Check: What is 2+3?' as a required field."""
    math = LabelInput("math", "Quick Check: What is 2+3?")
    page = FakePage([math])
    assert cua._fill_form(page, CTX, "https://www.launchingnext.com/submit/")
    assert math.value == "5"


def test_arith_answer_parsing():
    assert cua._arith_answer("What is 7 + 4?") == 11
    assert cua._arith_answer("what is 9-2") == 7
    assert cua._arith_answer("Are you human?") is None


def test_user_field_gets_a_person_or_site_name():
    """`user` ("Your Name") matched none of the old branches and stayed blank,
    which the form then rejected."""
    user = LabelInput("user", "Your Name")
    page = FakePage([user])
    assert cua._fill_form(page, CTX, "https://dir.example")
    assert user.value == "Jane Doe"
