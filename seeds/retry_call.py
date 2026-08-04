"""Retrying a flaky call with exponential backoff."""

from __future__ import annotations

import random
import time
from typing import Any, Callable

from codeact import helper


@helper(
    job="orchestrate",
    domains=["time"],
    side_effects="inherits",
    examples=[
        {
            "code": "retry_call(lambda: 2 + 2)",
            "note": "fn takes no arguments; bind them with a lambda or "
            "functools.partial. A first-attempt success never sleeps",
        },
        {
            "setup": (
                "_calls = []\n"
                "def _flaky():\n"
                "    _calls.append(1)\n"
                "    if len(_calls) < 3:\n"
                "        raise ValueError('cold start ' + str(len(_calls)))\n"
                "    return 'ready after ' + str(len(_calls)) + ' calls'"
            ),
            "code": "retry_call(_flaky, attempts=5, delay=0.001)",
            "note": "_flaky raises on its first two calls then succeeds: the two "
            "failures are swallowed and the third call's value comes back",
        },
        {
            "setup": (
                "_tries = []\n"
                "def _always():\n"
                "    _tries.append(1)\n"
                "    raise RuntimeError('still broken, call ' + str(len(_tries)))"
            ),
            "code": "retry_call(_always, attempts=3, delay=0.001)",
            "note": "_always raises every call and counts them. Attempts exhausted: "
            "the last exception is re-raised unchanged, and 'call 3' shows all three "
            "attempts really happened",
            "raises": True,
        },
        {
            "setup": "_tries.clear()",
            "code": "retry_call(_always, attempts=5, delay=0.001, retry_on=(KeyError,))",
            "note": "RuntimeError is not in retry_on, so it propagates immediately — "
            "'call 1' proves no retry was attempted",
            "raises": True,
        },
        {
            "setup": (
                "def _sleep_schedule(**kwargs):\n"
                "    # Run _always (fails every call) to exhaustion with time.sleep\n"
                "    # swapped out, so the waits are recorded instead of taken.\n"
                "    waits = []\n"
                "    _tries.clear()\n"
                "    real_sleep, time.sleep = time.sleep, waits.append\n"
                "    try:\n"
                "        retry_call(_always, **kwargs)\n"
                "    except RuntimeError:\n"
                "        pass\n"
                "    finally:\n"
                "        time.sleep = real_sleep\n"
                "    return {'waits': waits, 'calls_made': len(_tries)}"
            ),
            "code": "_sleep_schedule(attempts=3, delay=0.1, backoff=2.0)",
            "note": "the wait schedule of a run that exhausts its attempts: 3 calls but "
            "only 2 sleeps, 0.1s then 0.2s. Nothing is slept after the final failure — "
            "the helper raises the moment call 3 fails rather than pausing first",
        },
        {
            "code": "retry_call(lambda: 1 / 0, attempts=0)",
            "note": "attempts below 1 is rejected rather than returning None from a "
            "callable that was never invoked; same for any negative value",
            "raises": True,
        },
    ],
)
def retry_call(
    fn: Callable[[], Any],
    *,
    attempts: int = 3,
    delay: float = 0.1,
    backoff: float = 2.0,
    jitter: float = 0.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> Any:
    """Call something flaky again after a growing pause, then give up honestly.

    Use when: the operation fails for reasons that pass on their own — a rate
        limit, a cold service, a locked file, a dropped connection — and calling
        it twice is harmless.
    Don't use when: the call is not idempotent (a retried POST can charge a card
        twice; retry only if the API takes an idempotency key), or the failure
        is deterministic — bad credentials, a 404, a parse error — where every
        attempt fails identically and retrying only adds latency. Narrow
        `retry_on` rather than retrying everything.

    Args:
        fn: A zero-argument callable. Bind arguments with a lambda or
            functools.partial. It is called up to `attempts` times, so it must
            be safe to run more than once. Whatever it touches — network,
            filesystem, processes, money — this helper touches, once per
            attempt; see the side effects note below.
        attempts: Total number of calls, not extra ones — attempts=3 means one
            try plus at most two retries. Must be at least 1; 0 or negative
            raises ValueError rather than calling nothing.
        delay: Seconds to sleep after the first failure. Later waits grow from
            this. Sleeping happens only between attempts, never after the last:
            attempts=3 sleeps twice, and a run that exhausts its attempts raises
            as soon as the final call fails, with no trailing pause. 0 retries
            immediately, unless `jitter` is set — that is added on top.
        backoff: Multiplier applied to the wait after each failure. 2.0 with
            delay=0.1 waits 0.1s, 0.2s, 0.4s... Use 1.0 for a fixed interval.
        jitter: Upper bound, in seconds, on a random extra wait added to each
            sleep (drawn uniformly from [0, jitter)). Set it above 0 when many
            workers retry the same dependency, so they stop hammering it in
            lockstep. 0 (the default, and the effect of any negative value)
            keeps sleep durations exact.
        retry_on: Exception classes that count as retryable. Anything else is
            never retried: it propagates straight out of whichever attempt
            raised it, even if earlier attempts had failed retryably. The
            default, (Exception,), retries essentially everything while still
            letting KeyboardInterrupt and SystemExit through.

    Returns:
        exactly what fn() returned on the attempt that succeeded — this helper
        adds no wrapper, so the caller sees the same value a direct call gives.

    Raises:
        BaseException: whatever fn raised on its LAST attempt, re-raised with
            its original traceback once attempts are exhausted — the last
            failure, not the first. Earlier failures are discarded entirely:
            they are neither chained nor logged, so if attempt 1 fails with a
            useful message and attempt 3 with a vague one, the vague one is what
            surfaces. The caller handles it exactly as if there had been no
            retrying. An exception not listed in `retry_on` is never retried and
            comes out of the attempt that raised it untouched.
        ValueError: attempts is less than 1. That would call `fn` zero times and
            return None, which is indistinguishable from a successful call that
            returned None, so it is refused instead — pass at least 1.

    Preconditions:
        Side effects are declared "inherits": this helper performs none of its
        own, and does whatever `fn` does, up to `attempts` times over. Judge what
        a call touches, what it costs, and whether it needs secrets from the
        callable being passed in, not from this card. Total worst-case runtime is
        roughly the sum of the backoff series plus the calls themselves, so size
        `attempts`, `delay` and `backoff` against whatever deadline the caller is
        working to.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, got {attempts}")

    wait = float(delay)
    attempt = 1
    while True:
        try:
            return fn()
        except retry_on:
            if attempt >= attempts:
                raise
            pause = wait + (random.uniform(0.0, jitter) if jitter > 0 else 0.0)
            if pause > 0:
                time.sleep(pause)
            wait *= backoff
            attempt += 1
