"""Deterministic local embeddings, by feature hashing.

This is a lexical space, not a semantic one, and the distinction matters: it scores
"built REST services in Python" against "Python backend development" well, because they
share tokens, and scores it against "server-side engineering" near zero, because they
share none. That is precisely the weakness a real embedding model fixes, and precisely
why the fit score's calibration eval has to run against Vertex before any threshold is
believed.

It earns its place anyway. It makes the product demonstrable with no credentials, it
makes the hybrid retrieval path in M3 testable end to end offline, and it is a floor
the embedding model's contribution can be measured against.

The construction is standard feature hashing: word unigrams, word bigrams and character
trigrams are hashed into a fixed number of buckets with a sign drawn from the same
hash, then the vector is L2-normalised so cosine similarity is a dot product. Signed
hashing matters — without it, collisions can only add, and every pair of long texts
looks similar.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#.]*")
#: Words carrying no discriminative signal in this domain. Short, because an aggressive
#: stop list removes the tokens that distinguish two requirements from each other.
# fmt: off
_STOPWORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "in", "is", "it", "its", "of", "on", "or", "that", "the", "to", "with", "will",
    "you", "your", "we", "our", "their", "this", "those", "these",
])
# fmt: on
#: Character n-gram width. Three is the usual choice: long enough to be more than a
#: letter frequency, short enough to survive the suffix differences that separate
#: "engineer", "engineering" and "engineered".
_NGRAM = 3


def embed_text(text: str, dim: int) -> list[float]:
    """Project one text into `dim` dimensions. Deterministic across processes.

    `hashlib.blake2b` rather than the built-in `hash`, which is randomised per process
    by PYTHONHASHSEED. A vector stored today has to compare against one computed
    tomorrow, so the hash has to be stable outside this process's lifetime.
    """
    buckets = [0.0] * dim
    lowered = text.lower()
    tokens = [token for token in _TOKEN.findall(lowered) if token not in _STOPWORDS]

    for token in tokens:
        _accumulate(buckets, f"w:{token}", dim, weight=1.0)

    for first, second in itertools.pairwise(tokens):
        # Bigrams carry the phrase structure that separates "machine learning" from two
        # unrelated mentions of "machine" and "learning".
        _accumulate(buckets, f"b:{first}_{second}", dim, weight=0.7)

    condensed = " ".join(tokens)
    for index in range(len(condensed) - _NGRAM + 1):
        _accumulate(buckets, f"c:{condensed[index : index + _NGRAM]}", dim, weight=0.3)

    norm = math.sqrt(sum(value * value for value in buckets))
    if norm == 0.0:
        # An empty or fully-stopworded text. A zero vector would make every cosine
        # comparison undefined, so it becomes a fixed unit vector instead, which
        # compares as maximally dissimilar to real text rather than as an error.
        buckets[0] = 1.0
        return buckets
    return [value / norm for value in buckets]


def _accumulate(buckets: list[float], feature: str, dim: int, *, weight: float) -> None:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    # The low bit picks the sign, the rest picks the bucket, so one hash does both.
    sign = 1.0 if value & 1 else -1.0
    buckets[(value >> 1) % dim] += sign * weight
