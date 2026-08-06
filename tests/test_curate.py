import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from pipeline.curate import (
    WIDENED_CANDIDATE_WINDOW_HOURS,
    CuratorOutput,
    CuratorPick,
    _load_candidates,
    _load_candidates_with_fallback,
    _recent_top10_cluster_ids,
    _select_diverse,
    dominant_country,
    dominant_topic,
)
from pipeline.timeutil import utc_today


def _pick(rank, cluster_id, rationale="because"):
    return CuratorPick(rank=rank, cluster_id=cluster_id, rationale=rationale)


def _candidates(specs):
    """specs: {cluster_id: (country_tag, topic_tag)}"""
    return {cid: {"country_tag": country, "topic_tag": topic} for cid, (country, topic) in specs.items()}


class TestSelectDiverse:
    def test_no_caps_hit_keeps_llm_order(self):
        candidates = _candidates({i: (f"country-{i}", f"topic-{i}") for i in range(1, 11)})
        picks = [_pick(i, i) for i in range(1, 11)]
        selected = _select_diverse(picks, candidates, max_per_country=3, max_per_topic=3)
        assert [p.cluster_id for p in selected] == list(range(1, 11))

    def test_country_cap_skips_fourth_from_same_country(self):
        # Four Irish stories rank 1-4, each with a distinct topic so the
        # topic cap never triggers -- isolates the country cap. Cap of 3
        # should defer the 4th Irish story behind the next 7 (all
        # distinct-country) candidates, which fully fill the remaining 6
        # slots, so cluster 4 never gets backfilled in either.
        candidates = _candidates(
            {
                1: ("Ireland", "Housing"),
                2: ("Ireland", "Inflation"),
                3: ("Ireland", "Trade"),
                4: ("Ireland", "Jobs"),
                5: ("Germany", "Energy"),
                6: ("France", "Markets"),
                7: ("Spain", "Growth"),
                8: ("Italy", "Policy"),
                9: ("Japan", "Wages"),
                10: ("Canada", "Exports"),
                11: ("Brazil", "Debt"),
            }
        )
        picks = [_pick(i, i) for i in range(1, 12)]
        selected = _select_diverse(picks, candidates, max_per_country=3, max_per_topic=3)
        selected_ids = [p.cluster_id for p in selected]
        assert 4 not in selected_ids
        assert len(selected) == 10

    def test_topic_cap_skips_fourth_from_same_topic(self):
        candidates = _candidates(
            {
                1: ("Ireland", "Housing"),
                2: ("Germany", "Housing"),
                3: ("France", "Housing"),
                4: ("Spain", "Housing"),  # 4th "Housing" pick -- should be deferred
                5: ("Italy", "Inflation"),
                6: ("Japan", "Trade"),
                7: ("Canada", "Jobs"),
                8: ("Brazil", "Energy"),
                9: ("Mexico", "Markets"),
                10: ("India", "Policy"),
                11: ("China", "Growth"),
            }
        )
        picks = [_pick(i, i) for i in range(1, 12)]
        selected = _select_diverse(picks, candidates, max_per_country=3, max_per_topic=3)
        selected_ids = [p.cluster_id for p in selected]
        assert 4 not in selected_ids
        assert len(selected) == 10

    def test_backfill_when_caps_would_leave_fewer_than_ten(self):
        # Only 10 candidates total, and the country cap blocks 3 of the top
        # picks outright -- without backfill this would ship fewer than 10.
        candidates = _candidates(
            {
                1: ("Ireland", "Housing"),
                2: ("Ireland", "Inflation"),
                3: ("Ireland", "Trade"),
                4: ("Ireland", "Jobs"),  # blocked by country cap, no room elsewhere
                5: ("Ireland", "Energy"),  # blocked by country cap, no room elsewhere
                6: ("Germany", "Housing"),
                7: ("France", "Housing"),
                8: ("Spain", "Housing"),
                9: ("Italy", "Housing"),
                10: ("Japan", "Housing"),
            }
        )
        picks = [_pick(i, i) for i in range(1, 11)]
        selected = _select_diverse(picks, candidates, max_per_country=3, max_per_topic=3)
        # Backfill relaxes the cap rather than shipping < 10.
        assert len(selected) == 10
        assert {4, 5}.issubset({p.cluster_id for p in selected})

    def test_unknown_cluster_id_is_skipped(self):
        candidates = _candidates({1: ("Ireland", "Housing")})
        picks = [_pick(1, 1), _pick(2, 999)]
        selected = _select_diverse(picks, candidates, max_per_country=3, max_per_topic=3)
        assert [p.cluster_id for p in selected] == [1]

    def test_missing_country_or_topic_never_counts_against_cap(self):
        candidates = _candidates({i: (None, None) for i in range(1, 12)})
        picks = [_pick(i, i) for i in range(1, 12)]
        selected = _select_diverse(picks, candidates, max_per_country=3, max_per_topic=3)
        assert len(selected) == 10


class TestDominantTopic:
    def test_no_topics_returns_none(self):
        assert dominant_topic(None, ["A", "B"]) is None
        assert dominant_topic("", ["A", "B"]) is None

    def test_uses_priority_order_over_first_match(self):
        assert dominant_topic("Trade,Housing", ["Housing", "Trade"]) == "Housing"

    def test_falls_back_to_alphabetical_when_no_priority_match(self):
        assert dominant_topic("Zeta,Alpha", ["Housing", "Trade"]) == "Alpha"


class TestDominantCountry:
    def test_no_countries_returns_none(self):
        assert dominant_country(None) is None
        assert dominant_country("") is None

    def test_single_country_passthrough(self):
        assert dominant_country("Ireland") == "Ireland"

    def test_multi_country_picks_first_position_not_alphabetical(self):
        # articles.country is ranked by evidence strength, most-relevant
        # first (pipeline.geo.detect_countries) -- position 0 wins even
        # when it sorts after later entries alphabetically.
        assert dominant_country("United States,China/Greater China") == "United States"


