# Day 10 Reliability Lab — Design Spec

**Date:** 2026-05-13
**Scope:** Core 85 points + 4 stretch goals (concurrency, Redis graceful degrade, xfail year-guardrail pass, SLO table). Target: ≥ 100 points with bonus.
**Authoritative rubric:** `README.md` (100-point breakdown). Fallback to `docs/RUBRIC.md` if grader uses that variant.

---

## 1. Architecture

User prompt enters `ReliabilityGateway.complete()`. Flow:

1. **Cache check** — if cache enabled, call `cache.get(prompt)`. Privacy queries skip cache; exact-hash match returns `(value, 1.0)`; similarity scan returns best ≥ threshold after false-hit guard.
2. **Circuit-protected providers** — iterate `[primary, backup]`. Each call goes through `CircuitBreaker.call()`. OPEN breaker fails fast with `CircuitOpenError`; gateway `continue`s to next provider. Successful provider response is cached then returned.
3. **Static fallback** — if all providers fail/OPEN, return `"The service is temporarily degraded..."` with `route="static_fallback"`.

```
User prompt
    │
    ▼
[ReliabilityGateway]
    │
    ├── cache.get() ────────► HIT → return (cache_hit, score)
    │     │
    │     │ MISS
    │     ▼
    ├── for provider in [primary, backup]:
    │     breaker.call(provider.complete)
    │       OPEN  → CircuitOpenError → continue
    │       FAIL  → record_failure → continue
    │       OK    → cache.set + return (route="primary:X" or "fallback:X")
    │
    └── static_fallback (all failed)

Cache backend (config-switched):
  ResponseCache (in-memory)
  SharedRedisCache (Redis Hash + EXPIRE; ConnectionError → fallback ResponseCache)
```

**Modules edited:** `circuit_breaker.py`, `gateway.py`, `cache.py`, `chaos.py`.
**Modules new:** `tests/test_circuit_breaker.py`, `tests/test_similarity.py`, `configs/no_cache.yaml`, `reports/final_report.md`, `reports/metrics.json`, `reports/metrics_nocache.json`.
**Untouched:** `config.py`, `metrics.py`, `Makefile`, `pyproject.toml`, `docker-compose.yml`. `providers.py` only gains a `call_count: int = 0` field for the no-retry-storm test.

---

## 2. Components

### 2.1 CircuitBreaker (`src/reliability_lab/circuit_breaker.py`)

State machine (3 states):

| State | allow_request | record_success | record_failure |
|---|---|---|---|
| CLOSED | True | failure_count=0, success_count++ | failure_count++, success_count=0; if ≥ failure_threshold → OPEN, opened_at=monotonic() |
| OPEN | False if elapsed < reset_timeout; else transition HALF_OPEN, return True | n/a | n/a |
| HALF_OPEN | True (single probe) | failure_count=0, success_count++; if ≥ success_threshold → CLOSED, success_count=0 | failure_count++, success_count=0, → OPEN, opened_at=monotonic() |

Transition reasons logged to `transition_log`: `"failure_threshold"`, `"probe_success"`, `"probe_failure"`, `"reset_timeout_elapsed"`. Each entry: `{"from", "to", "reason", "ts"}` (ts = `time.time()` for wall-clock recovery calc).

### 2.2 ResponseCache (`src/reliability_lab/cache.py`)

**Hybrid similarity:**

```python
@staticmethod
def similarity(a: str, b: str) -> float:
    a_norm, b_norm = a.lower().strip(), b.lower().strip()
    if a_norm == b_norm:
        return 1.0
    def trigrams(s: str) -> set[str]:
        return {s[i:i+3] for i in range(len(s) - 2)}
    ta, tb = trigrams(a_norm), trigrams(b_norm)
    char_score = len(ta & tb) / len(ta | tb) if (ta and tb) else 0.0
    la, lb = set(a_norm.split()), set(b_norm.split())
    tok_score = len(la & lb) / len(la | lb) if (la and lb) else 0.0
    return 0.5 * char_score + 0.5 * tok_score
```

**get() guardrail flow:**
1. `_is_uncacheable(query)` → return `(None, 0.0)`.
2. TTL prune `self._entries`.
3. Iterate entries, track best score.
4. If best ≥ threshold and `_looks_like_false_hit(query, entry.key)` → append to `self.false_hit_log`, return `(None, best_score)`.
5. Return `(best_value, best_score)`.

