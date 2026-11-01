"""Idempotency-Key handling (queries M1-M3, table idempotency_keys).

Same key + same body  -> replay the stored response.
Same key + other body -> 409.
Required on AI executions and import starts; accepted on every creating POST.

TODO(M1/M10): implement claim / replay / store in the same transaction as the operation.
"""

import hashlib
import json
from typing import Any


def request_hash(method: str, path: str, body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{method.upper()} {path}\n{canonical}".encode()).hexdigest()
