"""Subject-country detection: which country/region an article is actually
ABOUT, as opposed to feeds.country (config/feeds.yaml), which is just where
the source outlet is based -- a Bloomberg (US) piece about the Irish housing
market should tag country=Ireland, not country=United States. See
pipeline.tag, which calls detect_countries() instead of inheriting
feeds.country the way it used to.

Two matching passes, both driven by config/countries.yaml (see that file's
header for the full rationale), both scoped to the same length-capped excerpt
(_NER_CHAR_CAP) so neither pass has visibility the other lacks:
  1. spaCy NER (en_core_web_sm, GPE/LOC/NORP/ORG entities) against the
     capped excerpt, entity text looked up in the places/demonyms
     gazetteer. Runs entirely locally (no torch, same "no external API" spirit
     as pipeline.embed's fastembed usage) -- catches country/city/nationality
     mentions with actual context, not just string luck.
  2. Direct substring/word-boundary search over the same capped excerpt for
     institution names (central banks, ministries, etc.) -- acronyms and
     less-common institutions are exactly what a small NER model tends to
     miss, so this is a deliberate second pass rather than relying on step 1
     alone.

Countries are ranked by evidence strength rather than collected into a set:
each mention is weighted by position (title/opening-paragraph mentions --
within _HEAD_CHAR_CAP -- outrank body-only ones) and summed per country, so a
country named once in passing doesn't stand shoulder-to-shoulder with the one
the article is actually about. Only the top _MAX_COUNTRIES survive; past that
the tags carry no information and just pollute the site's filters.

An article matching nothing in either pass gets no country from this module;
pipeline.tag falls back to "International" rather than the source feed's
country, since "unclear" is a more honest answer than "wherever we found it."
"""

from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache

import yaml

from pipeline.config import get_config, resolve_path

# NER cost scales with input length, and a news article's subject is
# established in its opening paragraphs -- capping keeps this pass fast on a
# large per-run backlog without meaningfully hurting accuracy. The
# institution substring pass is cheap regardless of length but is capped to
# the same excerpt so both passes agree on how much of the article they've
# actually seen -- otherwise a country named only past this cap would count
# via institution matching but not NER, an arbitrary asymmetry.
_NER_CHAR_CAP = 4000

# Mentions within this leading slice (title + opening paragraph, once title
# and body are joined into one blob by callers) count for more than mentions
# further in -- the position weighting that makes the ranking meaningful.
_HEAD_CHAR_CAP = 500

_WEIGHT_HEAD = 3
_WEIGHT_BODY = 1

# Past this many countries the tags stop carrying information (see module
# docstring / TAGGING_SPEC.md Phase 1).
_MAX_COUNTRIES = 3

_ENTITY_LABELS = {"GPE", "LOC", "NORP", "ORG"}


@lru_cache(maxsize=1)
def _load_gazetteer() -> dict:
    cfg = get_config()
    path = resolve_path(cfg["paths"]["countries_yaml"])
    with open(path, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("countries") or {}


@lru_cache(maxsize=1)
def _place_index() -> dict[str, str]:
    """Lowercased place/demonym name -> country bucket, for exact lookup
    against spaCy entity text (already segmented, so no substring pitfalls)."""
    idx: dict[str, str] = {}
    for country, data in _load_gazetteer().items():
        for p in (data or {}).get("places") or []:
            idx[p.lower()] = country
        for d in (data or {}).get("demonyms") or []:
            idx[d.lower()] = country
    return idx


@lru_cache(maxsize=1)
def _institution_patterns() -> list[tuple[str, re.Pattern | str]]:
    """(country, matcher) pairs -- a compiled word-boundary regex for
    single-word institution names (so "fed" doesn't match "federal" in an
    unrelated sense... it still can via 'the fed' phrasing, kept as a
    multi-word entry instead), a plain lowercase string for multi-word
    phrases matched by substring. Mirrors pipeline.tag.score_and_tag's
    existing convention."""
    entries: list[tuple[str, re.Pattern | str]] = []
    for country, data in _load_gazetteer().items():
        for kw in (data or {}).get("institutions") or []:
            kw_l = kw.lower()
            if " " in kw_l:
                entries.append((country, kw_l))
            else:
                entries.append((country, re.compile(r"\b" + re.escape(kw_l) + r"\b")))
    return entries


@lru_cache(maxsize=1)
def _get_nlp():
    """Cached per-process. Import is local so modules that don't need
    detection (e.g. anything just reading articles.country back out) don't
    require spaCy to be installed at import time. Parser/tagger/lemmatizer
    disabled -- only NER is needed here, and skipping the rest is the single
    biggest speedup available for this pipeline stage."""
    import spacy

    return spacy.load("en_core_web_sm", exclude=["parser", "tagger", "lemmatizer", "attribute_ruler"])


def _weight_for(pos: int) -> int:
    return _WEIGHT_HEAD if pos < _HEAD_CHAR_CAP else _WEIGHT_BODY


def detect_countries(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []

    scoped = text[:_NER_CHAR_CAP]
    scores: dict[str, int] = defaultdict(int)

    places = _place_index()
    nlp = _get_nlp()
    doc = nlp(scoped)
    for ent in doc.ents:
        if ent.label_ not in _ENTITY_LABELS:
            continue
        country = places.get(ent.text.lower().strip())
        if country:
            scores[country] += _weight_for(ent.start_char)

    blob = scoped.lower()
    for country, matcher in _institution_patterns():
        if isinstance(matcher, re.Pattern):
            positions = [m.start() for m in matcher.finditer(blob)]
        else:
            positions = []
            start = 0
            while True:
                idx = blob.find(matcher, start)
                if idx == -1:
                    break
                positions.append(idx)
                start = idx + len(matcher)
        for pos in positions:
            scores[country] += _weight_for(pos)

    if not scores:
        return []

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [country for country, _ in ranked[:_MAX_COUNTRIES]]
