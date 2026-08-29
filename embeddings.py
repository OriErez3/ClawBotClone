"""Text -> vector embeddings via Gemini, for the semantic-recall layer (storage lives in
database.py). Kept separate so database.py stays pure SQLite with no network dependency.

Fails soft: embed_text returns None on any error, so callers store/recall WITHOUT vectors
rather than crashing. The embedding model's output dimension must match database.EMBED_DIM.
"""
import logging
import math
import os

from google import genai
from google.genai import types

import database

logger = logging.getLogger(__name__)

#gemini-embedding-001 defaults to 3072 dims but supports 768 (kept small/cheap); EMBED_DIM in
#database.py must match. Sub-3072 outputs aren't unit-normalized by the API, so we normalize
#below - both sqlite-vec's distance and the cosine used for tool selection assume unit vectors.
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = database.EMBED_DIM

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
        response = _get_client().models.embed_content(
            model=EMBED_MODEL, contents=text,
            config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
        )
        values = list(response.embeddings[0].values)
        norm = math.sqrt(sum(v * v for v in values))
        return [v / norm for v in values] if norm else values  #unit-normalize for cosine/L2
    except Exception as e:
        logger.warning("Embedding failed: %s", e)
        return None

def remember(source: str, text: str, ref: str | None = None, replace: bool = False) -> None:
    """Embeds `text` and stores it for later semantic recall. `source` tags provenance
    ('message' | 'memory' | 'email'). `ref` is an optional external id: with replace=True an
    existing entry for that ref is overwritten first (a memory fact that changed); otherwise a
    ref that already exists is skipped (dedup, e.g. an email seen before). Best-effort - does
    nothing if the text is empty or embedding/the vector store is unavailable, never raises."""
    text = (text or "").strip()
    if not text:
        return
    if ref and replace:
        database.delete_embeddings_by_ref(ref)
    elif ref and database.embedding_ref_exists(ref):
        return
    vector = embed_text(text)
    if vector:
        database.add_embedding(source, text, vector, ref)

def forget(ref: str) -> None:
    """Removes any stored embedding with this external ref (e.g. a deleted memory fact)."""
    database.delete_embeddings_by_ref(ref)

def recall(query: str, k: int = 5, exclude: set | None = None) -> list:
    """Embeds `query` and returns up to k semantically similar stored texts. Convenience
    wrapper around recall_with_vector for callers that don't already have the embedding."""
    if not database.VECTOR_ENABLED:
        return []  #skip the embed API call entirely when there's nothing to search
    query = (query or "").strip()
    if not query:
        return []
    return recall_with_vector(embed_text(query), k, exclude)

def recall_with_vector(vector: list | None, k: int = 5, exclude: set | None = None) -> list:
    """Returns up to k stored texts most similar to an already-computed `vector`, nearest first,
    each a {'source', 'text', 'distance'}. `exclude` is a set of texts to drop (e.g. messages
    already in the recent history window) so recall surfaces things NOT already in context.
    Lets a caller embed the query once and reuse the vector (e.g. for tool selection too)."""
    if not vector or not database.VECTOR_ENABLED:
        return []
    fetch = k + (len(exclude) if exclude else 0)  #over-fetch so exclusions still leave up to k
    results = database.search_embeddings(vector, k=fetch)
    if exclude:
        results = [r for r in results if r["text"] not in exclude]
    return results[:k]
