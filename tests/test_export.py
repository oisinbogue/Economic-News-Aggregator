import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import pipeline.export as export_mod
from pipeline.db import SCHEMA


def _iso(dt):
    return dt.isoformat()


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.db"
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.row_factory = sqlite3.Row
    c.execute("INSERT INTO feeds (id, url, name) VALUES (1, 'https://feed.example/rss', 'Example Feed')")
    c.commit()
    yield c
    c.close()


def _insert_article(conn, url_hash, title="Title", fetched=None, published=None,
                     cluster_id=None, feed_id=1, country="", topics="", score=1, summary="summary"):
    conn.execute(
        """
        INSERT INTO articles (url_hash, feed_id, title, url, fetched, published, summary, country, topics, score, cluster_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (url_hash, feed_id, title, f"https://example.com/{url_hash}", fetched, published,
         summary, country, topics, score, cluster_id),
    )
    conn.commit()


def _insert_cluster(conn, cluster_id, created, representative_article):
    conn.execute(
        "INSERT INTO clusters (id, created, label, representative_article) VALUES (?, ?, 'label', ?)",
        (cluster_id, created, representative_article),
    )
    conn.commit()


def _insert_top10(conn, date, rank, cluster_id, rationale="because"):
    conn.execute(
        "INSERT INTO daily_top10 (date, rank, cluster_id, rationale) VALUES (?, ?, ?, ?)",
        (date, rank, cluster_id, rationale),
    )
    conn.commit()


# --- CSV row shaping -------------------------------------------------------------

class TestCsvRowShaping:
    def test_csv_row_maps_feed_id_to_source_name(self):
        article = {"url_hash": "a1", "feed_id": 1, "title": "T", "url": "https://x", "cluster_id": 5}
        row = export_mod._csv_row(article, {1: "Example Feed"})
        assert row["source"] == "Example Feed"
        assert row["cluster_id"] == 5
        assert row["url_hash"] == "a1"

    def test_csv_row_defaults_missing_optional_fields_to_empty_string(self):
        article = {"url_hash": "a1", "feed_id": 1}
        row = export_mod._csv_row(article, {})
        assert row["title"] == ""
        assert row["url"] == ""
        assert row["source"] == ""
        assert row["country"] == ""
        assert row["topics"] == ""
        assert row["published"] == ""
        assert row["fetched"] == ""
        assert row["summary"] == ""
        # score/cluster_id are left as None rather than coerced to "" -- a
        # CSV reader can distinguish "no score yet" from "score of zero".
        assert row["score"] is None
        assert row["cluster_id"] is None

    def test_write_csv_exports_excludes_unclustered_articles(self, conn, tmp_path):
        now = _iso(datetime.now(timezone.utc))
        _insert_article(conn, "clustered", fetched=now, cluster_id=1)
        _insert_article(conn, "unclustered", fetched=now, cluster_id=None)

        stats = export_mod.write_csv_exports(conn, tmp_path)

        assert stats["master_rows"] == 1
        master_text = (tmp_path / "exports" / "master.csv").read_text(encoding="utf-8")
        assert "clustered" in master_text
        assert "unclustered" not in master_text

    def test_write_csv_exports_splits_by_fetched_day(self, conn, tmp_path):
        day1 = _iso(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))
        day2 = _iso(datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc))
        _insert_article(conn, "a1", fetched=day1, cluster_id=1)
        _insert_article(conn, "a2", fetched=day2, cluster_id=2)

        stats = export_mod.write_csv_exports(conn, tmp_path)

        assert stats["daily_files"] == 2
        assert (tmp_path / "exports" / "daily" / "2026-07-20.csv").exists()
        assert (tmp_path / "exports" / "daily" / "2026-07-21.csv").exists()
        day1_text = (tmp_path / "exports" / "daily" / "2026-07-20.csv").read_text(encoding="utf-8")
        assert "a1" in day1_text
        assert "a2" not in day1_text


# --- RSS escaping ------------------------------------------------------------------

class TestRssEscaping:
    def test_rss_item_escapes_ampersand_and_angle_brackets_in_title_and_summary(self):
        lead = {
            "url": "https://example.com/story?a=1&b=2",
            "fetched": _iso(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)),
            "title": 'Bonds & "Rates" <update>',
            "summary": "A vs B & C's <tag>",
            "cluster_id": 1,
            "topic_list": [],
            "source_name": "Feed",
        }
        item = export_mod._rss_item(lead, base_url="")

        # xml.sax.saxutils.escape() (used here with its default entity map)
        # escapes &, <, > but deliberately leaves quotes as-is -- valid inside
        # XML element text content, unlike inside an attribute value.
        assert '<title>Bonds &amp; "Rates" &lt;update&gt;</title>' in item
        assert "A vs B &amp; C's &lt;tag&gt;" in item
        assert "<link>https://example.com/story?a=1&amp;b=2</link>" in item
        # Literal, unescaped tags must never leak through -- that would break
        # the XML document.
        assert "<update>" not in item
        assert "<tag>" not in item

    def test_rss_item_escapes_categories(self):
        lead = {
            "url": "https://example.com/story",
            "fetched": _iso(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)),
            "title": "T",
            "summary": "",
            "cluster_id": 1,
            "topic_list": ["Trade & Tariffs"],
            "source_name": "Feed",
        }
        item = export_mod._rss_item(lead, base_url="")
        assert "<category>Trade &amp; Tariffs</category>" in item

    def test_rss_item_guid_uses_url_when_base_url_empty(self):
        lead = {
            "url": "https://example.com/story",
            "fetched": _iso(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)),
            "title": "T", "summary": "", "cluster_id": 42, "topic_list": [], "source_name": "Feed",
        }
        item = export_mod._rss_item(lead, base_url="")
        assert '<guid isPermaLink="false">https://example.com/story</guid>' in item

    def test_rss_item_guid_uses_base_url_and_cluster_id_when_base_url_set(self):
        lead = {
            "url": "https://example.com/story",
            "fetched": _iso(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)),
            "title": "T", "summary": "", "cluster_id": 42, "topic_list": [], "source_name": "Feed",
            "permalink": "story/42.html",
        }
        item = export_mod._rss_item(lead, base_url="https://site.example/")
        assert '<guid isPermaLink="true">https://site.example/story/42.html</guid>' in item

    def test_rss_item_falls_back_to_now_on_unparseable_fetched(self):
        lead = {
            "url": "https://example.com/story", "fetched": "not-a-date",
            "title": "T", "summary": "", "cluster_id": 1, "topic_list": [], "source_name": "Feed",
        }
        # Must not raise despite the bad timestamp.
        item = export_mod._rss_item(lead, base_url="")
        assert "<pubDate>" in item

    def test_build_rss_escapes_title_in_channel_and_wraps_items(self):
        site_cfg = {"title": "News & Views", "base_url": ""}
        xml = export_mod.build_rss([], site_cfg)
        assert "<title>News &amp; Views</title>" in xml
        assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert "<rss version=\"2.0\"" in xml


# --- latest.json structure --------------------------------------------------------

class TestLatestJsonStructure:
    def test_build_latest_json_orders_top10_by_rank_and_includes_all_in_latest(self, conn):
        now = _iso(datetime.now(timezone.utc))
        _insert_article(conn, "a1", title="First", fetched=now, cluster_id=1, topics="MACROECONOMICS", country="IE")
        _insert_article(conn, "a2", title="Second", fetched=now, cluster_id=2, topics="TRADE")
        _insert_article(conn, "a3", title="Not Ranked", fetched=now, cluster_id=3)
        _insert_cluster(conn, 1, now, "a1")
        _insert_cluster(conn, 2, now, "a2")
        _insert_cluster(conn, 3, now, "a3")
        today = datetime.now(timezone.utc).date().isoformat()
        # Insert out of rank order to prove build_latest_json sorts by rank,
        # not insertion/query order.
        _insert_top10(conn, today, 2, 2, rationale="second reason")
        _insert_top10(conn, today, 1, 1, rationale="first reason")

        leads = export_mod._feed_items(conn, {1: "Example Feed"}, [])
        result = export_mod.build_latest_json(conn, {"title": "T", "base_url": ""}, leads)

        assert [item["cluster_id"] for item in result["top10"]] == [1, 2]
        assert result["top10"][0]["rank"] == 1
        assert result["top10"][0]["rationale"] == "first reason"
        assert len(result["latest"]) == 3
        not_ranked = next(item for item in result["latest"] if item["cluster_id"] == 3)
        assert not_ranked["rank"] is None
        assert not_ranked["rationale"] is None
        assert "generated" in result
        assert result["site"]["title"] == "T"

    def test_item_json_field_shape(self, conn):
        now = _iso(datetime.now(timezone.utc))
        _insert_article(conn, "a1", title="Story", fetched=now, cluster_id=1, country="IE", topics="MACROECONOMICS")
        _insert_cluster(conn, 1, now, "a1")

        leads = export_mod._feed_items(conn, {1: "Example Feed"}, ["MACROECONOMICS"])
        item = export_mod._item_json(leads[0], {}, base_url="")

        assert set(item.keys()) == {
            "cluster_id", "title", "url", "permalink", "source", "summary", "countries", "topics",
            "theme", "is_perspectives", "fetched", "rank", "rationale", "sources_count",
        }
        assert item["title"] == "Story"
        assert item["countries"] == ["IE"]
        assert item["topics"] == ["MACROECONOMICS"]
        assert item["theme"] == "MACROECONOMICS"
        assert item["sources_count"] == 1
        assert item["rank"] is None
        assert item["rationale"] is None

    def test_falls_back_to_most_recent_date_when_today_has_no_rows(self, conn):
        now = _iso(datetime.now(timezone.utc))
        _insert_article(conn, "a1", title="Old Top", fetched=now, cluster_id=1)
        _insert_cluster(conn, 1, now, "a1")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        _insert_top10(conn, yesterday, 1, 1, rationale="yesterday's pick")

        rationales = export_mod._rationales_for_today(conn)

        assert rationales == {1: (1, "yesterday's pick")}
