"""Embedding providers used by the local knowledge-base index."""

from __future__ import annotations

import os
from typing import Protocol

import openai


class EmbeddingConfigurationError(RuntimeError):
    """Raised when knowledge-base embedding configuration is unavailable."""


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int | None

    async def embed_query(self, text: str) -> list[float]: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatibleEmbeddingProvider:
    """Small adapter around the OpenAI-compatible embeddings endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        dimensions: int | None = None,
        batch_size: int = 32,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        self.batch_size = max(1, batch_size)
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)

    @classmethod
    def from_env(cls) -> "OpenAICompatibleEmbeddingProvider":
        api_key = os.environ.get("MINI_KB_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EmbeddingConfigurationError(
                "Knowledge-base embeddings are not configured. Set "
                "MINI_KB_EMBEDDING_API_KEY (or OPENAI_API_KEY)."
            )
        raw_dimensions = os.environ.get("MINI_KB_EMBEDDING_DIMENSIONS", "").strip()
        try:
            dimensions = int(raw_dimensions) if raw_dimensions else None
        except ValueError as exc:
            raise EmbeddingConfigurationError("MINI_KB_EMBEDDING_DIMENSIONS must be an integer.") from exc
        return cls(
            api_key=api_key,
            base_url=os.environ.get("MINI_KB_EMBEDDING_BASE_URL") or os.environ.get("OPENAI_BASE_URL"),
            model=os.environ.get("MINI_KB_EMBEDDING_MODEL", "text-embedding-3-small"),
            dimensions=dimensions,
            batch_size=int(os.environ.get("MINI_KB_EMBEDDING_BATCH_SIZE", "32")),
        )

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self.embed_documents([text])
        return vectors[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            kwargs: dict = {"model": self.model, "input": batch}
            if self.dimensions:
                kwargs["dimensions"] = self.dimensions
            response = await self._client.embeddings.create(**kwargs)
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors.extend([list(item.embedding) for item in ordered])
        if len(vectors) != len(texts):
            raise RuntimeError(f"Embedding provider returned {len(vectors)} vectors for {len(texts)} inputs.")
        actual_dimensions = len(vectors[0])
        if self.dimensions is not None and actual_dimensions != self.dimensions:
            raise RuntimeError(
                f"Embedding dimension mismatch: configured {self.dimensions}, provider returned {actual_dimensions}."
            )
        self.dimensions = actual_dimensions
        return vectors
