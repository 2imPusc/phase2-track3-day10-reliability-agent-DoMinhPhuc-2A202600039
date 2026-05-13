from reliability_lab.cache import ResponseCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


def _is_valid_route(route: str) -> bool:
    if route == "static_fallback":
        return True
    if route.startswith(("primary:", "fallback:", "cache_hit:")):
        return True
    return False


def test_gateway_returns_response_with_route_reason() -> None:
    provider = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=1.0)
    gateway = ReliabilityGateway([provider], {"primary": breaker}, ResponseCache(60, 0.5))
    result = gateway.complete("hello world")
    assert result.text
    assert _is_valid_route(result.route), f"unexpected route: {result.route}"
    assert result.latency_ms >= 0.0
