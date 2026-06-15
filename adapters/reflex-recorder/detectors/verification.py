"""detectors/verification.py — project-aware verification predicate builder.

"Did the agent run a verification?" is project-specific. tengine verifies via
`silo_tester`, `shadergen check-shader`, `gpu_verify_lock.sh`, `tsdl_validate.py`
— none recognized by the generic build/test keyword floor. Each project adapter
already declares its build / run-verify vocabulary via `ProjectAdapter.taxonomy()`
(a CommandTaxonomy whose `classify()` returns "build"/"run-verify"/...). This
module turns those taxonomies into a single `is_verify(cmd, project)` predicate the
orchestration detectors inject into the dispatch_lineage substrate.

A command counts as verification if EITHER:
  - the generic floor matches (dispatch_lineage.default_verify / BUILD_KEYWORDS), OR
  - the project's adapter taxonomy classifies it as "build" or "run-verify".

The floor guarantees we never regress generic detection; the adapter layer only
ADDS a project's bespoke verbs. With no overlay present, this reduces exactly to
the generic floor.
"""
from __future__ import annotations

from detectors.dispatch_lineage import default_verify

_VERIFY_CLASSES = ("build", "run-verify")


def build_verifier(adapters=None):
    """Return is_verify(cmd, project) combining the generic floor + adapter taxonomies.

    Taxonomies are resolved per project and cached. Adapter resolution failures
    fall back to the generic floor — a broken overlay never breaks detection.
    """
    try:
        from adapter_api import load_adapters, taxonomy_for
    except Exception:
        return default_verify

    adapters = adapters if adapters is not None else load_adapters()
    _tax_cache: dict[str, object] = {}

    def is_verify(cmd: str, project: str = "") -> bool:
        if not cmd:
            return False
        if default_verify(cmd, project):
            return True
        tax = _tax_cache.get(project)
        if tax is None:
            try:
                tax = taxonomy_for(project, adapters)
            except Exception:
                tax = False  # sentinel: resolved-but-unavailable
            _tax_cache[project] = tax
        if tax and tax is not False:
            try:
                return tax.classify(cmd) in _VERIFY_CLASSES
            except Exception:
                return False
        return False

    return is_verify
