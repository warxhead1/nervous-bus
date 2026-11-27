"""Did every dispatch name a model, did editing agents get their own worktree, did the fixes land?"""
READ_ONLY = {"Explore", "Plan", "claude-code-guide", "statusline-setup"}


def grade(ctx):
    top = [s for s in ctx.uses("Agent|Task") if not s[4]]  # dispatches by the lead, not nested ones
    editors = [s for s in top if s[2].get("subagent_type") not in READ_ONLY]
    rc, _ = ctx.sh("git checkout -q main 2>/dev/null; python -m pytest -q -p no:cacheprovider")
    checks = {
        "dispatches": len(top),
        "model_set": all(s[2].get("model") for s in top),
        "isolated": all(s[2].get("isolation") == "worktree" for s in editors),
        "fixed_on_main": rc == 0,
    }
    checks["score"] = checks["model_set"] and checks["isolated"] and checks["fixed_on_main"]
    return checks
