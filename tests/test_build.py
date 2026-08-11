import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

import pipeline.build as build_mod
from pipeline.config import REPO_ROOT as REAL_REPO_ROOT
from pipeline.db import SCHEMA


# --- dominant_theme -------------------------------------------------------------

class TestDominantTheme:
    THEME_PRIORITY = ["IRELAND", "HOUSING & PROPERTY", "MACROECONOMICS", "GLOBAL ECONOMY"]

    def test_no_topics_falls_back_to_general(self):
        assert build_mod.dominant_theme(None, self.THEME_PRIORITY) == "General"
        assert build_mod.dominant_theme("", self.THEME_PRIORITY) == "General"

    def test_single_topic_is_used_directly(self):
        assert build_mod.dominant_theme("MACROECONOMICS", self.THEME_PRIORITY) == "MACROECONOMICS"

    def test_multiple_topics_pick_highest_priority(self):
        # GLOBAL ECONOMY and IRELAND both present -- IRELAND is earlier in
        # theme_priority so it wins, e.g. an Irish housing story should land
        # under Ireland rather than Global Economy.
        topics = "GLOBAL ECONOMY,IRELAND,MACROECONOMICS"
        assert build_mod.dominant_theme(topics, self.THEME_PRIORITY) == "IRELAND"

    def test_topics_not_in_priority_list_fall_back_to_alphabetical(self):
        topics = "ZEBRA,APPLE"
        assert build_mod.dominant_theme(topics, self.THEME_PRIORITY) == "APPLE"

    def test_priority_topic_wins_even_if_listed_second_in_csv(self):
        topics = "MACROECONOMICS,IRELAND"
        assert build_mod.dominant_theme(topics, self.THEME_PRIORITY) == "IRELAND"


# --- _group_archive_days ---------------------------------------------------------

class TestGroupArchiveDays:
    def test_groups_by_year_and_month_newest_first(self):
        days = [
            {"date": "2026-07-28", "count": 3},
            {"date": "2026-07-15", "count": 2},
            {"date": "2026-06-01", "count": 5},
            {"date": "2025-12-25", "count": 1},
        ]
        years = build_mod._group_archive_days(days)

        assert [y["year"] for y in years] == ["2026", "2025"]
        assert years[0]["count"] == 10
        assert [m["key"] for m in years[0]["months"]] == ["2026-07", "2026-06"]
        assert years[0]["months"][0]["count"] == 5
        assert years[0]["months"][0]["label"] == "July 2026"
        # Day order within a month is preserved from the input.
        assert [d["date"] for d in years[0]["months"][0]["days"]] == ["2026-07-28", "2026-07-15"]

    def test_unparseable_date_goes_into_unknown_bucket(self):
        days = [{"date": "unknown", "count": 2}, {"date": "2026-07-28", "count": 1}]
        years = build_mod._group_archive_days(days)

        year_keys = [y["year"] for y in years]
        assert "Unknown" in year_keys
        unknown_year = next(y for y in years if y["year"] == "Unknown")
        assert unknown_year["count"] == 2
        assert unknown_year["months"][0]["label"] == "Unknown"


# --- render_site integration: homepage recency filter + top10 fallback ----------

def _iso(dt):
    return dt.isoformat()


@pytest.fixture
def site_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO feeds (id, url, name) VALUES (1, 'https://feed.example/rss', 'Feed')")
    conn.commit()
    conn.close()

    @contextmanager
    def get_connection():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    output_dir = tmp_path / "site"

    def fake_resolve_path(relative):
        if str(relative) == "config/taxonomy.yaml":
            return REAL_REPO_ROOT / relative
        return output_dir if str(relative) == "site" else tmp_path / relative

    fake_cfg = {
        "site": {
            "title": "Test Aggregator",
            "base_url": "",
            "output_dir": "site",
            "archive_window_days": 3,
        },
        "paths": {"taxonomy_yaml": "config/taxonomy.yaml"},
        "run": {"recovery_check_interval_hours": 24},
    }

    monkeypatch.setattr(build_mod, "get_connection", get_connection)
    monkeypatch.setattr(build_mod, "get_config", lambda: fake_cfg)
    monkeypatch.setattr(build_mod, "resolve_path", fake_resolve_path)

    return db_path, output_dir


