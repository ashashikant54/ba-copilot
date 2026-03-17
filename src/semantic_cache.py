# semantic_cache.py
# CoAnalytica — Semantic Caching Layer
#
# ═══════════════════════════════════════════════════════════════
# WHAT THIS FILE DOES
# ═══════════════════════════════════════════════════════════════
#
# Intercepts BABOK check calls before they reach GPT-4o-mini.
# If a semantically similar requirement set was evaluated before,
# returns the cached result immediately — zero GPT cost, ~50ms.
#
# HOW SEMANTIC SIMILARITY WORKS:
#   1. Requirements text → text-embedding-3-small → 1536-dim vector
#   2. Vector stored in Redis with the BABOK result
#   3. On new request: embed → cosine similarity vs all cached vectors
#   4. If similarity >= threshold (0.93): return cached result
#   5. If below threshold: call GPT, store new result
#
# REDIS KEY STRUCTURE:
#   cache:babok:{hash}:result    → JSON BABOK result
#   cache:babok:{hash}:embedding → serialised embedding vector
#   cache:babok:{hash}:meta      → metadata (session_id, hit_count, created_at)
#   cache:stats                  → running hit/miss/cost_saved counters
#
# COST SAVINGS TRACKING:
#   Every cache hit records estimated cost saved (tokens * price).
#   Admin dashboard reads cache:stats to show running totals.
 
import os
import sys
import json
import time
import hashlib
import struct
import logging
from typing import Optional
from datetime import datetime
 
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
 
from dotenv import load_dotenv
from openai import OpenAI
 
load_dotenv()
logger = logging.getLogger(__name__)
 
# ── Config ─────────────────────────────────────────────────────
SIMILARITY_THRESHOLD = float(
    os.getenv("SEMANTIC_CACHE_SIMILARITY_THRESHOLD", "0.93")
)
TTL_SECONDS = int(os.getenv("SEMANTIC_CACHE_TTL_HOURS", "168")) * 3600
EMBEDDING_MODEL = "text-embedding-3-small"
CACHE_PREFIX = "cache:babok:"
STATS_KEY = "cache:stats"
 
# GPT-4o-mini cost per 1K tokens (for savings calculation)
COST_PER_1K_INPUT  = 0.000150
COST_PER_1K_OUTPUT = 0.000600
AVG_BABOK_INPUT_TOKENS  = 900
AVG_BABOK_OUTPUT_TOKENS = 350
AVG_BABOK_COST = (
    AVG_BABOK_INPUT_TOKENS  / 1000 * COST_PER_1K_INPUT +
    AVG_BABOK_OUTPUT_TOKENS / 1000 * COST_PER_1K_OUTPUT
)
 
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
_redis_client = None
 
 
# ══════════════════════════════════════════════════════════════
# REDIS CONNECTION
# ══════════════════════════════════════════════════════════════
 
