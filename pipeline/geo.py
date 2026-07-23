"""Subject-country detection: which country/region an article is actually
ABOUT, as opposed to feeds.country (config/feeds.yaml), which is just where
the source outlet is based -- a Bloomberg (US) piece about the Irish housing
market should tag country=Ireland, not country=United States. See
pipeline.tag, which calls detect_countries() instead of inheriting
feeds.country the way it used to.

Two matching passes, both driven by config/countries.yaml (see that file's
header for the full rationale):
  1. spaCy NER (en_core_web_sm, GPE/LOC/NORP/ORG entities) against a
     length-capped excerpt, entity text looked up in the places/demonyms
     gazetteer. Runs entirely locally (no torch, same "no external API" spirit
     as pipeline.embed's fastembed usage) -- catches country/city/nationality
     mentions with actual context, not just string luck.
  2. Direct substring/word-boundary search over the full text for
     institution names (central banks, ministries, etc.) -- acronyms and
     less-common institutions are exactly what a small NER model tends to
     miss, so this is a deliberate second pass rather than relying on step 1
     alone.

An article matching nothing in either pass gets no country from this module;
pipeline.tag falls back to "International" rather than the source feed's
country, since "unclear" is a more honest answer than "wherever we found it."
"""

from __future__ import annotations

import re
from functools import lru_cache

import yaml

from pipeline.config import get_config, resolve_path

# NER cost scales with input length, and a news article's subject is
# established in its opening paragraphs -- capping keeps this pass fast on a
# large per-run backlog without meaningfully hurting accuracy. The
# institution substring pass below is cheap regardless of length and runs
# over the full text.
_NER_CHAR_CAP = 4000

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


def detect_countries(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []

    matched: set[str] = set()

    places = _place_index()
    nlp = _get_nlp()
    doc = nlp(text[:_NER_CHAR_CAP])
    for ent in doc.ents:
        if ent.label_ not in _ENTITY_LABELS:
            continue
        country = places.get(ent.text.lower().strip())
        if country:
            matched.add(country)

    blob = text.lower()
    for country, matcher in _institution_patterns():
        hit = matcher.search(blob) if isinstance(matcher, re.Pattern) else matcher in blob
        if hit:
            matched.add(country)

    return sorted(matched)