**set() guardrail:** `if _is_uncacheable(query): return` at top.

**New field:** `false_hit_log: list[dict[str, object]]` initialised in `__init__`.

### 2.3 SharedRedisCache (`src/reliability_lab/cache.py`)

**Top-of-file import added:** `import redis` (module-level, in addition to the existing lazy import inside `__init__`). This makes `redis.ConnectionError` / `redis.TimeoutError` referenceable from `get()` / `set()`.

**`set()` implementation:**

```python
def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
    if _is_uncacheable(query):
        return
    try:
        key = f"{self.prefix}{self._query_hash(query)}"
        self._redis.hset(key, mapping={"query": query, "response": value})
        self._redis.expire(key, self.ttl_seconds)
    except (redis.ConnectionError, redis.TimeoutError):
        self.degraded_writes += 1
        if self.fallback is not None:
            self.fallback.set(query, value, metadata)
```

**`get()` implementation:**

1. Privacy check → `(None, 0.0)`.
2. Try block wraps Redis ops.
3. Exact: `self._redis.hget(exact_key, "response")` → if found, return `(val, 1.0)`.
4. `self._redis.scan_iter(f"{self.prefix}*")` → for each key, `hget("query")`, compute `ResponseCache.similarity(query, cached_q)`, track best.
5. If best ≥ threshold and `_looks_like_false_hit()` → log + return `(None, best_score)`.
6. Else return `(best_response, best_score)`.
7. `except (redis.ConnectionError, redis.TimeoutError)`: `degraded_reads++`, delegate to fallback if present, else `(None, 0.0)`.

**`__init__` changes:** Accept optional `fallback: ResponseCache | None = None`. Add `degraded_reads: int = 0` and `degraded_writes: int = 0` counters.

### 2.4 ReliabilityGateway (`src/reliability_lab/gateway.py`)

**`complete()` rewrite:**

```python
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
            response = breaker.call(provider.complete, prompt)
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
        except (ProviderError, CircuitOpenError) as exc:
            last_error = f"{provider.name}:{type(exc).__name__}:{exc}"
            continue
    return GatewayResponse(
        text="The service is temporarily degraded. Please try again soon.",
        route="static_fallback",
        provider=None,
        cache_hit=False,
        latency_ms=(time.perf_counter() - t0) * 1000,
        estimated_cost=0.0,
        error=last_error,
    )
```

### 2.5 Chaos (`src/reliability_lab/chaos.py`)

**Pass/fail criteria per scenario.** Change `run_scenario` signature to return both metrics and the gateway:

```python
def run_scenario(config, queries, scenario) -> tuple[RunMetrics, ReliabilityGateway]:
    ...
    return metrics, gateway
```

Evaluation helper inspects gateway state where needed:

```python
def _evaluate(scenario_name: str, m: RunMetrics, gateway: ReliabilityGateway) -> str:
    if scenario_name == "primary_timeout_100":
        return "pass" if m.fallback_success_rate >= 0.9 and m.circuit_open_count >= 1 else "fail"
    if scenario_name == "primary_flaky_50":
        return "pass" if m.circuit_open_count >= 1 and m.successful_requests > 0 else "fail"
    if scenario_name == "all_healthy":
        return "pass" if m.circuit_open_count == 0 and m.availability >= 0.85 else "fail"
    if scenario_name == "cache_stale_candidate":
        log = getattr(gateway.cache, "false_hit_log", []) if gateway.cache else []
        return "pass" if len(log) >= 1 else "fail"
    return "pass" if m.successful_requests > 0 else "fail"
```

`run_simulation` calls `_evaluate(scenario.name, result, gw)` for each scenario.

**Fourth scenario** `cache_stale_candidate` added to `configs/default.yaml`. Pass criterion: `len(gateway.cache.false_hit_log) >= 1` (guardrail triggered ≥ 1 time).

**Concurrency (stretch):**

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def run_scenario(config, queries, scenario):
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    request_count = config.load_test.requests
    concurrency = max(1, config.load_test.concurrency)
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(gateway.complete, random.choice(queries))
                   for _ in range(request_count)]
        for f in as_completed(futures):
            result = f.result()
            # accumulate into metrics on main thread (no lock needed)
            ...
