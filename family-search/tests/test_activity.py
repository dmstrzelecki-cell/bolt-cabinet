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
