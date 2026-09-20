"""One bounded, inventory-only NVIDIA GPU measurement per collection batch."""
import os
import re
import selectors
import subprocess
import time


QUERY_TIMEOUT_S = 2.0
CLEANUP_REAP_S = 0.1
PROCESS_TIMEOUT_S = QUERY_TIMEOUT_S - CLEANUP_REAP_S
MAX_STDOUT_BYTES = 64 * 1024
MAX_DEVICES = 8
MIB = 1024 * 1024
MAX_MEMORY_MIB = (2 ** 63 - 1) // MIB
COMMAND = (
    "nvidia-smi",
    "--query-gpu=uuid,driver_version,memory.total,memory.used,memory.free,utilization.gpu",
    "--format=csv,noheader,nounits",
)
UUID = re.compile(r"^GPU-[0-9a-fA-F][0-9a-fA-F-]{7,126}$")
DRIVER = re.compile(r"^[0-9][0-9A-Za-z._-]{0,63}$")
UNAVAILABLE = frozenset({"unsupported", "timeout", "output_limit", "query_failed", "cleanup_failed", "not_collected"})


def unavailable_gpu(reason="not_collected", query_ms=0.0, attempted=False):
    """Return the fixed event shape for an unavailable query without guessing data."""
    if reason not in UNAVAILABLE:
        reason = "query_failed"
    return {"state": "unavailable", "reason": reason, "attempted": bool(attempted),
            "query_ms": max(0.0, float(query_ms)), "devices": []}


def _elapsed(started, clock):
    return max(0.0, (clock() - started) * 1000)


def _wait_until_exit(process, deadline, clock):
    """Wait only through a supplied deadline; return None rather than block forever."""
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        try:
            return process.wait(timeout=min(remaining, CLEANUP_REAP_S))
        except subprocess.TimeoutExpired:
            continue


def _terminate_and_reap(process, clock, deadline=None):
    """Stop only the direct process created by this query, then reap it."""
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        if process.stdout is not None:
            process.stdout.close()
    except OSError:
        pass
    deadline = clock() + CLEANUP_REAP_S if deadline is None else deadline
    return _wait_until_exit(process, deadline, clock) is not None


def _bounded_stdout(process, started, clock):
    """Read at most one byte beyond the cap so overflow is detected, not retained."""
    stream = process.stdout
    if stream is None:
        returncode = _wait_until_exit(process, started + PROCESS_TIMEOUT_S, clock)
        if returncode is not None:
            return "finished", b"", returncode
        return ("timeout" if _terminate_and_reap(process, clock, started + QUERY_TIMEOUT_S)
                else "cleanup_failed"), b"", None
    try:
        descriptor = stream.fileno()
        os.set_blocking(descriptor, False)
    except (AttributeError, OSError, ValueError):
        return ("failed" if _terminate_and_reap(process, clock) else "cleanup_failed"), b"", None

    output = bytearray()
    selector = selectors.DefaultSelector()
    try:
        try:
            selector.register(stream, selectors.EVENT_READ)
        except (OSError, ValueError):
            return ("failed" if _terminate_and_reap(process, clock) else "cleanup_failed"), b"", None
        while True:
            remaining = PROCESS_TIMEOUT_S - (clock() - started)
            if remaining <= 0:
                return ("timeout" if _terminate_and_reap(process, clock, started + QUERY_TIMEOUT_S)
                        else "cleanup_failed"), b"", None
            try:
                ready = selector.select(remaining)
            except (OSError, ValueError):
                return ("failed" if _terminate_and_reap(process, clock) else "cleanup_failed"), b"", None
            if not ready:
                continue
            eof = False
            for _, _ in ready:
                while True:
                    try:
                        amount = min(4096, MAX_STDOUT_BYTES + 1 - len(output))
                        chunk = os.read(descriptor, amount)
                    except BlockingIOError:
                        break
                    except (OSError, ValueError):
                        return ("failed" if _terminate_and_reap(process, clock) else "cleanup_failed"), b"", None
                    if not chunk:
                        eof = True
                        break
                    output.extend(chunk)
                    if len(output) > MAX_STDOUT_BYTES:
                        return ("overflow" if _terminate_and_reap(process, clock) else "cleanup_failed"), b"", None
                    if len(chunk) < amount:
                        break
            if eof:
                returncode = _wait_until_exit(process, started + PROCESS_TIMEOUT_S, clock)
                if returncode is not None:
                    return "finished", bytes(output), returncode
                return ("timeout" if _terminate_and_reap(process, clock, started + QUERY_TIMEOUT_S)
                        else "cleanup_failed"), b"", None
    finally:
        selector.close()


