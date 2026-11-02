"""Page-level tests: every module renders for every religion."""
import pytest

RELIGIONS = ["hindu", "islam", "christianity", "sikh", "atheist"]
OLD_CARD_TEXT = (
    "Today's deity, mantra, vrat",
    "Prayer times, Hijri date",
    "Season, feasts, reflection",
    "Simran, Gurbani, Gurpurabs",
    "A quiet moment, today's observance",
)


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


@pytest.mark.parametrize("religion", RELIGIONS)
def test_home_shows_unboxed_guidance_for_every_religion(client_for, religion):
    page = client_for(religion).get("/").get_data(as_text=True)
    assert "Today’s Guidance" in page
    assert "Last check-in:" in page
    assert "Try:" in page
    assert "New Quote" in page
    for old_text in OLD_CARD_TEXT:
        assert old_text not in page


@pytest.mark.parametrize("religion", RELIGIONS)
def test_home_guidance_card_never_shows_placeholder_text(client_for, religion):
    page = client_for(religion).get("/").get_data(as_text=True)
    assert "Pending Panchang API" not in page


@pytest.mark.parametrize("path", ["/", "/spiritual", "/mental", "/dashboard", "/emotional"])
@pytest.mark.parametrize("religion", RELIGIONS)
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


class TestNoDeveloperPlaceholdersInTheUi:
    @pytest.mark.parametrize("religion", RELIGIONS)
    def test_spiritual_day_api_has_no_placeholder_text(self, client_for, religion):
        body = client_for(religion).get("/api/spiritual/day?date=2026-10-17").get_data(as_text=True)
        for leftover in ("Pending Panchang", "Pending location", "Mock fallback", "connect Prokerala"):
            assert leftover not in body, leftover

    def test_offline_hindu_panchang_shows_only_what_is_known(self, client_for):
        data = client_for("hindu").get("/api/spiritual/day?date=2026-10-17").get_json()
        panchang = data["daily"]["extra"]["panchangam"]
        assert panchang["weekday"] == "Saturday"
        assert not {"tithi", "nakshatra", "yoga", "karana", "sunrise", "sunset"} & set(panchang)
        assert "weekday" in panchang["source_note"]

    def test_home_guidance_chips_explain_themselves(self, client_for):
        page = client_for("hindu").get("/").get_data(as_text=True)
        assert "Today’s Guidance" in page
        assert "Last check-in:" in page and "Try:" in page

    def test_spiritual_page_script_is_valid_javascript(self, client_for, tmp_path):
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            pytest.skip("node is not installed")
        html = client_for("hindu").get("/spiritual").get_data(as_text=True)
        script = tmp_path / "page.js"
        script.write_text("\n".join(re.findall(r"<script>(.*?)</script>", html, re.S)), encoding="utf-8")
        result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


class TestSpiritualPageLayout:
    def page(self, client_for):
        return client_for("hindu").get("/spiritual").get_data(as_text=True)

    def test_layout_is_class_based_so_it_can_stack_on_phones(self, client_for):
        from pathlib import Path
        page = self.page(client_for)
        assert 'class="spiritual-layout"' in page
        assert "grid-template-columns: 1fr 320px" not in page  # inline styles beat media queries
        css = (Path(__file__).resolve().parent.parent / "static" / "styles.css").read_text(encoding="utf-8")
        assert "@media (max-width: 860px)" in css and ".spiritual-layout { grid-template-columns: 1fr; }" in css

    def test_single_top_nav_has_all_primary_links_and_no_bottom_nav(self, client_for):
        page = client_for("hindu").get("/").get_data(as_text=True)
        assert page.count("<nav") == 1
        assert 'class="topbar-nav"' in page
        assert 'class="bottom-nav"' not in page
        for label in ("Home", "Soul", "Mind", "Heart", "Sign Out"):
            assert label in page
        for href in ('href="/"', 'href="/spiritual"', 'href="/mental"', 'href="/emotional"'):
            assert href in page

    def test_calendar_does_not_derive_today_from_the_utc_date(self, client_for):
        import re
        assert not re.search(r"toISOString\(\)\s*\.slice", self.page(client_for))

    def test_calendar_marks_today_selection_and_festivals_even_when_utc_and_local_dates_differ(
            self, client_for, tmp_path):
        """Runs the page's real renderCalendar in node against a tiny fake DOM. The fake clock reports
        2:00am on the 17th locally while its UTC date is still the 16th (what India sees overnight)."""
        import json
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            pytest.skip("node is not installed")
        script = "\n".join(re.findall(r"<script>(.*?)</script>", self.page(client_for), re.S))
        pad = re.search(r"function pad.*?\n", script).group(0)
        iso = re.search(r"function isoOf.*?\n", script).group(0)
        cal = re.search(r"function renderCalendar.*?\n}\n", script, re.S).group(0)
        harness = """
const RealDate = Date;
global.Date = class extends RealDate {
  constructor(...a) { const none = a.length === 0; super(...(none ? [2026, 9, 17, 2, 0] : a));
    if (none) this.toISOString = () => "2026-10-16T20:30:00.000Z"; }
};
const els = {};
const mk = () => ({ children: [], style: {}, attrs: {}, className: "", textContent: "", title: "",
  appendChild(c) { this.children.push(c); }, setAttribute(k, v) { this.attrs[k] = v; },
  addEventListener() {}, set innerHTML(v) { this.children = []; } });
global.document = { getElementById: id => (els[id] = els[id] || mk()), createElement: () => mk() };
let calMonth = new RealDate(2026, 9, 1);
const FESTIVALS = {"2026-10-20": "Dussehra"};
let selectedIso = "2026-10-20";
function loadDay() {}
""" + pad + iso + cal + """
renderCalendar();
const cells = els["sp-cal-grid"].children.filter(c => c.className.startsWith("cal-cell"));
const names = re => cells.filter(c => re.test(c.className)).map(c => String(c.textContent));
console.log(JSON.stringify({ today: names(/today/), selected: names(/selected/), festivals: names(/has-festival/),
  label: cells.find(c => String(c.textContent) === "20").attrs["aria-label"], total: cells.length }));
"""
        path = tmp_path / "cal.js"
        path.write_text(harness, encoding="utf-8")
        result = subprocess.run([node, str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)
        assert out == {"today": ["17"], "selected": ["20"], "festivals": ["20"],
                       "label": "20 October, Dussehra", "total": 31}
