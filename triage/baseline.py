"""Conventional ML baseline: TF-IDF + logistic regression.

Serves two purposes:
1. Fallback when the LLM is unavailable (offline, rate limited, invalid output).
2. The 'feasible alternative' the LLM is compared against in evaluate.py.
"""
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

FIELDS = {"type": "final_type", "module": "final_module", "priority": "final_priority"}


class BaselineClassifier:
    def __init__(self):
        self.models = {}
        self.constants = {}

    def fit(self, rows):
        texts = [f"{r['title']}. {r['description']}" for r in rows]
        for field, column in FIELDS.items():
            y = [r[column] for r in rows]
            if len(set(y)) < 2:  # LogisticRegression needs at least two classes
                self.constants[field] = y[0] if y else None
                continue
            model = make_pipeline(
                TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english"),
                LogisticRegression(max_iter=2000, class_weight="balanced"))
            model.fit(texts, y)
            self.models[field] = model
        return self

    def predict(self, text):
        result, confidences = {}, []
        for field in FIELDS:
            if field in self.models:
                model = self.models[field]
                probs = model.predict_proba([text])[0]
                best = probs.argmax()
                result[field] = model.classes_[best]
                confidences.append(float(probs[best]))
            else:
                result[field] = self.constants.get(field)
                confidences.append(1.0)
        # Average of per-field probabilities: a rough, not calibrated, confidence.
        result["confidence"] = round(sum(confidences) / len(confidences), 2)
        return result