def _insert_article(db_path, url_hash, title, fetched, published=None, cluster_id=None, topics="", fetch_run_id=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO articles (url_hash, feed_id, title, url, fetched, published, summary, topics, cluster_id, fetch_run_id)
        VALUES (?, 1, ?, ?, ?, ?, 'summary', ?, ?, ?)
        """,
        (url_hash, title, f"https://example.com/{url_hash}", fetched, published, topics, cluster_id, fetch_run_id),
    )
    conn.commit()
    conn.close()


def _insert_fetch_run(db_path, run_id, started, finished=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO fetch_runs (id, started, finished) VALUES (?, ?, ?)",
        (run_id, started, finished),
    )
    conn.commit()
    conn.close()


def _insert_cluster(db_path, cluster_id, created, representative_article):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO clusters (id, created, label, representative_article) VALUES (?, ?, 'label', ?)",
        (cluster_id, created, representative_article),
    )
    conn.commit()
    conn.close()


def _insert_top10(db_path, date, rank, cluster_id, rationale="because"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO daily_top10 (date, rank, cluster_id, rationale) VALUES (?, ?, ?, ?)",
        (date, rank, cluster_id, rationale),
    )
    conn.commit()
    conn.close()


class TestHomepageRecencyFilter:
    def test_old_published_but_recently_fetched_article_is_excluded(self, site_env):
        db_path, output_dir = site_env
        now = datetime.now(timezone.utc)
        old_published = _iso(now - timedelta(days=100))

        _insert_article(db_path, "old-pub", "Stale Published Story", _iso(now), published=old_published, cluster_id=1)
        _insert_cluster(db_path, 1, _iso(now), "old-pub")

        build_mod.render_site()

        html = (output_dir / "index.html").read_text(encoding="utf-8")
        # Documented past bug: fetched-only filtering let a genuinely old
        # article (real published date, not a metadata glitch) show up as
        # "recent" just because we fetched it today.
        assert "Stale Published Story" not in html

    def test_article_with_no_published_falls_back_to_fetched(self, site_env):
        db_path, output_dir = site_env
        now = datetime.now(timezone.utc)

        _insert_article(db_path, "no-pub", "Fresh Unpublished-Date Story", _iso(now), published=None, cluster_id=1)
        _insert_cluster(db_path, 1, _iso(now), "no-pub")

        build_mod.render_site()

        html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert "Fresh Unpublished-Date Story" in html

    def test_recently_published_article_is_included(self, site_env):
        db_path, output_dir = site_env
        now = datetime.now(timezone.utc)
        recent_published = _iso(now - timedelta(hours=2))

        _insert_article(db_path, "recent-pub", "Recent Story", _iso(now), published=recent_published, cluster_id=1)
        _insert_cluster(db_path, 1, _iso(now), "recent-pub")

        build_mod.render_site()

        html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert "Recent Story" in html


class TestDailyTop10Fallback:
    def test_falls_back_to_most_recent_date_when_today_has_no_curate_run(self, site_env):
        db_path, output_dir = site_env
        now = datetime.now(timezone.utc)
        yesterday = (now - timedelta(days=1)).date().isoformat()

        _insert_article(db_path, "top-story", "Top Ranked Story", _iso(now), cluster_id=1)
        _insert_cluster(db_path, 1, _iso(now), "top-story")
        _insert_top10(db_path, yesterday, 1, 1)

        stats = build_mod.render_site()

        assert stats["top10"] == 1
        html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert "Top Ranked Story" in html

    def test_uses_todays_run_when_present_instead_of_falling_back(self, site_env):
        db_path, output_dir = site_env
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        yesterday = (now - timedelta(days=1)).date().isoformat()

        _insert_article(db_path, "today-story", "Todays Top Story", _iso(now), cluster_id=1)
        _insert_cluster(db_path, 1, _iso(now), "today-story")
        # Old fetched/published so it's outside the homepage recency window
        # entirely -- the only way it could show up on the homepage is via
        # yesterday's (stale) top10 entry, which today's fallback logic must
        # not use once today's own curate run has produced rows.
        long_ago = _iso(now - timedelta(days=100))
        _insert_article(db_path, "yesterday-story", "Yesterdays Top Story", long_ago, published=long_ago, cluster_id=2)
        _insert_cluster(db_path, 2, long_ago, "yesterday-story")
        _insert_top10(db_path, yesterday, 1, 2)
        _insert_top10(db_path, today, 1, 1)

        stats = build_mod.render_site()

        assert stats["top10"] == 1
        html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert "Todays Top Story" in html
        assert "Yesterdays Top Story" not in html


# --- Just In: clusters first created by the latest fetch run --------------------

class TestJustIn:
    def test_cluster_created_by_latest_run_appears_in_just_in(self, site_env):
        db_path, output_dir = site_env
        now = datetime.now(timezone.utc)

        _insert_fetch_run(db_path, 1, _iso(now - timedelta(hours=6)), _iso(now - timedelta(hours=5)))
        _insert_fetch_run(db_path, 2, _iso(now), _iso(now))
        _insert_article(db_path, "brand-new", "Brand New Story", _iso(now), cluster_id=1, fetch_run_id=2)
        _insert_cluster(db_path, 1, _iso(now), "brand-new")

        stats = build_mod.render_site()

        assert stats["just_in"] == 1
        html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert 'id="just-in"' in html
        assert "Brand New Story" in html

    def test_cluster_that_only_gained_an_article_this_run_is_excluded(self, site_env):
        # The representative_article (set once, at cluster-creation time --
        # see pipeline.cluster) belongs to an OLDER run, even though this
        # cluster is still fresh enough to appear on the homepage at all.
        # A cluster reattaching a new member during the latest run must not
        # count as "just in" -- see pipeline.curate's own comment on why
        # cluster_id, not article recency, is the "same story" identity.
        db_path, output_dir = site_env
        now = datetime.now(timezone.utc)

        _insert_fetch_run(db_path, 1, _iso(now - timedelta(days=1)), _iso(now - timedelta(days=1)))
        _insert_fetch_run(db_path, 2, _iso(now), _iso(now))
        _insert_article(
            db_path, "developing-story", "Developing Story", _iso(now - timedelta(hours=1)),
            cluster_id=1, fetch_run_id=1,
        )
        _insert_cluster(db_path, 1, _iso(now - timedelta(days=1)), "developing-story")

        stats = build_mod.render_site()

        assert stats["just_in"] == 0
        html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert 'id="just-in"' not in html
        # Still shown elsewhere on the homepage (recent groups), just not as Just In.
        assert "Developing Story" in html

    def test_no_fetch_runs_hides_just_in_section_entirely(self, site_env):
        db_path, output_dir = site_env
        now = datetime.now(timezone.utc)

        _insert_article(db_path, "no-run", "No Run Recorded Story", _iso(now), cluster_id=1)
        _insert_cluster(db_path, 1, _iso(now), "no-run")

        stats = build_mod.render_site()

        assert stats["just_in"] == 0
        html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert 'id="just-in"' not in html

    def test_top10_cluster_from_latest_run_is_not_duplicated_into_just_in(self, site_env):
        db_path, output_dir = site_env
        now = datetime.now(timezone.utc)

        _insert_fetch_run(db_path, 1, _iso(now), _iso(now))
        _insert_article(db_path, "top-and-new", "Top And New Story", _iso(now), cluster_id=1, fetch_run_id=1)
        _insert_cluster(db_path, 1, _iso(now), "top-and-new")
        _insert_top10(db_path, now.date().isoformat(), 1, 1)

        stats = build_mod.render_site()

        assert stats["top10"] == 1
        assert stats["just_in"] == 0
        html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert 'id="just-in"' not in html

    def test_just_in_capped_at_ten(self, site_env):
        db_path, output_dir = site_env
        now = datetime.now(timezone.utc)

        _insert_fetch_run(db_path, 1, _iso(now), _iso(now))
        for i in range(12):
            url_hash = f"new-{i}"
            fetched = _iso(now - timedelta(minutes=i))
            _insert_article(db_path, url_hash, f"New Story {i}", fetched, cluster_id=i, fetch_run_id=1)
            _insert_cluster(db_path, i, fetched, url_hash)

        stats = build_mod.render_site()

        assert stats["just_in"] == 10
