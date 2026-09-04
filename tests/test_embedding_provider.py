import json
from pathlib import Path

import httpx

from book_engine.recommendations.providers import OpenAIEmbeddingProvider

FIXTURES = Path(__file__).parent / "fixtures"


def test_openai_embedding_provider_parses_recorded_response_and_usage() -> None:
    body = json.loads((FIXTURES / "openai" / "embeddings.json").read_text())
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=body,
            headers={"x-request-id": "req_fixture"},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAIEmbeddingProvider(
        api_key="secret-fixture",
        dimensions=3,
        client=client,
    )

    result = provider.embed_many(["first book", "second book"])

    assert result.vectors == ((0.1, 0.2, 0.3), (0.4, 0.5, 0.6))
    assert result.response_metadata == {
        "object": "list",
        "model": "text-embedding-3-small",
        "request_id": "req_fixture",
        "vector_count": 2,
    }
    assert result.usage["prompt_tokens"] == 17
    assert result.usage["estimated_cost_usd"] == 0.00000034
    assert captured["authorization"] == "Bearer secret-fixture"
    assert captured["payload"] == {
        "input": ["first book", "second book"],
        "model": "text-embedding-3-small",
        "encoding_format": "float",
        "dimensions": 3,
    }