```

Metrics accumulation stays on main thread; breaker counters tolerate Python GIL races at off-by-one level (documented in Next Steps).

### 2.6 Config tuning targets

Edit `configs/default.yaml`:

| Setting | Value | Justification |
|---|---:|---|
| circuit_breaker.failure_threshold | 3 | Detects failure in 3 attempts; tolerant of jitter at fail_rate=0.25 (~1.5% chance of false-open in healthy state) |
| circuit_breaker.reset_timeout_seconds | 2 | Matches FakeLLMProvider base_latency × ~10 attempts |
| circuit_breaker.success_threshold | 2 | 2 successful probes before closing — reduces flapping |
| cache.ttl_seconds | 300 | FAQ-type queries stable for 5 minutes |
| cache.similarity_threshold | 0.85 | Empirically tested with hybrid scorer: 0.70 yields year-diff false hits; 0.85 yields zero |
| load_test.requests | 200 | Sufficient samples for stable P95/P99 |
| load_test.concurrency | 10 | Light prod-like contention |

Also add `load_test.concurrency: 10` if not already present.

`configs/no_cache.yaml`: copy of `default.yaml` with `cache.enabled: false`.

---

## 3. Data Flow

### 3.1 Request lifecycle (cache miss + primary success)

```
gateway.complete(prompt)
  ├─ t0 = perf_counter()
  ├─ cache.get(prompt)
  │    └─ scan entries, similarity < threshold → (None, 0.0)
  ├─ breaker["primary"].call(primary.complete, prompt)
  │    ├─ allow_request() → True (CLOSED)
  │    ├─ primary.complete(prompt) → ProviderResponse(text, latency, cost)
  │    └─ record_success()
  ├─ cache.set(prompt, response.text, {"provider": "primary"})
  └─ return GatewayResponse(route="primary:primary", latency_ms=Δ*1000, ...)
```

### 3.2 Circuit-open fallback

```
gateway.complete(prompt) [after primary has hit failure_threshold]
  ├─ cache miss
  ├─ breaker["primary"].call(...) → CircuitOpenError
  ├─ continue
  ├─ breaker["backup"].call(backup.complete, prompt) → OK
  ├─ cache.set(...)
  └─ return GatewayResponse(route="fallback:backup", ...)
```

### 3.3 Recovery cycle

```
CLOSED → (3 failures) → OPEN [opened_at=T]
       OPEN → (request at T+2s) → HALF_OPEN [via allow_request]
       HALF_OPEN → (2 successes) → CLOSED [recovery complete]

calculate_recovery_time_ms() reads transition_log:
  open_ts at first to="open"
  closed_ts at next to="closed"
  recovery_ms = (closed_ts - open_ts) * 1000
```

### 3.4 Redis shared state evidence

Two `SharedRedisCache` instances with same prefix on one Redis instance see same keys. Demonstrated in `test_shared_state_across_instances` and in report via `docker compose exec redis redis-cli KEYS "rl:cache:*"`.

---

## 4. Error Handling & Graceful Degradation

### 4.1 Failure → handler matrix

| Failure | Handler | Behavior | Metric impact |
|---|---|---|---|
| `ProviderError` from provider.complete | `breaker.call()` | `record_failure()`, raise → gateway `continue` | failure_count++; may transition OPEN |
| Circuit OPEN, request arrives | `gateway.complete()` for-loop | `CircuitOpenError` caught, `continue` | route shifts to next provider |
| All providers fail/OPEN | end of for-loop | static_fallback response | `failed_requests++`, `static_fallbacks++` |
| `_is_uncacheable(query)` | cache.get/set head | skip cache entirely | no cache hit, no log |
| `_looks_like_false_hit` | cache.get tail | return None | `false_hit_log` append |
| Redis `ConnectionError`/`TimeoutError` (set) | try/except in set | optional fallback.set; `degraded_writes++` | best-effort write |
| Redis `ConnectionError`/`TimeoutError` (get) | try/except in get | optional fallback.get; `degraded_reads++` | best-effort read |
| TTL expired entry | `ResponseCache.get` prune step | entry removed pre-scan | no log |

### 4.2 SharedRedisCache graceful degrade wire-up

In `chaos.build_gateway()`:

```python
if config.cache.backend == "redis":
    fallback_mem = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    cache = SharedRedisCache(
        config.cache.redis_url,
        config.cache.ttl_seconds,
        config.cache.similarity_threshold,
        fallback=fallback_mem,
    )
