import random

from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


def test_open_breaker_skips_provider() -> None:
    random.seed(0)
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    backup = FakeLLMProvider("backup", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breakers = {
        "primary": CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=100.0),
        "backup": CircuitBreaker("backup", failure_threshold=2, reset_timeout_seconds=100.0),
    }
    gateway = ReliabilityGateway([primary, backup], breakers, cache=None)

    # Drive primary to OPEN
    for _ in range(5):
        gateway.complete("warm-up")

    before = primary.call_count
    for _ in range(20):
        gateway.complete("post-open")
    after = primary.call_count

    assert after == before, f"primary called {after - before} times while OPEN — retry storm!"
