"""Counter deltas are valid only for one boot, entity and uninterrupted interval."""


def intervalize(current, previous, boot_id, mono, wall, max_gap_s=150):
    unavailable = {"state": "unavailable", "elapsed_s": None, "deltas": {}, "rates": {}}
    if not previous or current["state"] != "ok" or previous["entity_data"]["state"] != "ok":
        return {**unavailable, "reason": "no_previous_or_unavailable"}
    old = previous["entity_data"]
    if not boot_id or not previous["boot_id"] or not current["identity"] or not old["identity"]:
        return {**unavailable, "reason": "identity_unavailable"}
    if (boot_id != previous["boot_id"] or current["identity"] != old["identity"]
            or current.get("device_identity") != old.get("device_identity")):
        return {**unavailable, "state": "reset", "reason": "identity_changed"}
    elapsed, wall_elapsed = mono - previous["monotonic_s"], wall - previous["ts"]
    if elapsed <= 0 or elapsed > max_gap_s or abs(wall_elapsed - elapsed) > 5:
        return {**unavailable, "reason": "clock_or_sampling_gap"}
    deltas = {}
    for key, value in current["counters"].items():
        prior = old["counters"].get(key)
        if value is None or prior is None:
            deltas[key] = None
        elif value < prior:
            return {**unavailable, "state": "reset", "reason": "counter_decreased"}
        else:
            deltas[key] = value - prior
    return {"state": "ok", "reason": None, "elapsed_s": elapsed, "deltas": deltas,
            "rates": {key + "_per_s": value / elapsed if value is not None else None
                      for key, value in deltas.items()}}
