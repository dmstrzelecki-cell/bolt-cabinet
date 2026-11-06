"""Activity monitoring and parent notifications."""
from __future__ import annotations

import pytest

from conftest import CHILD, Client
from familysearch.notify import COOLDOWN, Notifier
from test_search_content import SEED, seed


class Sent:
    def __init__(self):
        self.msgs = []

    def __call__(self, url, fmt, title, body, click):
        self.msgs.append({"url": url, "fmt": fmt, "title": title, "body": body, "click": click})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def sent():
    return Sent()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def mon(make_app, keys, sent, clock):
    """App with a notifier wired to a recorder; returns (child, parent, core)."""
    n = Notifier("https://ntfy.example/secret-topic", "ntfy", "https://search.thestrzs.net/parent#activity",
                 send=sent, clock=clock, background=False)
    app = make_app(notifier=n)
    child = Client(app, keys, CHILD)
    parent = Client(app, keys, "david@example.com")
    return child, parent, app.state.core


def turn_on(parent, **extra):
    assert parent.post("/api/parent/settings", json={"store_history": True, **extra}).status_code == 200


def test_nothing_is_logged_until_a_parent_turns_monitoring_on(mon, fake_meili, sent):
    child, parent, core = mon
    seed(fake_meili, *SEED)
    child.get("/api/search?q=moon")
    child.get("/api/search?q=naked")
    child.post("/api/activity", json={"url": "https://www.nasa.gov/moon", "q": "moon"})
    assert parent.get("/api/parent/activity").json()["rows"] == []
    assert sent.msgs == []  # flagged-search alerts need monitoring on
    assert child.get("/api/config").json()["monitoring_notice"] is False


def test_logs_searches_tiles_opens_and_flags(mon, fake_meili, sent):
    child, parent, core = mon
    seed(fake_meili, *SEED)
    turn_on(parent)
    child.get("/api/search?q=moon")
    child.get("/api/search?q=moon&page=2")                 # paging is not a new search
    child.get("/api/search?tile=space&q=")
    child.post("/api/activity", json={"url": "https://www.nasa.gov/moon", "q": "moon"})
    child.post("/api/activity", json={"url": "https://evil.example.com/x", "q": "moon"})  # not approved: ignored
    child.get("/api/search?q=naked mole rat")              # blocked word
    child.get("/api/search?q=how to get on snapchat")      # watch word: results still shown
    child.post("/api/requests", json={"kind": "query", "text": "dinosaur eggs"})
    child.get("/api/suggest?q=moo")                        # autocomplete is never logged
    rows = parent.get("/api/parent/activity").json()["rows"]
    got = [(r["kind"], r["q"], r["tile"], r["url"], r["flag"], r["term"]) for r in reversed(rows)]
    assert got == [
        ("search", "moon", "", "", "", ""),
        ("tile", "", "Space", "", "", ""),
        ("open", "moon", "", "https://www.nasa.gov/moon", "", ""),
        ("search", "naked mole rat", "", "", "blocked", "naked"),
        ("search", "how to get on snapchat", "", "", "watch", "snapchat"),
        ("ask", "dinosaur eggs", "", "", "request", ""),
    ]
    assert rows[-1]["results"] > 0                            # "moon" found results
    flagged = parent.get("/api/parent/activity?flagged=1").json()["rows"]
    assert {r["flag"] for r in flagged} == {"blocked", "watch", "request"}
    st = parent.get("/api/parent/state").json()
    assert st["activity"]["flagged_7d"] == 2 and st["activity"]["searches_7d"] == 3
    assert child.get("/api/config").json()["monitoring_notice"] is True


def test_watch_words_do_not_block_results(mon, fake_meili):
    child, parent, core = mon
    seed(fake_meili, *SEED)
    parent.post("/api/parent/filters", json={"blocked": ["naked"], "watch": ["moon"], "synonyms": {}})
    d = child.get("/api/search?q=moon").json()
    assert d["blocked"] is False and d["results"]


def test_notifications_say_only_that_something_needs_a_look(mon, sent, clock):
    child, parent, core = mon
    turn_on(parent)
    child.get("/api/search?q=naked")
    assert len(sent.msgs) == 1
    m = sent.msgs[0]
    assert "naked" not in m["body"] and "naked" not in m["title"]
    assert "blocked search" in m["body"] and m["click"].endswith("/parent#activity")
    assert m["url"] == "https://ntfy.example/secret-topic" and m["fmt"] == "ntfy"


def test_notifications_can_include_the_text_when_a_parent_chooses(mon, sent, clock):
    child, parent, core = mon
    turn_on(parent, notify={"blocked": True, "watch": True, "request": True, "include_text": True})
    child.get("/api/search?q=how to get on snapchat")
    assert "how to get on snapchat" in sent.msgs[0]["body"]


def test_notifications_are_coalesced_and_respect_choices(mon, sent, clock):
    child, parent, core = mon
    turn_on(parent, notify={"blocked": True, "watch": False, "request": True, "include_text": False})
    child.get("/api/search?q=snapchat")          # watch notifications off
    assert sent.msgs == []
    child.get("/api/search?q=naked")             # sent now
    child.get("/api/search?q=nude")              # within cooldown: held
    child.get("/api/search?q=porn")
    assert len(sent.msgs) == 1
    clock.t += COOLDOWN + 1
    core.notifier.flush()                        # what the timer does
    assert len(sent.msgs) == 2 and "2 × a blocked search" in sent.msgs[1]["body"]
    child.post("/api/requests", json={"kind": "site", "text": "www.lego.com"})
    clock.t += COOLDOWN + 1
    core.notifier.flush()
    assert "Ask-a-grown-up" in sent.msgs[-1]["body"]


