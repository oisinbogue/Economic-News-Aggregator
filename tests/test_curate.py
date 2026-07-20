import sqlite3
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from pipeline.curate import (
    CuratorOutput,
    CuratorPick,
    _recent_top10_cluster_ids,
    _select_diverse,
    dominant_topic,
)


def _pick(rank, cluster_id, rationale="because"):
    return CuratorPick(rank=rank, cluster_id=cluster_id, rationale=rationale)


def _candidates(specs):
    """specs: {cluster_id: (country, topic_tag)}"""
    return {cid: {"country": country, "topic_tag": topic} for cid, (country, topic) in specs.items()}


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
        today = date.today()
        two_days_ago = (today - timedelta(days=2)).isoformat()
        conn.execute(
            "INSERT INTO daily_top10 VALUES (?, 1, 101, 'reason')", (two_days_ago,)
        )
        conn.commit()
        assert _recent_top10_cluster_ids(conn, lookback_days=3) == {101}

    def test_excludes_clusters_outside_lookback_window(self):
        conn = self._conn()
        today = date.today()
        five_days_ago = (today - timedelta(days=5)).isoformat()
        conn.execute(
            "INSERT INTO daily_top10 VALUES (?, 1, 202, 'reason')", (five_days_ago,)
        )
        conn.commit()
        assert _recent_top10_cluster_ids(conn, lookback_days=3) == set()

    def test_excludes_todays_rows(self):
        conn = self._conn()
        today_str = date.today().isoformat()
        conn.execute(
            "INSERT INTO daily_top10 VALUES (?, 1, 303, 'reason')", (today_str,)
        )
        conn.commit()
        assert _recent_top10_cluster_ids(conn, lookback_days=3) == set()

    def test_dedupes_cluster_ids_across_days(self):
        conn = self._conn()
        today = date.today()
        d1 = (today - timedelta(days=1)).isoformat()
        d2 = (today - timedelta(days=2)).isoformat()
        conn.execute("INSERT INTO daily_top10 VALUES (?, 1, 404, 'r')", (d1,))
        conn.execute("INSERT INTO daily_top10 VALUES (?, 3, 404, 'r')", (d2,))
        conn.commit()
        assert _recent_top10_cluster_ids(conn, lookback_days=3) == {404}


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
