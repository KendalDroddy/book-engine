"""Model-backed implementations of the provider-neutral embedding contract."""

from typing import Any

import httpx

from book_engine.recommendations.types import EmbeddingBatch


class OpenAIEmbeddingProvider:
    """Batched OpenAI embeddings with usage returned for durable provenance."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
        input_cost_per_million_tokens: float = 0.02,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("An OpenAI API key is required")
        self.model = model
        self.dimensions = dimensions
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._input_cost = input_cost_per_million_tokens
        self._client = client

    def embed_many(self, texts: list[str]) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch((), {"model": self.model}, self._usage(0, 0))
        payload: dict[str, object] = {
            "input": texts,
            "model": self.model,
            "encoding_format": "float",
            "dimensions": self.dimensions,
        }
        if self._client is None:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.post(
                    f"{self._base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
        else:
            response = self._client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        response.raise_for_status()
        body = response.json()
        data = sorted(body["data"], key=lambda item: item["index"])
        vectors = tuple(
            tuple(float(value) for value in item["embedding"]) for item in data
        )
        raw_usage: dict[str, Any] = body.get("usage", {})
        prompt_tokens = int(raw_usage.get("prompt_tokens", 0))
        total_tokens = int(raw_usage.get("total_tokens", prompt_tokens))
        return EmbeddingBatch(
            vectors=vectors,
            response_metadata={
                "object": body.get("object"),
                "model": body.get("model"),
                "request_id": response.headers.get("x-request-id"),
                "vector_count": len(vectors),
            },
            usage=self._usage(prompt_tokens, total_tokens),
        )

    def _usage(self, prompt_tokens: int, total_tokens: int) -> dict[str, Any]:
        return {
            "prompt_tokens": prompt_tokens,
            "total_tokens": total_tokens,
            "api_requests": int(total_tokens > 0),
            "input_cost_per_million_tokens_usd": self._input_cost,
            "estimated_cost_usd": round(
                prompt_tokens * self._input_cost / 1_000_000, 8
            ),
        }
