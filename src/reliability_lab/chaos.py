from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(
    config: LabConfig, provider_overrides: dict[str, float] | None = None
) -> ReliabilityGateway:
    providers: list[FakeLLMProvider] = []
    for p in config.providers:
        fail_rate = (
            provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        )
        providers.append(
            FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens)
        )
    breakers: dict[str, CircuitBreaker] = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            fallback_mem = ResponseCache(
                config.cache.ttl_seconds, config.cache.similarity_threshold
            )
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
                fallback=fallback_mem,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def _accumulate(metrics: RunMetrics, result: object) -> None:
    metrics.total_requests += 1
    metrics.estimated_cost += result.estimated_cost  # type: ignore[attr-defined]
    if result.cache_hit:  # type: ignore[attr-defined]
        metrics.cache_hits += 1
        metrics.estimated_cost_saved += 0.001
    route: str = result.route  # type: ignore[attr-defined]
    if route.startswith("fallback:"):
        metrics.fallback_successes += 1
        metrics.successful_requests += 1
    elif route == "static_fallback":
        metrics.static_fallbacks += 1
        metrics.failed_requests += 1
    else:
        metrics.successful_requests += 1
    latency = result.latency_ms  # type: ignore[attr-defined]
    if latency:
        metrics.latencies_ms.append(latency)


def run_scenario(
    config: LabConfig, queries: list[str], scenario: ScenarioConfig
) -> tuple[RunMetrics, ReliabilityGateway]:
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    request_count = config.load_test.requests
    concurrency = max(1, config.load_test.concurrency)

    prompts = [random.choice(queries) for _ in range(request_count)]
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(gateway.complete, prompt) for prompt in prompts]
        for f in as_completed(futures):
            _accumulate(metrics, f.result())

    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for t in breaker.transition_log
        if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics, gateway


def _evaluate(scenario_name: str, m: RunMetrics, gateway: ReliabilityGateway) -> str:
    if scenario_name == "primary_timeout_100":
        return (
            "pass"
            if m.fallback_success_rate >= 0.9 and m.circuit_open_count >= 1
            else "fail"
        )
    if scenario_name == "primary_flaky_50":
        return (
            "pass"
            if m.circuit_open_count >= 1 and m.successful_requests > 0
            else "fail"
        )
    if scenario_name == "all_healthy":
        return "pass" if m.circuit_open_count == 0 and m.availability >= 0.85 else "fail"
    if scenario_name == "cache_stale_candidate":
        log = getattr(gateway.cache, "false_hit_log", []) if gateway.cache is not None else []
        return "pass" if len(log) >= 1 else "fail"
    return "pass" if m.successful_requests > 0 else "fail"


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics, gateway = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": _evaluate("default", metrics, gateway)}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result, gw = run_scenario(config, queries, scenario)
        combined.scenarios[scenario.name] = _evaluate(scenario.name, result, gw)

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (
                    combined.recovery_time_ms + result.recovery_time_ms
                ) / 2

    return combined