def get_redis():
    """
    Lazy Redis connection. Returns None gracefully if Redis is
    unavailable — the agent falls back to direct GPT calls.
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client
 
    conn_str = os.getenv("REDIS_CONNECTION_STRING", "")
    if not conn_str:
        logger.warning("REDIS_CONNECTION_STRING not set — caching disabled")
        return None
 
    try:
        import redis
        _redis_client = redis.from_url(
            conn_str,
            decode_responses=False,  # raw bytes for embedding storage
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=False,
        )
        _redis_client.ping()
        logger.info("✅ Redis connection established")
        print("✅ Semantic cache: Redis connected")
        return _redis_client
    except Exception as e:
        logger.warning(f"Redis connection failed: {e} — caching disabled")
        print(f"⚠️  Semantic cache: Redis unavailable ({e}) — running without cache")
        _redis_client = None
        return None
 
 
# ══════════════════════════════════════════════════════════════
# EMBEDDING
# ══════════════════════════════════════════════════════════════
 
def embed_requirements(requirements: list) -> Optional[list]:
    """
    Embed a list of requirements into a single vector.
    Concatenates all effective_text fields, embeds as one string.
    Returns 1536-dim float list or None on failure.
    """
    texts = []
    for r in requirements:
        effective = r.get("effective_text") or r.get("edited_text") or r.get("text", "")
        req_type  = r.get("type", "")
        texts.append(f"[{r.get('id','')}] ({req_type}) {effective}")
 
    combined = "\n".join(texts)[:8000]  # cap to avoid token limit
 
    try:
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=combined,
        )
        return response.data[0].embedding
    except Exception as e:
        logger.warning(f"Embedding failed: {e}")
        return None
 
 
def _vec_to_bytes(vec: list) -> bytes:
    """Serialise float list to compact bytes for Redis storage."""
    return struct.pack(f"{len(vec)}f", *vec)
 
 
def _bytes_to_vec(b: bytes) -> list:
    """Deserialise bytes back to float list."""
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b))
 
 
def _cosine_similarity(a: list, b: list) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)
 
 
def _req_hash(requirements: list) -> str:
    """
    Deterministic hash of requirement IDs + effective text.
    Used as part of the Redis key for exact-match fast lookup.
    """
    parts = []
    for r in sorted(requirements, key=lambda x: x.get("id", "")):
        effective = r.get("effective_text") or r.get("edited_text") or r.get("text", "")
        parts.append(f"{r.get('id','')}:{effective}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
 
 
# ══════════════════════════════════════════════════════════════
# CACHE LOOKUP
# ══════════════════════════════════════════════════════════════
 
def cache_lookup(requirements: list, session_id: str = "") -> Optional[dict]:
    """
    Look up cached BABOK result for a set of requirements.
 
    Strategy:
      1. Exact hash match — instant lookup, no embedding needed
      2. Semantic similarity scan — embeds and compares against
         all stored embeddings, returns best match above threshold
 
    Returns cached result dict or None if no match.
    """
    r = get_redis()
    if r is None:
        return None
 
    req_hash = _req_hash(requirements)
 
    # ── Step 1: Exact hash match ───────────────────────────────
    exact_key = f"{CACHE_PREFIX}{req_hash}:result"
    try:
        cached = r.get(exact_key)
        if cached:
            result = json.loads(cached.decode("utf-8"))
            _record_hit(r, req_hash, session_id, similarity=1.0, exact=True)
            print(f"   🎯 Cache HIT (exact) — key: {req_hash}")
            return result
    except Exception as e:
        logger.warning(f"Cache exact lookup error: {e}")
 
    # ── Step 2: Semantic similarity search ────────────────────
    query_embedding = embed_requirements(requirements)
    if query_embedding is None:
        return None
 
    try:
        # Scan all stored cache keys
        all_keys = list(r.scan_iter(f"{CACHE_PREFIX}*:embedding"))
        if not all_keys:
            return None
 
        best_similarity = 0.0
        best_hash = None
 
        for emb_key in all_keys:
            try:
                emb_bytes = r.get(emb_key)
                if not emb_bytes:
                    continue
                stored_vec = _bytes_to_vec(emb_bytes)
                sim = _cosine_similarity(query_embedding, stored_vec)
                if sim > best_similarity:
                    best_similarity = sim
                    # Extract hash from key: cache:babok:{hash}:embedding
                    key_str = emb_key.decode("utf-8") if isinstance(emb_key, bytes) else emb_key
                    best_hash = key_str.split(":")[2]
            except Exception:
                continue
 
        if best_similarity >= SIMILARITY_THRESHOLD and best_hash:
            result_key = f"{CACHE_PREFIX}{best_hash}:result"
            cached = r.get(result_key)
            if cached:
                result = json.loads(cached.decode("utf-8"))
                _record_hit(r, best_hash, session_id,
                           similarity=best_similarity, exact=False)
                print(f"   🎯 Cache HIT (semantic sim={best_similarity:.3f}) "
                      f"— key: {best_hash}")
                return result
 
        print(f"   💨 Cache MISS (best_sim={best_similarity:.3f} < {SIMILARITY_THRESHOLD})")
        _record_miss(r)
        return None
 
    except Exception as e:
        logger.warning(f"Cache semantic lookup error: {e}")
        return None
 
 
# ══════════════════════════════════════════════════════════════
# CACHE STORE
# ══════════════════════════════════════════════════════════════
 
def cache_store(
    requirements: list,
    babok_result: dict,
    session_id: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
) -> bool:
    """
    Store a BABOK result in Redis with its embedding.
    Called after every cache miss + successful GPT call.
 
    Stores three keys per result:
      {prefix}{hash}:result    → JSON result
      {prefix}{hash}:embedding → serialised vector
      {prefix}{hash}:meta      → metadata for debugging
    """
    r = get_redis()
    if r is None:
        return False
 
    req_hash       = _req_hash(requirements)
    query_embedding = embed_requirements(requirements)
    if query_embedding is None:
        return False
 
    try:
        pipe = r.pipeline()
 
        # Result
        pipe.set(
            f"{CACHE_PREFIX}{req_hash}:result",
            json.dumps(babok_result),
            ex=TTL_SECONDS
        )
 
        # Embedding
        pipe.set(
            f"{CACHE_PREFIX}{req_hash}:embedding",
            _vec_to_bytes(query_embedding),
            ex=TTL_SECONDS
        )
 
        # Metadata
        meta = {
            "session_id":  session_id,
            "created_at":  datetime.now().isoformat(),
            "req_count":   len(requirements),
            "score":       babok_result.get("overall_quality_score", 0),
            "tokens_in":   tokens_in,
            "tokens_out":  tokens_out,
            "cost_usd":    round(cost_usd, 6),
            "hit_count":   0,
        }
        pipe.set(
            f"{CACHE_PREFIX}{req_hash}:meta",
            json.dumps(meta),
            ex=TTL_SECONDS
        )
 
        pipe.execute()
        print(f"   💾 Cached result — key: {req_hash}, "
              f"score: {babok_result.get('overall_quality_score',0)}, "
              f"TTL: {TTL_SECONDS//3600}h")
        return True
 
    except Exception as e:
        logger.warning(f"Cache store error: {e}")
        return False
 
 
# ══════════════════════════════════════════════════════════════
# STATS TRACKING
# ══════════════════════════════════════════════════════════════
 
def _record_hit(r, cache_hash: str, session_id: str,
                similarity: float, exact: bool) -> None:
    """Record a cache hit in the stats counter."""
    try:
        pipe = r.pipeline()
 
        # Global stats
        pipe.hincrbyfloat(STATS_KEY, "total_hits", 1)
        pipe.hincrbyfloat(STATS_KEY, "total_cost_saved_usd", AVG_BABOK_COST)
        pipe.hset(STATS_KEY, "last_hit_at", datetime.now().isoformat())
 
        # Increment hit count on the cached entry's meta
        meta_key = f"{CACHE_PREFIX}{cache_hash}:meta"
        meta_bytes = r.get(meta_key)
        if meta_bytes:
            meta = json.loads(meta_bytes.decode("utf-8"))
            meta["hit_count"] = meta.get("hit_count", 0) + 1
            meta["last_hit_at"] = datetime.now().isoformat()
            meta["last_similarity"] = round(similarity, 4)
            pipe.set(meta_key, json.dumps(meta),
                     ex=TTL_SECONDS)
 
        pipe.execute()
    except Exception:
        pass
 
 
def _record_miss(r) -> None:
    """Record a cache miss in the stats counter."""
    try:
        r.hincrbyfloat(STATS_KEY, "total_misses", 1)
    except Exception:
        pass
 
 
def get_cache_stats() -> dict:
    """
    Return cache statistics for the Admin dashboard.
    Called by GET /cache/stats endpoint.
    """
    r = get_redis()
    if r is None:
        return {
            "enabled":          False,
            "status":           "Redis unavailable",
            "total_hits":       0,
            "total_misses":     0,
            "hit_rate_pct":     0.0,
            "cost_saved_usd":   0.0,
            "cached_entries":   0,
            "threshold":        SIMILARITY_THRESHOLD,
            "ttl_hours":        TTL_SECONDS // 3600,
        }
 
    try:
        raw = r.hgetall(STATS_KEY)
        stats = {k.decode(): v.decode() for k, v in raw.items()}
 
        hits   = float(stats.get("total_hits", 0))
        misses = float(stats.get("total_misses", 0))
        total  = hits + misses
        hit_rate = round((hits / total * 100) if total > 0 else 0.0, 1)
 
        # Count cached entries (one per result key)
        cached_entries = sum(
            1 for _ in r.scan_iter(f"{CACHE_PREFIX}*:result")
        )
 
        return {
            "enabled":         True,
            "status":          "connected",
            "total_hits":      int(hits),
            "total_misses":    int(misses),
            "total_calls":     int(total),
            "hit_rate_pct":    hit_rate,
            "cost_saved_usd":  round(float(stats.get("total_cost_saved_usd", 0)), 6),
            "cached_entries":  cached_entries,
            "threshold":       SIMILARITY_THRESHOLD,
            "ttl_hours":       TTL_SECONDS // 3600,
            "avg_cost_per_call": round(AVG_BABOK_COST, 6),
            "last_hit_at":     stats.get("last_hit_at", "—"),
        }
    except Exception as e:
        return {
            "enabled": True,
            "status":  f"error: {e}",
            "total_hits": 0,
            "total_misses": 0,
            "hit_rate_pct": 0.0,
            "cost_saved_usd": 0.0,
            "cached_entries": 0,
            "threshold": SIMILARITY_THRESHOLD,
            "ttl_hours": TTL_SECONDS // 3600,
        }
 
 
def clear_cache() -> dict:
    """
    Clear all cached BABOK results. Called by DELETE /cache endpoint.
    Useful when prompts change significantly and cached results are stale.
    """
    r = get_redis()
    if r is None:
        return {"success": False, "message": "Redis unavailable"}
 
    try:
        keys = list(r.scan_iter(f"{CACHE_PREFIX}*"))
        keys.append(STATS_KEY.encode())
        if keys:
            r.delete(*keys)
        return {
            "success": True,
            "deleted": len(keys),
            "message": f"Cleared {len(keys)} cache keys"
        }
    except Exception as e:
        return {"success": False, "message": str(e)}