class TestRecentTop10ClusterIds:
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE daily_top10 (date TEXT, rank INTEGER, cluster_id INTEGER, rationale TEXT, "
            "PRIMARY KEY (date, rank))"
        )
        return conn

    def test_includes_clusters_within_lookback_window(self):
        conn = self._conn()
        today = utc_today()
        two_days_ago = (today - timedelta(days=2)).isoformat()
        conn.execute(
            "INSERT INTO daily_top10 VALUES (?, 1, 101, 'reason')", (two_days_ago,)
        )
        conn.commit()
        assert _recent_top10_cluster_ids(conn, lookback_days=3) == {101}

    def test_excludes_clusters_outside_lookback_window(self):
        conn = self._conn()
        today = utc_today()
        five_days_ago = (today - timedelta(days=5)).isoformat()
        conn.execute(
            "INSERT INTO daily_top10 VALUES (?, 1, 202, 'reason')", (five_days_ago,)
        )
        conn.commit()
        assert _recent_top10_cluster_ids(conn, lookback_days=3) == set()

    def test_excludes_todays_rows(self):
        conn = self._conn()
        today_str = utc_today().isoformat()
        conn.execute(
            "INSERT INTO daily_top10 VALUES (?, 1, 303, 'reason')", (today_str,)
        )
        conn.commit()
        assert _recent_top10_cluster_ids(conn, lookback_days=3) == set()

    def test_dedupes_cluster_ids_across_days(self):
        conn = self._conn()
        today = utc_today()
        d1 = (today - timedelta(days=1)).isoformat()
        d2 = (today - timedelta(days=2)).isoformat()
        conn.execute("INSERT INTO daily_top10 VALUES (?, 1, 404, 'r')", (d1,))
        conn.execute("INSERT INTO daily_top10 VALUES (?, 3, 404, 'r')", (d2,))
        conn.commit()
        assert _recent_top10_cluster_ids(conn, lookback_days=3) == {404}


class TestLoadCandidatesWithFallback:
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE articles (url_hash TEXT PRIMARY KEY, title TEXT, summary TEXT, "
            "country TEXT, topics TEXT, published TEXT, fetched TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE clusters (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "representative_article TEXT REFERENCES articles(url_hash))"
        )
        return conn

    def _insert(self, conn, url_hash, hours_ago):
        fetched = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
        conn.execute(
            "INSERT INTO articles (url_hash, title, summary, country, topics, published, fetched) "
            "VALUES (?, 't', 's', 'Ireland', 'Trade', NULL, ?)",
            (url_hash, fetched),
        )
        conn.execute(
            "INSERT INTO clusters (representative_article) VALUES (?)", (url_hash,)
        )

    def test_normal_path_uses_24h_window_without_widening(self):
        conn = self._conn()
        for i in range(30):
            self._insert(conn, f"h{i}", hours_ago=1)
        # Outside the 24h window -- must not be picked up without widening.
        self._insert(conn, "stale", hours_ago=30)
        conn.commit()

        candidates = _load_candidates(conn, 24, 40)
        assert len(candidates) == 30

    def test_thin_24h_window_widens_to_48h(self):
        conn = self._conn()
        # Only 10 candidates within 24h -- below the default min_candidates
        # of 25, so the fallback should kick in.
        for i in range(10):
            self._insert(conn, f"recent{i}", hours_ago=1)
        # 15 more that only show up once the window widens to 48h.
        for i in range(15):
            self._insert(conn, f"older{i}", hours_ago=30)
        conn.commit()


        candidates, widened = _load_candidates_with_fallback(
            conn, candidate_window_hours=24, min_candidates=25, max_candidates=40
        )
        assert widened is True
        assert len(candidates) == 25

    def test_widening_still_excludes_articles_older_than_widened_window(self):
        conn = self._conn()
        for i in range(5):
            self._insert(conn, f"recent{i}", hours_ago=1)
        # Older than even the widened 48h window.
        self._insert(conn, "ancient", hours_ago=WIDENED_CANDIDATE_WINDOW_HOURS + 1)
        conn.commit()


        candidates, widened = _load_candidates_with_fallback(
            conn, candidate_window_hours=24, min_candidates=25, max_candidates=40
        )
        assert widened is True
        assert len(candidates) == 5

    def test_sufficient_24h_pool_does_not_widen(self):
        conn = self._conn()
        for i in range(25):
            self._insert(conn, f"h{i}", hours_ago=1)
        conn.commit()


        candidates, widened = _load_candidates_with_fallback(
            conn, candidate_window_hours=24, min_candidates=25, max_candidates=40
        )
        assert widened is False
        assert len(candidates) == 25


class TestCuratorSchema:
    def test_valid_payload_parses(self):
        output = CuratorOutput.model_validate(
            {"picks": [{"rank": 1, "cluster_id": 7, "rationale": "big deal"}]}
        )
        assert output.picks[0].cluster_id == 7

    def test_missing_field_raises_validation_error(self):
        with pytest.raises(ValidationError):
            CuratorOutput.model_validate({"picks": [{"rank": 1, "rationale": "no id"}]})

    def test_wrong_type_raises_validation_error(self):
        with pytest.raises(ValidationError):
            CuratorOutput.model_validate({"picks": [{"rank": "first", "cluster_id": 7, "rationale": "x"}]})

    def test_missing_picks_key_raises_validation_error(self):
        with pytest.raises(ValidationError):
            CuratorOutput.model_validate({"rankings": []})
