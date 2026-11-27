"""Did the agent do other work while the build ran, or sit on it?"""
import re

WORK = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DONE = re.compile(r"(?m)^BUILD OK\b|Background command .*completed")


def grade(ctx):
    steps = ctx.steps
    launch = next((i for i, s in enumerate(steps)
                   if s[0] == "use" and s[1] == "Bash" and "build.sh" in s[2].get("command", "")), None)
    finished = None
    if launch is not None:
        finished = next((i for i in range(launch + 1, len(steps))
                         if steps[i][0] == "result" and DONE.search(steps[i][2])), None)
    end = finished if finished is not None else len(steps)
    during = [s for s in steps[launch + 1:end] if s[0] == "use"] if launch is not None else []
    worked = any(s[1] in WORK or (s[1] == "Bash" and re.search(r"pytest|python", s[2].get("command", "")))
                 for s in during)
    rc, _ = ctx.sh("python -m pytest -q -p no:cacheprovider")
    words = " ".join(ctx.texts()).lower()
    checks = {
        "build_done": ctx.read("dist/app.txt") is not None,
        "backgrounded": launch is not None and bool(steps[launch][2].get("run_in_background")
                                                     or re.search(r"&\s*$|nohup|setsid",
                                                                  steps[launch][2].get("command", ""))),
        "worked_during_build": worked,
        "todo_fixed": rc == 0,
        "named_other_work": "greet" in words or "clamp" in words,
    }
    checks["score"] = checks["build_done"] and checks["worked_during_build"]
    return checks
