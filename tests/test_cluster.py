import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import pipeline.cluster as cluster_mod
from pipeline.embed import pack_vector

MODEL = "test-model"

SCHEMA = """
CREATE TABLE articles (
    url_hash TEXT PRIMARY KEY,
    feed_id INTEGER,
    title TEXT,
    fetched TEXT,
    summary TEXT,
    topics TEXT,
    score INTEGER,
    cluster_id INTEGER
);
CREATE TABLE clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT,
    label TEXT,
    representative_article TEXT,
    parent_cluster_id INTEGER
);
CREATE TABLE article_embeddings (
    url_hash TEXT,
    model TEXT,
    vector BLOB,
    created TEXT,
    PRIMARY KEY (url_hash, model)
);
"""

DEFAULT_CFG = {
    "cluster": {
        "window_days": 3,
        "title_shared_words": 3,
        "embedding_cosine_threshold": 0.62,
        "chain_window_days": 14,
        "chain_cosine_threshold": 0.78,
    },
    "embed": {"model": MODEL},
}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    @contextmanager
    def get_connection():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    monkeypatch.setattr(cluster_mod, "get_connection", get_connection)
    monkeypatch.setattr(cluster_mod, "get_config", lambda: DEFAULT_CFG)
    return path


def _insert_article(path, url_hash, title, fetched, topics="Economy", score=1,
                     cluster_id=None, vector=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO articles (url_hash, feed_id, title, fetched, summary, topics, score, cluster_id) "
        "VALUES (?, 1, ?, ?, 'summary', ?, ?, ?)",
        (url_hash, title, fetched, topics, score, cluster_id),
    )
    if vector is not None:
        conn.execute(
            "INSERT INTO article_embeddings (url_hash, model, vector, created) VALUES (?, ?, ?, ?)",
            (url_hash, MODEL, pack_vector(np.array(vector, dtype=np.float32)), fetched),
        )
    conn.commit()
    conn.close()


def _insert_cluster(path, created, representative_article):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO clusters (created, label, representative_article) VALUES (?, ?, ?)",
        (created, "label", representative_article),
    )
    conn.commit()
    cluster_id = cur.lastrowid
    conn.close()
    return cluster_id


def _cluster_id_of(path, url_hash):
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT cluster_id FROM articles WHERE url_hash = ?", (url_hash,)).fetchone()
    conn.close()
    return row[0]


SIMILAR_A = [1.0, 0.05]
SIMILAR_B = [0.98, 0.15]   # cosine with SIMILAR_A well above 0.62
DISSIMILAR = [0.0, 1.0]    # orthogonal to SIMILAR_A -- cosine 0


class TestSigWords:
    def test_strips_stopwords_and_short_words(self):
        assert cluster_mod._sig_words("The UK cut interest rates by up to 25bp") == {
            "cut", "interest", "rates", "25bp",
        }

    def test_case_and_punctuation_insensitive(self):
        assert cluster_mod._sig_words("Inflation, Inflation!") == {"inflation"}


class TestEmbeddingBasedClustering:
    def test_similar_summaries_form_a_new_cluster(self, db):
        now = _iso(_now())
        _insert_article(db, "a1", "Fed raises interest rates", now, vector=SIMILAR_A)
        _insert_article(db, "a2", "Federal Reserve hikes rates", now, vector=SIMILAR_B)

        stats = cluster_mod.process_all()

        assert stats["new_clusters"] == 1
        assert stats["new_cluster_members"] == 2
        assert _cluster_id_of(db, "a1") == _cluster_id_of(db, "a2")

    def test_dissimilar_summaries_stay_in_separate_clusters(self, db):
        now = _iso(_now())
        _insert_article(db, "a1", "Fed raises interest rates", now, vector=SIMILAR_A)
        _insert_article(db, "a2", "England wins the World Cup final", now, vector=DISSIMILAR)

        stats = cluster_mod.process_all()

        assert stats["new_clusters"] == 2
        assert _cluster_id_of(db, "a1") != _cluster_id_of(db, "a2")

    def test_candidate_joins_existing_recent_cluster(self, db):
        now = _iso(_now())
        _insert_article(db, "lead", "Fed raises interest rates", now, vector=SIMILAR_A, cluster_id=1)
        _insert_cluster(db, now, "lead")
        _insert_article(db, "a2", "Federal Reserve hikes rates", now, vector=SIMILAR_B)

        stats = cluster_mod.process_all()

        assert stats["joined_existing"] == 1
        assert stats["new_clusters"] == 0
        assert _cluster_id_of(db, "a2") == 1

    def test_articles_outside_window_are_ignored(self, db):
        old = _iso(_now() - timedelta(days=10))
        now = _iso(_now())
        _insert_article(db, "old", "Fed raises interest rates", old, vector=SIMILAR_A)
        _insert_article(db, "new", "Federal Reserve hikes rates", now, vector=SIMILAR_B)

        stats = cluster_mod.process_all()

        # "old" is outside window_days=3, so it's neither a lead nor a
        # candidate -- "new" forms its own singleton cluster instead of
        # matching against it.
        assert stats["new_clusters"] == 1
        assert stats["new_cluster_members"] == 1

    def test_missing_embedding_falls_back_to_title_word_overlap(self, db):
        now = _iso(_now())
        _insert_article(db, "a1", "Bank of England raises interest rates sharply", now)
        _insert_article(db, "a2", "Bank of England raises rates again sharply", now)

        stats = cluster_mod.process_all()

        assert stats["new_clusters"] == 1
        assert stats["new_cluster_members"] == 2

    def test_missing_embedding_and_no_title_overlap_stay_separate(self, db):
        now = _iso(_now())
        _insert_article(db, "a1", "Bank of England raises interest rates", now)
        _insert_article(db, "a2", "England wins the World Cup final tonight", now)

        stats = cluster_mod.process_all()

        assert stats["new_clusters"] == 2


class TestChainLinking:
    def test_new_cluster_links_to_similar_older_cluster(self, db):
        old_created = _iso(_now() - timedelta(days=10))
        _insert_article(db, "old", "Fed raises interest rates", old_created,
                         vector=SIMILAR_A, cluster_id=1)
        _insert_cluster(db, old_created, "old")

        now = _iso(_now())
        _insert_article(db, "new", "Federal Reserve hikes rates again", now, vector=SIMILAR_B)

        stats = cluster_mod.process_all()

        assert stats["chained"] == 1
        conn = sqlite3.connect(db)
        parent = conn.execute(
            "SELECT parent_cluster_id FROM clusters WHERE representative_article = 'new'"
        ).fetchone()[0]
        conn.close()
        assert parent == 1

    def test_new_cluster_does_not_link_to_dissimilar_older_cluster(self, db):
        old_created = _iso(_now() - timedelta(days=10))
        _insert_article(db, "old", "England wins the World Cup final", old_created,
                         vector=DISSIMILAR, cluster_id=1)
        _insert_cluster(db, old_created, "old")

        now = _iso(_now())
        _insert_article(db, "new", "Fed raises interest rates", now, vector=SIMILAR_A)

        stats = cluster_mod.process_all()

        assert stats["chained"] == 0

    def test_cluster_older_than_chain_window_is_ignored(self, db):
        too_old = _iso(_now() - timedelta(days=20))
        _insert_article(db, "old", "Fed raises interest rates", too_old,
                         vector=SIMILAR_A, cluster_id=1)
        _insert_cluster(db, too_old, "old")

        now = _iso(_now())
        _insert_article(db, "new", "Federal Reserve hikes rates again", now, vector=SIMILAR_B)

        stats = cluster_mod.process_all()

        assert stats["chained"] == 0
