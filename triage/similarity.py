"""Finds past resolved issues that are similar to a new one.

Primary method: sentence embeddings (semantic similarity, understands rewording).
Fallback method: TF-IDF cosine similarity if sentence-transformers is not installed
or the model cannot be downloaded.
"""
import logging

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from . import config

log = logging.getLogger(__name__)

EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # small, free, runs locally on CPU


class SimilarityIndex:
    def __init__(self):
        self.method = "tfidf"
        self.duplicate_threshold = 0.45
        self.min_score = 0.15
        self._encoder = None
        if config.use_embeddings():
            try:
                from sentence_transformers import SentenceTransformer
                self._encoder = SentenceTransformer(EMBEDDING_MODEL)
                self.method = "embeddings"
                self.duplicate_threshold = 0.75
                self.min_score = 0.35
            except Exception as exc:  # missing package or no network to download model
                log.warning("Embeddings unavailable (%s); using TF-IDF similarity.", exc)
        self.rows, self.matrix, self._tfidf = [], None, None

    @staticmethod
    def _text(row):
        return f"{row['title']}. {row['description']}"

    def build(self, rows):
        self.rows = rows
        texts = [self._text(r) for r in rows]
        if not texts:
            self.matrix = None
            return self
        if self.method == "embeddings":
            self.matrix = self._encoder.encode(texts, normalize_embeddings=True)
        else:
            self._tfidf = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True,
                                          stop_words="english")
            self.matrix = self._tfidf.fit_transform(texts)
        return self

    def search(self, text, k=config.TOP_K_SIMILAR):
        if self.matrix is None or not self.rows:
            return []
        if self.method == "embeddings":
            q = self._encoder.encode([text], normalize_embeddings=True)[0]
            scores = self.matrix @ q
        else:
            q = self._tfidf.transform([text])
            scores = (self.matrix @ q.T).toarray().ravel()  # tf-idf rows are L2-normalised
        order = np.argsort(-scores)[:k]
        return [{"id": self.rows[i]["id"], "title": self.rows[i]["title"],
                 "resolution": self.rows[i]["resolution"],
                 "similarity": round(float(scores[i]), 3)}
                for i in order if scores[i] >= self.min_score]
