from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ) -> None:
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str) -> GatewayResponse:
        t0 = time.perf_counter()
        if self.cache is not None:
            cached, score = self.cache.get(prompt)
            if cached is not None:
                return GatewayResponse(
                    text=cached,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    estimated_cost=0.0,
                )

        last_error: str | None = None
        for idx, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
            except (ProviderError, CircuitOpenError) as exc:
                last_error = f"{provider.name}:{type(exc).__name__}:{exc}"
                continue
            if self.cache is not None:
                self.cache.set(prompt, response.text, {"provider": provider.name})
            route = f"{'primary' if idx == 0 else 'fallback'}:{provider.name}"
            return GatewayResponse(
                text=response.text,
                route=route,
                provider=provider.name,
                cache_hit=False,
                latency_ms=(time.perf_counter() - t0) * 1000,
                estimated_cost=response.estimated_cost,
            )

        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=(time.perf_counter() - t0) * 1000,
            estimated_cost=0.0,
            error=last_error,
        )
