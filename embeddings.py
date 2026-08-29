"""Text -> vector embeddings via Gemini, for the semantic-recall layer (storage lives in
database.py). Kept separate so database.py stays pure SQLite with no network dependency.

Fails soft: embed_text returns None on any error, so callers store/recall WITHOUT vectors
rather than crashing. The embedding model's output dimension must match database.EMBED_DIM.
"""
import logging
import os

from google import genai

logger = logging.getLogger(__name__)

#text-embedding-004 outputs 768-dim vectors and is cheap; keep in sync with database.EMBED_DIM
EMBED_MODEL = "text-embedding-004"

_client: genai.Client | None = None

def _get_client() -> genai.Client:
    #Lazily built (and reused) so importing this module doesn't require the API key to be set
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client

def embed_text(text: str) -> list | None:
    """Returns the embedding vector for `text`, or None on empty input or any API failure
    (the caller then simply skips vector storage/recall for this item)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        response = _get_client().models.embed_content(model=EMBED_MODEL, contents=text)
        return list(response.embeddings[0].values)
    except Exception as e:
        logger.warning("Embedding failed: %s", e)
        return None
