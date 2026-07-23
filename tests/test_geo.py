import pytest

pytest.importorskip("spacy")

from pipeline.geo import detect_countries


class TestDetectCountries:
    def test_source_country_is_not_the_signal(self):
        # A US outlet's own dateline shouldn't matter -- only the subject.
        result = detect_countries(
            "Bloomberg reports the Irish housing market continues to "
            "overheat as Dublin rents climb."
        )
        assert result == ["Ireland"]

    def test_institution_only_reference_still_resolves(self):
        # No country name at all -- "the Fed" / Washington carry it.
        result = detect_countries(
            "The Federal Reserve held rates steady on Wednesday, Jerome "
            "Powell said in Washington."
        )
        assert result == ["United States"]

    def test_multi_country_article_tags_both(self):
        result = detect_countries(
            "US-China trade tensions escalate as Beijing imposes new "
            "tariffs on American goods."
        )
        assert result == ["China/Greater China", "United States"]

    def test_multilateral_institution_maps_to_international(self):
        result = detect_countries(
            "The IMF warned of a global recession risk in its latest "
            "World Economic Outlook."
        )
        assert result == ["International"]

    def test_no_match_returns_empty_not_a_guess(self):
        result = detect_countries(
            "A general piece about inflation trends worldwide with no "
            "specific country named."
        )
        assert result == []

    def test_empty_text(self):
        assert detect_countries("") == []
        assert detect_countries(None) == []
