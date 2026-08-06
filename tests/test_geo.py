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

    def test_title_mention_outranks_body_only_mention(self):
        # Same country mentioned once up front vs. a different country
        # mentioned only deep in the body -- the title/opening mention must
        # win even though both are single mentions.
        filler = "This part of the story is unrelated padding text. " * 20
        text = (
            "Dublin house prices keep rising, economists say. "
            + filler
            + "A separate report out of Ottawa touched on housing too."
        )
        result = detect_countries(text)
        assert result[0] == "Ireland"
        assert "Canada" in result

    def test_caps_at_three_countries(self):
        text = (
            "A wide-ranging piece touches on Dublin, Beijing, Ottawa, "
            "Canberra, London, Washington and Riyadh, in no particular "
            "order, with roughly equal space given to each."
        )
        result = detect_countries(text)
        assert len(result) <= 3

    def test_spice_bag_worked_example_ranks_ireland_first(self):
        # TAGGING_SPEC.md Phase 1 worked example: a food/diaspora piece
        # about Ireland that, before ranking, dragged in seven countries
        # (including China/Greater China) with equal standing. Ranked by
        # position-weighted evidence, Ireland should win and the result
        # should be capped at 3.
        title = (
            "Kelly Earley: I like spice bags as much as the next person, "
            "but we need to draw the line"
        )
        opening = (
            "Spice bags have become a beloved staple of the Irish "
            "diaspora, a symbol of Ireland's late-night food culture "
            "found from Dublin to Cork."
        )
        body = (
            "The dish has fans in Australia and New Zealand who miss "
            "home. Expats in Canada have opened their own versions. "
            "Even in China and across Greater China the trend has been "
            "noted online. Commentators in the Middle East have written "
            "about diaspora food more broadly. A similar story played "
            "out in the United Kingdom among Irish communities. And in "
            "the United States, Irish pubs have started serving spice "
            "bags too."
        )
        result = detect_countries(f"{title}. {opening} {body}")
        assert result[0] == "Ireland"
        assert len(result) <= 3
        assert "China/Greater China" not in result
