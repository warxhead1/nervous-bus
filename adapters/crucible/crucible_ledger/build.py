"""View generation. Built in: the decisions view. Other lanes add views by
shipping crucible_<group>/views.py beside the dispatcher with
`render(records, state) -> {relative_path: text}`."""
import glob
import importlib.util
import json
import os
import subprocess

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DECISIONS_DIR = os.path.join("design", "decisions")


def decisions_view(records, state):
    out = {}
    for e in state.values():
        if e["kind"] != "decision":
            continue
        b = e["body"]
        lines = [
            "---",
            f"id: {e['id']}",
            f"state: {e['state']}",
            f"authority: {b.get('authority', '')}",
            f"door: {b.get('door', '')}",
            f"question: {b.get('q_id', '')}",
            f"supersedes: {b.get('supersedes') or ''}",
            f"touches: {json.dumps(b.get('touches', []))}",
            f"constrains: {json.dumps(b.get('constrains', []))}",
            "---",
            "",
            f"# {e['id']}",
            "",
            "Chosen: " + json.dumps(b.get("chosen", {}), sort_keys=True),
            "",
        ]
        out[os.path.join(DECISIONS_DIR, e["id"] + ".md")] = "\n".join(lines)
    return out


def discover_views():
    found = []
    for path in sorted(glob.glob(os.path.join(PKG_PARENT, "crucible_*", "views.py"))):
        name = "crucible_view_" + os.path.basename(os.path.dirname(path))
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "render"):
            found.append((path, mod.render))
    return found


def expected_views(records, state):
    views = dict(decisions_view(records, state))
    for path, render in discover_views():
        for rel, text in render(records, state).items():
            if rel in views:
                raise ValueError(f"view {rel} produced twice (second: {path})")
            views[rel] = text
    return views


def diff(root, views):
    drift = []
    for rel, text in sorted(views.items()):
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            drift.append({"path": rel, "kind": "missing"})
        else:
            with open(p, encoding="utf-8") as f:
                if f.read() != text:
                    drift.append({"path": rel, "kind": "changed"})
    ddir = os.path.join(root, DECISIONS_DIR)
    if os.path.isdir(ddir):
        for n in sorted(os.listdir(ddir)):
            rel = os.path.join(DECISIONS_DIR, n)
            if n.startswith("d-") and n.endswith(".md") and rel not in views:
                drift.append({"path": rel, "kind": "extra"})
    return drift


def write(root, views, drift):
    for d in drift:
        p = os.path.join(root, d["path"])
        if d["kind"] == "extra":
            os.unlink(p)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(views[d["path"]])


def commit(root, paths):
    """Commit regenerated views on the current (design) branch. Returns the sha or None."""
    if not paths:
        return None
    subprocess.run(["git", "-C", root, "add", "-A", "--"] + paths, check=True)
    r = subprocess.run(["git", "-C", root, "diff", "--cached", "--quiet", "--"] + paths)
    if r.returncode == 0:
        return None
    subprocess.run(["git", "-C", root, "commit", "-q", "-m", "ledger: regenerate views", "--"] + paths,
                   check=True)
    out = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    return out.stdout.strip()
