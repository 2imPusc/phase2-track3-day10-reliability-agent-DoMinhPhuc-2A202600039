import time

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


def test_closed_to_open_at_threshold() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=3, reset_timeout_seconds=1.0)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_open_fails_fast() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=1, reset_timeout_seconds=10.0)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False


def test_open_to_half_open_after_timeout() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=1, reset_timeout_seconds=0.05)
    cb.record_failure()
    time.sleep(0.1)
    assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN


def test_half_open_failure_reopens_immediately() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=5, reset_timeout_seconds=0.05)
    # Force HALF_OPEN
    cb.state = CircuitState.HALF_OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_half_open_success_closes_after_threshold() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=1, reset_timeout_seconds=0.05, success_threshold=2)
    cb.state = CircuitState.HALF_OPEN
    cb.record_success()
    assert cb.state == CircuitState.HALF_OPEN  # need another probe
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_transition_log_records_open_and_close() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=1, reset_timeout_seconds=0.05, success_threshold=1)
    cb.record_failure()
    time.sleep(0.1)
    cb.allow_request()
    cb.record_success()
    tos = [entry["to"] for entry in cb.transition_log]
    assert "open" in tos
    assert "half_open" in tos
    assert "closed" in tos


def test_call_raises_circuit_open_error_when_open() -> None:
    cb = CircuitBreaker(name="t", failure_threshold=1, reset_timeout_seconds=10.0)
    cb.record_failure()
    try:
        cb.call(lambda: "should not run")
    except CircuitOpenError:
        return
    raise AssertionError("CircuitOpenError not raised")