else:
    cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
```

### 4.3 No-retry-storm invariant

When breaker is OPEN, `allow_request()` returns False → `breaker.call()` raises `CircuitOpenError` immediately. Gateway catches and `continue`s. There is no inner retry of the same provider. Verified by `test_no_retry_storm` which counts provider call invocations before and after OPEN state.

### 4.4 Concurrency safety

`RunMetrics` mutation happens only on the main thread (after `as_completed`). Workers only call `gateway.complete()` which returns a `GatewayResponse`. No locks required on metrics.

Breaker counters are shared across threads and not lock-protected. Off-by-one in `failure_count` is acceptable given thresholds are ≥ 3. Documented in report's "Next Steps" as candidate for `threading.Lock` if scaling further.

### 4.5 Failure analysis (report mục 8)

**Chosen weakness:** Circuit state is in-process — multi-instance deployments have inconsistent breaker views.

- Scenario: Instance A observes 3 primary failures → OPEN; instance B sees 0 failures → still routes to primary. Inconsistent UX, slower aggregate recovery.
- Fix: Move `failure_count`, `state`, `opened_at` to Redis via `INCR` + `GET`/`SET` with `EXPIRE`. Pub/Sub channel `breaker:state:<name>` for transition broadcast.
- Tradeoff: +1 Redis round-trip (~1ms) per request. Acceptable for consistency.

---

## 5. Testing Strategy

### 5.1 Existing tests — expected outcome

| Test file | Before | After |
|---|---|---|
| test_config.py | pass | pass |
| test_metrics.py | pass | pass |
| test_gateway_contract.py | may fail (empty route) | pass |
| test_todo_requirements.py | xfail | xpass (xfail not strict in pyproject) |
| test_redis_cache.py (6 tests) | skip without Redis | all pass with `make docker-up` |

### 5.2 New tests

**`tests/test_circuit_breaker.py`:**

- `test_closed_to_open_at_threshold` — 3 failures → OPEN
- `test_open_fails_fast` — allow_request False when OPEN within timeout
- `test_half_open_failure_reopens` — failure in HALF_OPEN → OPEN immediately
- `test_half_open_success_closes` — success_threshold probes in HALF_OPEN → CLOSED
- `test_transition_log_has_entries` — transitions logged with from/to/reason/ts

**`tests/test_similarity.py`:**

- `test_exact_match_one` — identical strings score 1.0
- `test_year_diff_below_threshold` — 2024 vs 2026 same template scores < 0.92
- `test_privacy_query_uncacheable` — set/get with "balance for user 123" returns None

**`tests/test_no_retry_storm.py`** (optional, requires `FakeLLMProvider.call_count`):

- Drive primary to OPEN, then run 20 more requests, assert primary.call_count unchanged.

### 5.3 Manual verification commands

```bash
make docker-up
make test                                     # 0 failures
make typecheck                                # mypy strict pass
make lint                                     # ruff clean
make run-chaos                                # reports/metrics.json
python scripts/run_chaos.py --config configs/no_cache.yaml --out reports/metrics_nocache.json
make report                                   # reports/final_report.md skeleton (then manual fill)
docker compose exec redis redis-cli KEYS "rl:cache:*"
make docker-down && make run-chaos            # graceful degrade test (should not crash)
```

### 5.4 Reproducibility check

Grader sequence on fresh venv:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d
make test
make run-chaos
make report
```

Must produce `metrics.json` whose numbers match those quoted in `final_report.md`.

---

## 6. Report Structure (`reports/final_report.md`)

### 6.1 Section-by-section data source

| Section | Source |
|---|---|
| 1. Architecture summary | Section 1 of this spec |
| 2. Configuration table | `configs/default.yaml` + Section 2.6 justifications |
| 3. SLO definitions | Stretch goal; targets in 6.2 |
| 4. Metrics | Paste from `reports/metrics.json` |
| 5. Cache comparison | Two runs: `default.yaml` vs `no_cache.yaml` |
| 6. Redis shared cache | `test_shared_state_across_instances` output + `redis-cli KEYS` |
| 7. Chaos scenarios | `metrics.json` `scenarios` field + transition_log evidence |
| 8. Failure analysis | Section 4.5 |
| 9. Next steps | 3 bullets from stretch goals not implemented |

### 6.2 SLO table