def run_query(popen=None, clock=None):
    """Run nvidia-smi without a shell and return a bounded internal result."""
    popen = subprocess.Popen if popen is None else popen
    clock = time.monotonic if clock is None else clock
    started = clock()
    try:
        process = popen(COMMAND, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, close_fds=True)
    except FileNotFoundError:
        return {"state": "unavailable", "reason": "unsupported", "attempted": True,
                "query_ms": _elapsed(started, clock), "stdout": b""}
    except OSError:
        return {"state": "unavailable", "reason": "query_failed", "attempted": True,
                "query_ms": _elapsed(started, clock), "stdout": b""}

    try:
        state, output, returncode = _bounded_stdout(process, started, clock)
    except Exception:
        state, output, returncode = ("failed" if _terminate_and_reap(process, clock, started + QUERY_TIMEOUT_S)
                                     else "cleanup_failed"), b"", None
    finally:
        stream = getattr(process, "stdout", None)
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    elapsed = _elapsed(started, clock)
    if state == "timeout":
        return {"state": "unavailable", "reason": "timeout", "attempted": True,
                "query_ms": elapsed, "stdout": b""}
    if state == "overflow":
        return {"state": "unavailable", "reason": "output_limit", "attempted": True,
                "query_ms": elapsed, "stdout": b""}
    if state == "cleanup_failed":
        return {"state": "unavailable", "reason": "cleanup_failed", "attempted": True,
                "query_ms": elapsed, "stdout": b""}
    if state != "finished" or returncode:
        return {"state": "unavailable", "reason": "query_failed", "attempted": True,
                "query_ms": elapsed, "stdout": b""}
    return {"state": "ok", "reason": None, "attempted": True,
            "query_ms": elapsed, "stdout": output}


def _nullable_integer(value, maximum=None):
    value = value.strip()
    if value in ("", "N/A", "[Not Supported]"):
        return None
    if not value.isdigit():
        raise ValueError("malformed_number")
    number = int(value)
    if maximum is not None and number > maximum:
        raise ValueError("impossible_value")
    return number


def _device(fields):
    if len(fields) != 6:
        raise ValueError("malformed_fields")
    raw_uuid, raw_driver, total, used, free, utilization = (field.strip() for field in fields)
    if not UUID.fullmatch(raw_uuid):
        raise ValueError("malformed_uuid")
    uuid = "GPU-" + raw_uuid[4:].lower()
    driver = None if raw_driver in ("", "N/A", "[Not Supported]") else raw_driver
    if driver is not None and not DRIVER.fullmatch(driver):
        raise ValueError("malformed_driver")
    total = _nullable_integer(total, MAX_MEMORY_MIB)
    used = _nullable_integer(used, MAX_MEMORY_MIB)
    free = _nullable_integer(free, MAX_MEMORY_MIB)
    utilization = _nullable_integer(utilization, 100)
    if ((total is not None and used is not None and used > total)
            or (total is not None and free is not None and free > total)):
        raise ValueError("impossible_memory")
    gauges = {"memory_total_bytes": None if total is None else total * MIB,
              "memory_used_bytes": None if used is None else used * MIB,
              "memory_free_bytes": None if free is None else free * MIB,
              "utilization_pct": utilization}
    complete = driver is not None and all(value is not None for value in gauges.values())
    return {"uuid": uuid, "driver_version": driver, "state": "ok" if complete else "partial",
            "reason": None if complete else "field_unavailable", **gauges}


def parse_output(output, query_ms=0.0, attempted=True):
    """Parse only the six requested inventory columns into typed, bounded gauges."""
    if not isinstance(output, bytes) or len(output) > MAX_STDOUT_BYTES:
        return unavailable_gpu("output_limit", query_ms, attempted)
    try:
        lines = [line for line in output.decode("ascii").splitlines() if line.strip()]
    except UnicodeDecodeError:
        return {"state": "partial", "reason": "malformed_output", "attempted": bool(attempted),
                "query_ms": max(0.0, float(query_ms)), "devices": []}
    if not lines:
        return {"state": "empty", "reason": "empty", "attempted": bool(attempted),
                "query_ms": max(0.0, float(query_ms)), "devices": []}

    devices, invalid, duplicates = [], False, set()
    seen = set()
    for line in lines:
        try:
            device = _device(line.split(","))
        except ValueError:
            invalid = True
            continue
        if device["uuid"] in seen:
            duplicates.add(device["uuid"])
            continue
        seen.add(device["uuid"])
        devices.append(device)
    if duplicates:
        devices = [device for device in devices if device["uuid"] not in duplicates]
    devices.sort(key=lambda device: device["uuid"])
    limited = len(devices) > MAX_DEVICES
    devices = devices[:MAX_DEVICES]
    if duplicates:
        reason = "duplicate_uuid"
    elif invalid:
        reason = "malformed_output"
    elif limited:
        reason = "device_limit"
    elif any(device["state"] != "ok" for device in devices):
        reason = "partial_reading"
    else:
        reason = None
    state = "ok" if reason is None else "partial"
    return {"state": state, "reason": reason, "attempted": bool(attempted),
            "query_ms": max(0.0, float(query_ms)), "devices": devices}


def collect_gpu(runner=None):
    """Collect one GPU inventory query; failures remain a typed batch measurement."""
    result = (run_query if runner is None else runner)()
    if result.get("state") != "ok":
        return unavailable_gpu(result.get("reason", "query_failed"), result.get("query_ms", 0),
                               result.get("attempted", True))
    return parse_output(result.get("stdout", b""), result.get("query_ms", 0),
                        result.get("attempted", True))
