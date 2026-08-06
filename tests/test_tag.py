"""Covers TAGGING_SPEC.md Phase 2: the LLM tag block that rides along on
pipeline.summarize's existing Cerebras call, and the keyword tagger's
demotion to a parse-failure fallback.

The Cerebras call itself is never made -- every test drives parse_response
with a canned completion, which is the only part of the loop whose behaviour
we actually control.
"""

import pytest

from pipeline.summarize import (
    Analysis,
    build_prompt,
    parse_response,
    rank_countries,
)
from pipeline.tag import load_keywords, load_themes, score_and_tag

THEMES = ("IRELAND", "HOUSING & PROPERTY", "MACROECONOMICS", "GLOBAL ECONOMY")
CANDIDATES = ["Ireland", "United Kingdom", "United States"]


def response(summary="Rents rose again.", topics="IRELAND", country="Ireland"):
    return f"SUMMARY: {summary}\nTOPICS: {topics}\nCOUNTRY: {country}"


class TestClosedTopicSet:
    def test_valid_themes_are_kept(self):
        result = parse_response(
            response(topics="IRELAND, HOUSING & PROPERTY"), THEMES, CANDIDATES
        )
        assert result.topics == ["IRELAND", "HOUSING & PROPERTY"]

    def test_invented_theme_is_dropped_not_coerced(self):
        # "PROPERTY MARKET" is close to a real theme; it must not be snapped
        # onto one -- guessing is the failure mode this phase exists to fix.
        result = parse_response(
            response(topics="IRELAND, PROPERTY MARKET"), THEMES, CANDIDATES
        )
        assert result.topics == ["IRELAND"]

    def test_case_and_bullet_noise_still_resolves_to_taxonomy_spelling(self):
        result = parse_response(
            response(topics="- housing & property\n- macroeconomics"), THEMES, CANDIDATES
        )
        assert result.topics == ["HOUSING & PROPERTY", "MACROECONOMICS"]

    def test_duplicates_collapse(self):
        result = parse_response(response(topics="IRELAND, IRELAND"), THEMES, CANDIDATES)
        assert result.topics == ["IRELAND"]

    def test_explicit_none_is_a_real_answer_not_a_failure(self):
        # An empty list means "the model declined", which is a usable result;
        # None would send the row to the keyword fallback instead.
        result = parse_response(response(topics="NONE"), THEMES, CANDIDATES)
        assert result.topics == []

    def test_all_topics_invalid_is_treated_as_unparsed(self):
        # Nothing recognisable and no explicit decline -- we cannot tell "no
        # theme fits" from "it ignored the list", so don't record an answer.
        result = parse_response(response(topics="Sport, Weather"), THEMES, CANDIDATES)
        assert result.topics is None

    def test_prompt_offers_every_theme_and_an_explicit_decline(self):
        prompt = build_prompt("t", "b", THEMES, CANDIDATES)
        for theme in THEMES:
            assert f"- {theme}" in prompt
        assert "NONE" in prompt


class TestCountryChoiceIsConstrainedToCandidates:
    def test_model_picks_from_the_candidate_list(self):
        result = parse_response(response(country="United States"), THEMES, CANDIDATES)
        assert result.country == "United States"

    def test_free_form_answer_outside_the_candidates_is_rejected(self):
        # The whole point of the hybrid design: NER guarantees the answer
        # lands in a real config/countries.yaml bucket.
        result = parse_response(response(country="Scotland"), THEMES, CANDIDATES)
        assert result.country is None

    def test_none_means_no_country_chosen(self):
        result = parse_response(response(country="NONE"), THEMES, CANDIDATES)
        assert result.country is None

    def test_prompt_lists_the_ranked_candidates(self):
        prompt = build_prompt("t", "b", THEMES, CANDIDATES)
        for candidate in CANDIDATES:
            assert f"- {candidate}" in prompt

    def test_prompt_with_no_candidates_does_not_invite_free_form(self):
        prompt = build_prompt("t", "b", THEMES, [])
        assert "COUNTRY: write NONE." in prompt


class TestRankCountries:
    def test_chosen_country_is_promoted_to_position_zero(self):
        # dominant_country reads position 0, so this is the fix landing.
        assert rank_countries("United States", CANDIDATES) == [
            "United States",
            "Ireland",
            "United Kingdom",
        ]

    def test_remaining_candidates_keep_phase_one_order(self):
        assert rank_countries("United Kingdom", CANDIDATES) == [
            "United Kingdom",
            "Ireland",
            "United States",
        ]

    def test_no_choice_falls_back_to_phase_one_ranking(self):
        assert rank_countries(None, CANDIDATES) == CANDIDATES

    def test_no_candidates_at_all_yields_nothing_to_tag(self):
        assert rank_countries(None, []) == []


class TestParseFailureFallsBackToKeywords:
    def test_missing_summary_marker_asks_for_a_retry(self):
        assert parse_response("Rents rose again in Dublin.", THEMES, CANDIDATES) is None

    def test_empty_summary_asks_for_a_retry(self):
        assert parse_response("SUMMARY:\nTOPICS: IRELAND", THEMES, CANDIDATES) is None

    def test_missing_topics_block_keeps_the_summary_but_drops_the_tags(self):
        result = parse_response("SUMMARY: Rents rose again.", THEMES, CANDIDATES)
        assert result == Analysis("Rents rose again.", None, None)

    def test_summary_spanning_several_lines_survives(self):
        raw = "SUMMARY: First sentence.\nSecond sentence.\nTOPICS: IRELAND\nCOUNTRY: Ireland"
        result = parse_response(raw, THEMES, CANDIDATES)
        assert result.summary == "First sentence.\nSecond sentence."
        assert result.topics == ["IRELAND"]


class TestKeywordTaggerSurvivesDemotion:
    """score_and_tag is no longer the primary path but must keep working:
    it is the parse-failure fallback, and it is still the only source of
    articles.score until TAGGING_SPEC.md Phase 4 replaces it."""

    def test_still_scores_and_tags(self):
        score, themes = score_and_tag(
            "The housing crisis deepened as house prices rose.", load_keywords()
        )
        assert score >= 2
        assert "HOUSING & PROPERTY" in themes

    def test_themes_offered_to_the_llm_match_the_keyword_taxonomy(self):
        # The two paths must agree on what a valid theme name is, or an
        # LLM-tagged row and a fallback-tagged row filter differently.
        assert set(load_themes()) == {kw["theme"] for kw in load_keywords()}

    def test_taxonomy_has_the_eight_themes_the_spec_assumes(self):
        assert len(load_themes()) == 8
