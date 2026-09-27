import json
from datetime import datetime, timezone

from app.collectors.http import FetchError, HttpResponse, RobotsDisallowed
from app.collectors.steam import NEWS_URL
from app.db import repository as repo
from app.services import ingest
from app.services.notify import Notifier, NotifyError, dispatch_pending

from .conftest import FakeHttp, fixture_text


def _source(conn, key="palworld-steam"):
    return repo.get_source(conn, key)


def test_first_run_is_baseline_without_notifications(conn, config):
    http = FakeHttp({NEWS_URL: fixture_text("steam_palworld.json")})
    outcome = ingest.run_source(conn, _source(conn), http, config, notify_channel="log")
    assert outcome.status == "ok"
    assert len(outcome.new_ids) == 2  # la vente et l'événement sont filtrés
    assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
    log = conn.execute("SELECT status, new_count FROM fetch_log").fetchone()
    assert tuple(log) == ("ok", 2)


def test_second_run_dedupes_and_notifies_new(conn, config):
    data = json.loads(fixture_text("steam_palworld.json"))
    http = FakeHttp({NEWS_URL: json.dumps(data)})
    ingest.run_source(conn, _source(conn), http, config, notify_channel="log")

    new_item = dict(data["appnews"]["newsitems"][1], gid="999", title="Hotfix v0.6.8.1",
                    contents="[list][*]Fixed a crash on startup[/list]", date=1790200000)
    data["appnews"]["newsitems"].insert(0, new_item)
    http.routes[NEWS_URL] = json.dumps(data)
    outcome = ingest.run_source(conn, _source(conn), http, config, notify_channel="log")
    assert len(outcome.new_ids) == 1 and outcome.updated_ids == []
    assert conn.execute("SELECT count(*) FROM patches").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1


def test_changed_content_updates_existing_patch(conn, config):
    data = json.loads(fixture_text("steam_palworld.json"))
    http = FakeHttp({NEWS_URL: json.dumps(data)})
    first = ingest.run_source(conn, _source(conn), http, config, notify_channel="log")
    data["appnews"]["newsitems"][0]["contents"] += "\n[h2]Addendum[/h2][list][*]Fixed a typo[/list]"
    http.routes[NEWS_URL] = json.dumps(data)
    outcome = ingest.run_source(conn, _source(conn), http, config, notify_channel="log")
    assert outcome.new_ids == [] and len(outcome.updated_ids) == 1
    patch = repo.get_patch(conn, outcome.updated_ids[0])
    assert patch["revision"] == 2
    assert patch["sections"][-1]["title"] == "Addendum"
    assert outcome.updated_ids[0] in first.new_ids


def test_not_modified_keeps_cache_headers(conn, config):
    http = FakeHttp({NEWS_URL: HttpResponse(NEWS_URL, 304, "", {})})
    conn.execute("UPDATE sources SET etag = '\"abc\"' WHERE key = 'palworld-steam'")
    outcome = ingest.run_source(conn, _source(conn), http, config, notify_channel=None)
    assert outcome.status == "not_modified"
    assert _source(conn).etag == '"abc"'


def test_failure_is_logged_and_backed_off(conn, config):
    http = FakeHttp({NEWS_URL: FetchError("HTTP 503", status=503, retry_after=7200)})
    outcome = ingest.run_source(conn, _source(conn), http, config, notify_channel=None)
    assert outcome.status == "error"
    src = _source(conn)
    assert src.consecutive_failures == 1
    next_at = datetime.fromisoformat(src.next_fetch_at)
    assert (next_at - datetime.now(timezone.utc)).total_seconds() > 7000
    row = conn.execute("SELECT status, error FROM fetch_log").fetchone()
    assert row["status"] == "error" and "503" in row["error"]


def test_robots_disallow_uses_max_backoff(conn, config):
    http = FakeHttp({NEWS_URL: RobotsDisallowed("interdit")})
    ingest.run_source(conn, _source(conn), http, config, notify_channel=None)
    delta = datetime.fromisoformat(_source(conn).next_fetch_at) - datetime.now(timezone.utc)
    assert delta.total_seconds() > config.max_backoff - 60


def test_one_failing_source_does_not_block_others(conn, config):
    http = FakeHttp({NEWS_URL: fixture_text("steam_palworld.json")})
    # Seule l'URL Steam répond ; les sources Blizzard échouent.
    outcomes = ingest.run_due(conn, http, config, notify_channel=None, force=True)
    statuses = {o.source_key: o.status for o in outcomes}
    assert statuses["palworld-steam"] == "ok"
    assert statuses["diablo-4-blizzard"] == "error"
    # Les sources désactivées du catalogue ne sont pas collectées.
    assert "wuthering-waves-site" not in statuses


def test_inactive_game_is_not_collected(conn, config):
    repo.set_game_active(conn, "palworld", False)
    due = [s.key for s in repo.sources_due(conn, "9999")]
    assert "palworld-steam" not in due


def test_backoff_growth_is_capped():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    delays = [(ingest.next_fetch(now, 3600, f, 86400) - now).total_seconds() for f in range(0, 8)]
    assert delays[0] == 3600
    assert delays[1] < delays[2] < delays[3]
    assert max(delays) == 86400


class FlakyNotifier(Notifier):
    channel = "log"

    def __init__(self):
        self.sent = []
        self.fail = True

    def send(self, notification):
        if self.fail:
            raise NotifyError("boom")
        self.sent.append(notification)


def test_notifications_retry_until_sent(conn, config):
    data = json.loads(fixture_text("steam_palworld.json"))
    http = FakeHttp({NEWS_URL: json.dumps(data)})
    ingest.run_source(conn, _source(conn), http, config, notify_channel="log")
    data["appnews"]["newsitems"].insert(0, dict(data["appnews"]["newsitems"][1], gid="1000",
                                                title="Hotfix v0.6.9"))
    http.routes[NEWS_URL] = json.dumps(data)
    ingest.run_source(conn, _source(conn), http, config, notify_channel="log")

    notifier = FlakyNotifier()
    assert dispatch_pending(conn, notifier, "https://gu.test") == 0
    notifier.fail = False
    assert dispatch_pending(conn, notifier, "https://gu.test") == 1
    assert notifier.sent[0].title == "Palworld — nouveau patch"
    assert notifier.sent[0].click_url.startswith("https://gu.test/patch/")
    assert dispatch_pending(conn, notifier, "https://gu.test") == 0
