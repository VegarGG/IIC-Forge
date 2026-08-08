"""Text embedder for Stage-2 dedupe.

The SQLite vec_index column is float[384], matching all-MiniLM-L6-v2.
Production uses ``SentenceTransformerEmbedder``; tests use ``MockEmbedder``
(deterministic L2-normalized vectors derived from SHA-256 of the input).
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Any, List, Protocol


DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
# Pin the model artifact used by the production image. This prevents a mutable
# Hugging Face branch from changing dedupe behavior between image rebuilds.
DEFAULT_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> List[float]: ...


class MockEmbedder:
    """Deterministic L2-normalized hash-vector for tests.

    Two identical inputs give identical vectors. Different inputs give
    cosine far below 0.92, so the test suite never crosses the dedupe
    threshold accidentally.
    """

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed(self, text: str) -> List[float]:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        # Expand the 32-byte seed into self.dim float chunks via repeated SHA-256.
        out: List[float] = []
        block = seed
        while len(out) < self.dim:
            block = hashlib.sha256(block).digest()
            for i in range(0, len(block), 4):
                if len(out) >= self.dim:
                    break
                # interpret 4 bytes as signed int → [-1, 1]
                (n,) = struct.unpack(">i", block[i:i+4])
                out.append(n / 2_147_483_648.0)
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / norm for x in out]


class SentenceTransformerEmbedder:
    """Production embedder. Model loads lazily on first .embed() call."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        *,
        revision: str | None = None,
    ) -> None:
        self._model_name = model_name
        self._revision = (
            DEFAULT_MODEL_REVISION
            if revision is None and model_name == DEFAULT_MODEL_NAME
            else revision
        )
        self._model = None  # lazy
        # all-MiniLM-L6-v2 is 384-dim; this is documented and stable.
        self.dim = 384

    def _ensure_loaded(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy import
            self._model = SentenceTransformer(
                self._model_name,
                revision=self._revision,
            )
        return self._model

    def load(self) -> None:
        """Eagerly load the model.

        Public API so callers (e.g. triage startup) can force a fail-fast,
        loud failure if sentence-transformers is missing or the model can't
        download — instead of silently zeroing out every event mid-soak.
        """
        self._ensure_loaded()

    def embed(self, text: str) -> List[float]:
        model = self._ensure_loaded()
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()
