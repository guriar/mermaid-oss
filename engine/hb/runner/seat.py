"""hb.run.seat - run one seat and judge what it wrote.

A seat is a subprocess: brief in, report out. This module launches it through its
runtime adapter, then reads the report file and checks it against the schema and
the tree's HEAD. The verdicts the runner acts on:

  ok              a valid report for this tip
  invalid-report  the seat returned but the report is missing, unparseable, or
                  fails its schema; the problems are named
  rate-limited    the CLI said so; wait for the reset before another attempt
  stalled         no activity for the role's stall window; relaunched from progress
  context         a builder's running context crossed the ceiling (adapters.context_ceiling);
                  it is relaunched on its own progress file
  cut             the conductor's control file cut it; relaunched, not counted
  timeout         the fuse cut it
  crashed         non-zero exit with no report
  missing         the runtime is not on this host
"""
import json
from pathlib import Path

from hb.rig.common import report_text
from hb.runner import adapters
from hb.rig import gitops
from hb.runner.schemas import check_report



def run_seat(role, brief_path, out_path, cwd, run_id, attempt, runtime=None, model=None,
             timeout_s=3600, base_sha=None, env=None, tools=None):
    """Launch the seat and validate its report. Never raises for the seat's sake."""
    name, default_model = adapters.seat_runtime(role, runtime)
    model = model or default_model
    ok, why = adapters.probe(name, role)
    if not ok:
        return {"result": "missing", "why": why, "runtime": name, "model": model,
                "report": None, "problems": [why], "launch": None}
    out_path = Path(out_path)
    if out_path.exists():
        out_path.unlink()
    effort = adapters.seat_effort(role)
    launch = adapters.launch(name, role, brief_path, cwd, out_path.parent, model=model,
                             timeout_s=timeout_s, env=env, effort=effort, tools=tools,
                             context_ceiling=adapters.context_ceiling() if role in ("build", "verify") else None)
    res = {"runtime": name, "model": model, "effort": effort, "launch": launch, "report": None,
           "problems": []}
    if launch["exit_class"] in ("rate-limited", "timeout", "missing", "stalled", "cut", "context"):
        res["result"] = launch["exit_class"]
        return res
    if not out_path.exists():
        res["result"] = "crashed" if launch["exit_class"] == "crashed" else "invalid-report"
        res["problems"] = [f"no report at {out_path}"]
        return res
    try:
        # a report is judged by its content, never by its codec or its first byte
        report = json.loads(report_text(out_path))
    except (json.JSONDecodeError, OSError) as ex:
        res["result"] = "invalid-report"
        res["problems"] = [f"report is not JSON: {ex}"]
        return res
    # A verify seat is blind to the diff, so the base sha is not a fact it holds;
    # the runner fills it rather than respawning a seat over a field it cannot know.
    if role == "verify" and isinstance(report, dict) and not report.get("base_sha") and base_sha:
        report["base_sha"] = base_sha
    problems = check_report(report, role, run_id, attempt, gitops.head_of(cwd), base_sha)

    if role == "verify" and not problems:
        # the check grades a proofless WORKS down in place; the file every later reader opens says so too
        out_path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    res["report"] = report
    res["problems"] = problems
    res["result"] = "ok" if not problems else "invalid-report"
    return res
