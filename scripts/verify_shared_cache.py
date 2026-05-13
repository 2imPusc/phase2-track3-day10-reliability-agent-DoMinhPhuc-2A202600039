"""Demonstrate two SharedRedisCache instances reading shared state."""
from __future__ import annotations

from reliability_lab.cache import SharedRedisCache


def main() -> None:
    c1 = SharedRedisCache(
        redis_url="redis://localhost:6379/0",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:demo:",
    )
    c2 = SharedRedisCache(
        redis_url="redis://localhost:6379/0",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:demo:",
    )
    c1.flush()
    c1.set("circuit breaker explanation", "states: closed, open, half_open")
    response, score = c2.get("circuit breaker explanation")
    print("c1.set -> c2.get")
    print(f"  response: {response}")
    print(f"  score:    {score:.2f}")
    c1.flush()
    c1.close()
    c2.close()


if __name__ == "__main__":
    main()
