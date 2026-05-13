from reliability_lab.cache import ResponseCache


def test_exact_match_scores_one() -> None:
    assert ResponseCache.similarity("hello world", "hello world") == 1.0


def test_completely_different_low_score() -> None:
    score = ResponseCache.similarity("abc", "zzzzzzzz")
    assert score < 0.3


def test_year_diff_below_92_threshold() -> None:
    score = ResponseCache.similarity(
        "Summarize refund policy for 2024 deadline",
        "Summarize refund policy for 2026 deadline",
    )
    assert score < 0.92


def test_privacy_query_not_cached() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    cache.set("balance for user 123", "Balance: $500")
    cached, _ = cache.get("balance for user 123")
    assert cached is None


def test_false_hit_year_diff_blocks_lookup() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
    cached, _ = cache.get("Summarize refund policy for 2026 deadline")
    assert cached is None
    assert len(cache.false_hit_log) >= 1


def test_similar_safe_queries_match() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    cache.set("Explain circuit breaker states in one paragraph.", "states explained")
    cached, score = cache.get("Explain circuit breaker states in one paragraph.")
    assert cached == "states explained"
    assert score == 1.0