def test_test_notification_and_unconfigured(mon, make_app, keys, sent):
    child, parent, core = mon
    assert parent.post("/api/parent/notify/test", json={}).status_code == 200
    assert sent.msgs[-1]["title"] == "Family Search: test"
    assert child.post("/api/parent/notify/test", json={}).status_code == 403
    plain = Client(make_app(), keys, "david@example.com")
    assert plain.post("/api/parent/notify/test", json={}).status_code == 400
    assert plain.get("/api/parent/state").json()["notify_configured"] is False


def test_monitoring_off_keeps_log_until_cleared_and_settings_validate(mon):
    child, parent, core = mon
    turn_on(parent)
    child.get("/api/search?q=moon")
    parent.post("/api/parent/settings", json={"store_history": False})
    child.get("/api/search?q=stars")
    rows = parent.get("/api/parent/activity").json()["rows"]
    assert [r["q"] for r in rows] == ["moon"]
    assert parent.post("/api/parent/activity/clear", json={}).status_code == 200
    assert parent.get("/api/parent/activity").json()["rows"] == []
    assert parent.post("/api/parent/settings", json={"retention_days": 7}).status_code == 400
    assert parent.post("/api/parent/settings", json={"retention_days": 365}).status_code == 200
    assert parent.post("/api/parent/settings", json={"show_notice": False}).status_code == 200
    turn_on(parent)
    assert child.get("/api/config").json()["monitoring_notice"] is False
    actions = [r["action"] for r in parent.get("/api/parent/audit?kind=parent").json()["rows"]]
    assert "activity log cleared" in actions and "activity monitoring off" in actions


def test_child_cannot_read_activity(mon):
    child, parent, core = mon
    assert child.get("/api/parent/activity").status_code == 403
    assert child.post("/api/parent/activity/clear", json={}).status_code == 403
    assert child.post("/api/parent/settings", json={"store_history": False}).status_code == 403


def test_notify_url_never_reaches_the_browser(mon):
    child, parent, core = mon
    turn_on(parent)
    parent.get("/api/parent/state")
    child.get("/api/config")
    assert b"secret-topic" not in b"".join(parent.bodies + child.bodies)


def type_out(child, text):
    """What the search box sends as she types (one call per pause)."""
    for i in range(2, len(text) + 1):
        child.get("/api/suggest", params={"q": text[:i]})


def rows_of(parent, kind=None):
    rows = parent.get("/api/parent/activity").json()["rows"]
    return [r for r in rows if kind is None or r["kind"] == kind]


def test_typed_but_erased_flagged_word_is_logged_once(mon, sent, clock):
    child, parent, core = mon
    turn_on(parent)
    type_out(child, "how do i get snapchat on my phone")   # then she erases it
    typed = rows_of(parent, "typed")
    assert len(typed) == 1
    assert typed[0]["q"] == "how do i get snapchat on my phone"  # the fullest draft is kept
    assert typed[0]["flag"] == "watch" and typed[0]["term"] == "snapchat"
    assert len(sent.msgs) == 1 and "typed in the search box" in sent.msgs[0]["body"]
    assert "snapchat" not in sent.msgs[0]["body"]               # no text unless opted in


def test_ordinary_typing_is_never_logged(mon, sent):
    child, parent, core = mon
    turn_on(parent)
    type_out(child, "how far away is the moon")
    type_out(child, "snap peas")       # "snap" is not the watch word "snapchat"
    assert rows_of(parent) == [] and sent.msgs == []


def test_drafts_ignored_when_monitoring_is_off(mon, sent):
    child, parent, core = mon
    type_out(child, "naked mole rat")
    assert rows_of(parent) == [] and sent.msgs == []


def test_blocked_word_typed_is_flagged_blocked(mon):
    child, parent, core = mon
    turn_on(parent)
    type_out(child, "naked")
    assert child.get("/api/suggest?q=naked").json()["suggestions"] == []
    typed = rows_of(parent, "typed")
    assert len(typed) == 1 and typed[0]["flag"] == "blocked" and typed[0]["term"] == "naked"


def test_typed_then_searched_becomes_one_search_row(mon, sent, clock):
    child, parent, core = mon
    turn_on(parent)
    type_out(child, "discord servers")
    child.get("/api/search?q=discord servers")
    rows = rows_of(parent)
    assert [(r["kind"], r["term"]) for r in rows] == [("search", "discord")]
    clock.t += 10_000
    core.notifier.flush()
    assert len(sent.msgs) == 1   # told once, not twice
    type_out(child, "discord servers")   # typing it again right after searching adds nothing
    assert len(rows_of(parent)) == 1


def test_two_different_flagged_words_are_two_rows(mon):
    child, parent, core = mon
    turn_on(parent)
    type_out(child, "tiktok")
    type_out(child, "vape")
    assert sorted(r["term"] for r in rows_of(parent, "typed")) == ["tiktok", "vape"]


def test_watch_word_search_offers_ask_a_grownup(mon, fake_meili):
    child, parent, core = mon
    seed(fake_meili, *SEED)
    d = child.get("/api/search?q=moon bullying").json()
    assert d["suggest_ask"] is True and d["blocked"] is False and d["results"]   # results still shown
    assert "bullying" not in str({k: v for k, v in d.items() if k != "q"})       # word not revealed
    assert child.get("/api/search?q=moon").json()["suggest_ask"] is False
    assert child.get("/api/search?q=naked").json()["suggest_ask"] is False       # blocked has its own message
    parent.post("/api/parent/settings", json={"watch_prompt": False})
    assert child.get("/api/search?q=moon bullying").json()["suggest_ask"] is False
    assert parent.get("/api/parent/state").json()["settings"]["watch_prompt"] is False
