"""Page-level tests: every module renders for every religion."""
import pytest

SPIRITUAL_CARD_TEXT = {
    "hindu": "Today's deity, mantra, vrat",
    "islam": "Prayer times, Hijri date",
    "christianity": "Season, feasts, reflection",
    "sikh": "Simran, Gurbani, Gurpurabs",
    "atheist": "A quiet moment, today's observance",
}


@pytest.fixture()
def client_for(client, fake_store):
    """Sign in a fresh user that follows the given religion."""
    def make(religion):
        user = fake_store.create_user(f"{religion}@example.com", religion.title(), "password123", religion=religion)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user["id"])
            sess["_fresh"] = True
        return client
    return make


@pytest.mark.parametrize("religion", SPIRITUAL_CARD_TEXT)
def test_home_shows_religion_specific_spiritual_card(client_for, religion):
    page = client_for(religion).get("/").get_data(as_text=True)
    assert SPIRITUAL_CARD_TEXT[religion] in page
    # none of the other religions' wording leaks in
    for other, text in SPIRITUAL_CARD_TEXT.items():
        if other != religion:
            assert text not in page


@pytest.mark.parametrize("religion", SPIRITUAL_CARD_TEXT)
def test_home_guidance_card_never_shows_placeholder_text(client_for, religion):
    page = client_for(religion).get("/").get_data(as_text=True)
    assert "Pending Panchang API" not in page


@pytest.mark.parametrize("path", ["/", "/spiritual", "/mental", "/dashboard", "/emotional"])
@pytest.mark.parametrize("religion", SPIRITUAL_CARD_TEXT)
def test_every_page_renders_for_every_religion(client_for, religion, path):
    assert client_for(religion).get(path).status_code == 200


def test_pages_require_login(client):
    for path in ("/", "/spiritual", "/mental", "/dashboard", "/emotional"):
        assert client.get(path).status_code == 302


def test_unknown_page_renders_friendly_404(client):
    res = client.get("/no-such-page")
    assert res.status_code == 404 and b"drifted out of view" in res.data


class TestChatPage:
    def test_chat_page_has_safety_notes_hidden_until_needed(self, client_for):
        page = client_for("hindu").get("/emotional").get_data(as_text=True)
        assert 'id="crisis-banner"' in page and 'id="support-note"' in page
        assert page.count("hidden>") >= 2  # both start hidden
        assert "third-party AI service" in page  # honest data-sharing notice stays visible

    def test_chat_page_is_accessible_and_has_no_inline_html_sinks(self, client_for):
        page = client_for("sikh").get("/emotional").get_data(as_text=True)
        assert 'role="log"' in page and 'class="skip-link"' in page and 'id="main"' in page
        assert "innerHTML" not in page  # model output must only ever be inserted as text

    def test_every_class_the_chat_page_uses_is_styled(self, client_for):
        import re
        from pathlib import Path
        css = (Path(__file__).resolve().parent.parent / "static" / "styles.css").read_text(encoding="utf-8")
        page = client_for("atheist").get("/emotional").get_data(as_text=True)
        used = {c for m in re.findall(r'class="([^"]*)"', page) for c in m.split()}
        unstyled = {c for c in used if f".{c}" not in css} - {"no-entries-msg", "journal-form", "card"}
        assert unstyled == set(), unstyled