| SLI | SLO target | Actual | Met? |
|---|---|---:|---|
| Availability | ≥ 99% | TBD from metrics | Yes/No |
| Latency P95 | < 2500 ms | TBD | Yes/No |
| Fallback success rate | ≥ 95% | TBD | Yes/No |
| Cache hit rate | ≥ 10% | TBD | Yes/No |
| Recovery time | < 5000 ms | TBD | Yes/No |

### 6.3 Chaos scenarios table (template)

| Scenario | Expected | Observed | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary OPEN within 3 reqs; fallback_success_rate ≥ 0.9 | TBD | TBD |
| primary_flaky_50 | Circuit oscillates; mix of primary and fallback responses | TBD | TBD |
| all_healthy | No circuit OPEN; availability ≥ 0.95 | TBD | TBD |
| cache_stale_candidate | False-hit guardrail triggers ≥ 1 time | TBD | TBD |

---

## 7. Implementation Order

| Step | Module | Verify |
|---|---|---|
| 1 | `circuit_breaker.py` — `record_success`, `record_failure` | new `test_circuit_breaker.py` |
| 2 | `gateway.py` — route reasons, `time.perf_counter` wrap | `test_gateway_contract.py` |
| 3 | `cache.py` — hybrid similarity, false-hit guardrails, `false_hit_log` | new `test_similarity.py`, `test_todo_requirements.py` xpass |
| 4 | `cache.py` — `SharedRedisCache.set/get` | `test_redis_cache.py` (Redis up) |
| 5 | `cache.py` — graceful degrade with `fallback` arg | manual: `make docker-down && make run-chaos` |
| 6 | `chaos.py` — pass/fail criteria, `cache_stale_candidate` scenario, gateway return | `make run-chaos` |
| 7 | `chaos.py` — ThreadPoolExecutor concurrency | wall-clock comparison |
| 8 | `configs/no_cache.yaml` | manual copy |
| 9 | Two runs (cached + no_cache) | both metrics files exist |
| 10 | `reports/final_report.md` | manual fill, 0 TODO |
| 11 | `make typecheck` + `make lint` | both clean |
| 12 | Fresh-venv reproducibility check | grader sequence passes |

---

## 8. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Docker not running on Windows | Document Redis native install in report fallback; prioritise core 70 points first |
| `make` missing on Windows | Use raw commands from Makefile (pytest, python scripts/...) — `pip install -e ".[dev]"` works regardless |
| xfail strict mode | Verified `pyproject.toml` `[tool.pytest.ini_options]` does not set `xfail_strict=true` |
| Concurrency causes flaky tests | Concurrency test asserts only wall-clock comparison, not absolute latency |
| mypy strict on `redis_lib` import | `self._redis: Any` annotation already in starter code |
| Breaker counter races under threading | Off-by-one tolerable at thresholds ≥ 3; documented in Next Steps |

---

## 9. Deliverables Checklist

| # | Artifact | Generated by | Verification |
|---|---|---|---|
| 1 | `src/reliability_lab/*.py` — all TODOs resolved | manual edit | `grep -rn "TODO(student)" src/` → empty |
| 2 | `reports/metrics.json` | `make run-chaos` | 13 fields present; scenarios pass |
| 3 | `reports/metrics_nocache.json` | `run_chaos.py --config configs/no_cache.yaml` | for cache comparison |
| 4 | `reports/final_report.md` | manual fill from template | 9 sections; 0 "TODO" markers |
| 5 | `reports/test_output.log` | `make test 2>&1 \| tee reports/test_output.log` | all pass |
| 6 | `docker-compose.yml` | already present | grader runs `docker compose up -d` |
| 7 | `configs/no_cache.yaml` | manual copy | `cache.enabled: false` |
| 8 | `tests/test_circuit_breaker.py` (new) | manual | 5 tests pass |
| 9 | `tests/test_similarity.py` (new) | manual | 3 tests pass |

---

## 10. Summary

- **Scope:** Core 85 + 4 stretch (concurrency, Redis graceful degrade, xfail year-guardrail pass, SLO table). Target ≥ 100 points.
- **Strategy:** Single bundled implementation, no incremental sub-projects.
- **Risk-managed:** Core modules first (CB → cache → gateway → chaos), Redis last as it depends on Docker.
- **Pass gate:** `make test` 0 failures, `metrics.json` 13 fields, report 9 sections 0 TODO, grader command sequence reproducible.
