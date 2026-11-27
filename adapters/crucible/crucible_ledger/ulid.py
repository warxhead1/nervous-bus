import os
import threading
import time

_ALPHA = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last = [0, 0]


def ulid():
    """26-char Crockford ULID, monotonic within the process."""
    with _lock:
        ms = int(time.time() * 1000)
        if ms <= _last[0]:
            ms = _last[0]
            rnd = _last[1] + 1
            if rnd >= 1 << 80:
                ms += 1
                rnd = int.from_bytes(os.urandom(10), "big")
        else:
            rnd = int.from_bytes(os.urandom(10), "big")
        _last[0], _last[1] = ms, rnd
    n = (ms << 80) | rnd
    out = []
    for _ in range(26):
        out.append(_ALPHA[n & 31])
        n >>= 5
    return "".join(reversed(out))
