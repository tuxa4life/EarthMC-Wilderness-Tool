"""Shared HTTP helper.

`fetch` wraps a requests call with exponential-backoff retries on transient
failures — both network errors (timeout / connection reset) and retryable HTTP
statuses (429 + 5xx). It is the single choke point every EarthMC request goes
through, so retrying transient 5xx here means every endpoint benefits without
each caller reimplementing it.
"""
import time
import requests

# HTTP statuses worth retrying: rate-limit + the transient server-side 5xx that
# EarthMC regularly returns on a call that succeeds on a second attempt.
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _retry_delay(resp: requests.Response, attempt: int) -> float:
    """Backoff before the next attempt: 1 s, 2 s, 4 s … honoring Retry-After on 429."""
    backoff = float(2 ** attempt)
    if resp.status_code == 429:
        try:
            return max(backoff, float(resp.headers.get("Retry-After", backoff)))
        except (TypeError, ValueError):
            return backoff
    return backoff


def fetch(method, url, retries=3, **kwargs):
    """Call `method(url, **kwargs)`, retrying transient failures with backoff.

    `method` is a bound requests function (e.g. `session.get` / `session.post`).
    Retries on network errors (timeout / connection) **and** on retryable HTTP
    statuses (429 + 5xx); 429 honors the `Retry-After` header. Backoff is
    1 s, 2 s, 4 s … between attempts. Network errors are re-raised after the last
    retry; the final attempt's response is returned as-is (even if it's a 5xx),
    so the caller still calls `raise_for_status()` to surface a persistent error.
    """
    for attempt in range(retries + 1):
        try:
            resp = method(url, **kwargs)
        except requests.exceptions.RequestException:
            if attempt == retries:
                raise
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in _RETRY_STATUSES and attempt < retries:
            time.sleep(_retry_delay(resp, attempt))
            continue
        return resp
