"""hb.venue.venue - the venue a run drives on: one record per attempt, built and taken down
through the product's own commands (hb.venue.adapter), never by the engine's hand.

A venue is keyed by its issue and attempt. `up` stands it (the product's `venue.up`), signs in
when the venue has accounts (`venue.sign_in`), and records what came back: the origin, the
served sha, the tenant, the session. `seats` signs in k browsers for k testers. `down` takes
it down one recorded step at a time so a crash resumes. `list` and `sweep` read the records.
A stub venue (HB_VENUE_STUB=1) records what it would have run, so a lifecycle drill measures
the lifecycle without a product.
"""
import calendar
import json
import os
import shutil
import sys
import time
from pathlib import Path

from hb.rig.common import HB_ENTRY, atomic_write, canon, now_iso, out, run, state_dir
from hb.venue import adapter

DOWN_STEPS = ("venue", "browser", "session", "record")


# ── records ───────────────────────────────────────────────────────────────


def _venues_dir():
    d = state_dir() / "venues"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _venue_id(issue, attempt):
    n = str(issue).lstrip("#")
    import hashlib
    h = hashlib.sha1(f"{n}-{attempt}-{canon()}".encode("utf-8")).hexdigest()[:6]
    return f"v-{n}-{attempt}-{h}"


def _venue_name(issue, attempt):
    """The name the product's commands get: one venue per issue and attempt."""
    return f"venue-{str(issue).lstrip('#')}-{attempt}"


def _record_path(vid):
    return _venues_dir() / f"{vid}.json"


def _venue_dir(vid):
    d = _venues_dir() / vid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_write(rec):
    atomic_write(_record_path(rec["id"]), json.dumps(rec, indent=1))


def _record_read(vid):
    try:
        return json.loads(_record_path(vid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _records():
    rows = []
    for p in sorted(_venues_dir().glob("v-*.json")):
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            rows.append({"id": p.stem, "state": "unreadable"})
    return rows


def _find_live(issue, attempt):
    n = str(issue).lstrip("#")
    for rec in _records():
        if str(rec.get("issue")) == n and int(rec.get("attempt") or 1) == int(attempt) and rec.get("state") not in ("down", "unreadable"):
            return rec
    return None


def _worktree(a):
    return Path(a.worktree).resolve() if getattr(a, "worktree", None) else canon()


def _stub():
    return (os.environ.get("HB_VENUE_STUB") or "").strip() == "1"


def _leg(name, status, why=None, **extra):
    row = {"leg": name, "status": status}
    if why:
        row["why"] = why
    row.update(extra)
    return row


def _argv_or_stub(key, **values):
    """What the product's command would be, for the record; a tree with no venue block records that."""
    try:
        return adapter.argv(key, **values) if adapter.declared() else [f"<no venue.{key} in config>"]
    except SystemExit:
        return [f"<venue.{key} unusable>"]


# ── up ────────────────────────────────────────────────────────────────────


def _refuse_without_issue(a):
    if not a.issue:
        out({"error": "venue up needs --issue: the venue's identity derives from it.", "verdict": "usage"}, 3)


def _existing_is_stale(rec, worktree):
    """Why an existing record cannot be handed back: half-built, or recorded as serving another
    commit. Empty when it is fit to reuse. The served sha is the receipt's, never probed."""
    if rec.get("state") != "up":
        return f"record state is {rec.get('state')!r}, not up"
    served = str(rec.get("served_sha") or "")
    if not served:
        return ""
    rc, tip, _ = run(["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout=60)
    tip = (tip or "").strip()
    if tip and not (tip.startswith(served) or served.startswith(tip)):
        return f"serves {served[:9]}, the worktree tip is {tip[:9]}"
    return ""


def _leg_venue(a, rec, worktree):
    """The venue itself, through the product's `venue.up`; its receipt is the leg's truth."""
    cmd = _argv_or_stub("up", name=rec["name"], worktree=str(worktree), attempt=str(rec["attempt"]), options=a.options or "")
    if _stub():
        return _leg("venue", "stub", "HB_VENUE_STUB=1: recorded, not run", cmd=cmd,
                    receipt={"origin": "http://localhost:0", "served_sha": None, "facts": {}})
    if not adapter.declared():
        return _leg("venue", "unmeasured", "this product declares no venue block in harness-config.json", cmd=cmd)
    problems = adapter.check_config()
    if problems:
        return _leg("venue", "unmeasured", "; ".join(problems), cmd=cmd)
    receipt, why = adapter.up(rec["name"], worktree, attempt=rec["attempt"], options=a.options or "",
                              timeout=(a.timeout_min or 20) * 60)
    if why:
        return _leg("venue", "unmeasured", why, cmd=cmd)
    return _leg("venue", "ok", None, cmd=cmd, receipt=receipt)


def _leg_browser(rec, vdir):
    """A profile folder for the tester seats, only where the venue offers a screen."""
    if not _stub() and not adapter.offers("ui"):
        rec["browser_profile"] = ""
        return _leg("browser", "not-offered", adapter.not_offered("ui"))
    browser = vdir / "browser"
    (browser / "profile").mkdir(parents=True, exist_ok=True)
    rec["browser_profile"] = str(browser / "profile")
    return _leg("browser", "ok", None, profile=rec["browser_profile"])


def _leg_session(rec, where):
    """A signed-in session for one browser seat, through `venue.sign_in`, when the venue has accounts."""
    if _stub():
        return _leg("session", "stub", "HB_VENUE_STUB=1: recorded, not run", cmd=_argv_or_stub("sign_in", name=rec["name"]))
    if not adapter.offers("accounts"):
        return _leg("session", "not-offered", adapter.not_offered("accounts"))
    js, why = adapter.sign_in(rec["name"])
    if why:
        return _leg("session", "unmeasured", why)
    session = js["session"]
    # a session the product hands back as a path is copied beside the seat; a token is written there
    target = Path(where)
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(session, str) and Path(session).is_file():
        shutil.copyfile(session, target)
    else:
        atomic_write(target, json.dumps(session if not isinstance(session, str) else {"token": session}, indent=1))
    return _leg("session", "ok", None, session=str(target), user=js.get("user"))


def cmd_venue_up(a):
    """Build the venue and hand over its id, or refuse and say what is in the way."""
    _refuse_without_issue(a)
    worktree = _worktree(a)
    existing = _find_live(a.issue, a.attempt)
    if existing:
        why = _existing_is_stale(existing, worktree)
        if not why:
            out({"venue": existing["id"], "verdict": "existing", "reused": True,
                 "detail": "a venue for this issue and attempt is already standing and serves this worktree's "
                           "tip; a second up returns it rather than building a rival",
                 "state": existing.get("state"), "origin": existing.get("origin"), "served_sha": existing.get("served_sha"),
                 "model": existing.get("model"), "record": str(_record_path(existing["id"]))}, 0)
        rc_d, o_d, e_d = run([sys.executable, HB_ENTRY, "venue", "down", "--venue", existing["id"], "--keep-db"], timeout=600)
        after = _record_read(existing["id"]) or {}
        if after.get("state") != "down":
            out({"error": f"the stale venue {existing['id']} ({why}) could not be taken down: {(e_d or o_d or '')[-300:]}",
                 "verdict": "refused-stale-venue-stays"}, 2)

    vid = _venue_id(a.issue, a.attempt)
    vdir = _venue_dir(vid)
    rec = {"id": vid, "issue": str(a.issue).lstrip("#"), "attempt": int(a.attempt), "owner": a.owner,
           "state": "building", "created": now_iso(),
           "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
           "worktree": str(worktree), "dir": str(vdir), "name": _venue_name(a.issue, a.attempt),
           "model": "real" if a.real_llm else "mock",
           "model_note": "a receipt claiming model JUDGMENT is UNMEASURED unless model=real",
           "tz": "UTC", "slug": vid, "stub": _stub(), "capabilities": list(adapter.capabilities()) if adapter.declared() and not _stub() else [],
           "legs": []}
    _record_write(rec)

    leg = _leg_venue(a, rec, worktree)
    receipt = leg.get("receipt") or {}
    rec["origin"] = receipt.get("origin") or ""
    rec["served_sha"] = receipt.get("served_sha")
    rec["tenant_id"] = receipt.get("tenant_id")
    rec["facts"] = receipt.get("facts") if isinstance(receipt.get("facts"), dict) else {}
    rec["legs"].append(leg)
    _record_write(rec)
    rec["legs"].append(_leg_browser(rec, vdir))
    _record_write(rec)
    rec["storage_state"] = str(vdir / "browser" / "storage-state.json")
    if leg["status"] in ("ok", "stub"):
        rec["legs"].append(_leg_session(rec, rec["storage_state"]))
    else:
        rec["legs"].append(_leg("session", "unmeasured", "the venue leg did not land"))
    if leg["status"] == "ok" and adapter.offers("model"):
        ok, said = adapter.set_model(rec["name"], a.real_llm)
        rec["legs"].append(_leg("model", "ok" if ok else "unmeasured", None if ok else str(said)[-200:], real=bool(a.real_llm)))
    _record_write(rec)
    bad = [l["leg"] for l in rec["legs"] if l["status"] not in ("ok", "stub", "not-offered")]
    rec["state"] = "incomplete" if bad else "up"
    _record_write(rec)
    payload = {"venue": vid, "verdict": rec["state"], "origin": rec["origin"], "served_sha": rec["served_sha"],
               "tenant_id": rec["tenant_id"], "facts": rec["facts"], "model": rec["model"], "tz": "UTC", "owner": a.owner,
               "worktree": str(worktree), "name": rec["name"], "browser_profile": rec["browser_profile"],
               "storage_state": rec["storage_state"], "capabilities": rec["capabilities"],
               "record": str(_record_path(vid)), "legs": rec["legs"]}
    if bad:
        payload["error"] = "the venue stands but these legs are UNMEASURED: " + ", ".join(bad) + ". A receipt from it says so on its face."
    out(payload, 2 if bad else 0)


# ── seats: one signed-in browser per concurrent tester ────────────────────


def cmd_venue_seats(a):
    """K browser seats on a standing venue, one per tester that drives it at the same time,
    each with its own profile and its own sign-in (a copied session shares one refresh token,
    and the first seat to refresh logs the others out)."""
    rec = _record_read(a.venue) if a.venue else None
    if rec is None:
        out({"error": f"no venue record for {a.venue}", "verdict": "unknown-venue"}, 2)
    if rec.get("state") != "up":
        out({"error": f"venue {a.venue} is {rec.get('state')!r}, not up; seats sign in to a standing venue", "verdict": "refused-not-up"}, 2)
    count = max(1, int(a.count or 1))
    vdir = _venue_dir(rec["id"])
    seats, bad = [], []
    for k in range(1, count + 1):
        sdir = vdir / "browser" / f"seat-{k}"
        (sdir / "profile").mkdir(parents=True, exist_ok=True)
        seat = {"seat": k, "browser_profile": str(sdir / "profile"), "storage_state": str(sdir / "storage-state.json")}
        session = _leg_session(rec, seat["storage_state"])
        seat["legs"] = [session]
        if session["status"] not in ("ok", "stub", "not-offered"):
            bad.append(k)
        seats.append(seat)
    rec["seats"] = seats
    _record_write(rec)
    if bad:
        out({"venue": rec["id"], "verdict": "incomplete", "seats": [s for s in seats if s["seat"] not in bad],
             "error": "seats " + ", ".join(str(k) for k in bad) + " could not sign in: "
                      + "; ".join(str(l.get("why")) for s in seats if s["seat"] in bad for l in s["legs"]
                                  if l["status"] not in ("ok", "stub", "not-offered"))[:400]}, 2)
    out({"venue": rec["id"], "verdict": "ok", "seats": seats}, 0)


# ── down ──────────────────────────────────────────────────────────────────


def cmd_venue_down(a):
    """Take the venue down, one recorded step at a time, so a crash resumes."""
    if not a.venue:
        out({"error": "venue down needs --venue <id>", "verdict": "usage"}, 3)
    rec = _record_read(a.venue)
    if rec is None:
        out({"error": f"no venue record for {a.venue}", "verdict": "unknown-venue", "known": [r["id"] for r in _records()]}, 2)
    if rec.get("state") == "down":
        if a.keep_db or rec.get("db") != "kept":
            out({"venue": rec["id"], "verdict": "already-down", "down_at": rec.get("down_at"),
                 "steps_done": rec.get("down_steps") or list(DOWN_STEPS)}, 0)
        # a venue stopped with its data kept is dropped by a plain down: the venue step runs again
        rec["down_steps"] = [s for s in rec.get("down_steps") or [] if s != "venue"]

    done = list(rec.get("down_steps") or [])
    crash_at = (os.environ.get("HB_VENUE_STUB_CRASH") or "").strip()
    vdir = Path(rec.get("dir") or _venue_dir(rec["id"]))
    name = rec.get("name") or _venue_name(rec["issue"], rec["attempt"])
    steps = []
    for step in DOWN_STEPS:
        if step in done:
            steps.append({"step": step, "status": "already-done"})
            continue
        if crash_at == step:
            sys.stdout.flush()
            os._exit(70)
        if step == "venue":
            cmd = _argv_or_stub("down", name=name, keep="--keep" if a.keep_db else "")
            if _stub():
                steps.append({"step": step, "status": "stub", "cmd": cmd})
            elif not adapter.declared():
                steps.append({"step": step, "status": "unmeasured", "why": "no venue block in config", "cmd": cmd})
            else:
                ok, tail = adapter.down(name, keep=a.keep_db)
                steps.append({"step": step, "status": "ok" if ok else "unmeasured", "cmd": cmd,
                              "why": None if ok else tail, "db": "kept" if a.keep_db else "dropped"})
        elif step == "browser":
            shutil.rmtree(vdir / "browser", ignore_errors=True)
            steps.append({"step": step, "status": "ok", "gone": not (vdir / "browser").exists()})
        elif step == "session":
            for f in [*vdir.glob("*.json")]:
                if f.name != f"{rec['id']}.json" and "storage" in f.name or f.name.startswith("token"):
                    try:
                        f.unlink()
                    except OSError:
                        pass
            steps.append({"step": step, "status": "ok"})
        elif step == "record":
            steps.append({"step": step, "status": "ok"})
        done.append(step)
        rec["down_steps"] = done
        rec["state"] = "down-partial"
        _record_write(rec)

    rec["state"] = "down"
    rec["down_at"] = now_iso()
    rec["db"] = "kept" if a.keep_db else "dropped"
    _record_write(rec)
    bad = [s["step"] for s in steps if s.get("status") == "unmeasured"]
    out({"venue": rec["id"], "verdict": "down" if not bad else "down-unmeasured", "steps": steps,
         "db": "kept" if a.keep_db else "dropped", "unmeasured": bad, "record": str(_record_path(rec["id"]))}, 2 if bad else 0)


# ── list, sweep ───────────────────────────────────────────────────────────


def _age_hours(rec):
    try:
        t = calendar.timegm(time.strptime(rec.get("created", ""), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None
    return round((time.time() - t) / 3600.0, 2)


def _row(rec):
    return {"venue": rec.get("id"), "issue": rec.get("issue"), "attempt": rec.get("attempt"), "owner": rec.get("owner"),
            "state": rec.get("state"), "age_hours": _age_hours(rec), "origin": rec.get("origin"),
            "served_sha": rec.get("served_sha"), "name": rec.get("name"), "model": rec.get("model"), "tz": rec.get("tz"),
            "stub": rec.get("stub", False)}


def cmd_venue_list(a):
    rows = [_row(r) for r in _records()]
    out({"venues": rows, "count": len(rows)}, 0)


def cmd_venue_sweep(a):
    """Orphans: older than --older-than-hours, or serving a worktree whose branch landed."""
    cutoff = float(a.older_than_hours)
    landed = {str(Path(p).resolve()).lower() for p in getattr(a, "landed", None) or []}
    picked = []
    for rec in _records():
        if rec.get("state") in ("down", "unreadable"):
            continue
        age = _age_hours(rec)
        reasons = []
        if age is not None and age >= cutoff:
            reasons.append(f"age {age}h >= {cutoff}h")
        if rec.get("worktree") and str(Path(rec["worktree"]).resolve()).lower() in landed:
            reasons.append("its worktree's branch landed")
        if reasons:
            picked.append({"row": _row(rec), "reasons": reasons})
    swept = []
    if a.apply:
        for p in picked:
            rc, o, _ = run([sys.executable, HB_ENTRY, "venue", "down", "--venue", p["row"]["venue"]], timeout=900)
            swept.append({"venue": p["row"]["venue"], "true_exit": rc, "tail": (o or "")[-200:]})
    out({"verdict": "swept" if a.apply else "dry-run", "older_than_hours": cutoff, "candidates": picked,
         "count": len(picked), "swept": swept}, 0)


def cmd_venue(a):
    {"up": cmd_venue_up, "down": cmd_venue_down, "list": cmd_venue_list, "sweep": cmd_venue_sweep, "seats": cmd_venue_seats}[a.op](a)


def register(sub):
    s = sub.add_parser("venue", help="the venue a run drives on: up|down|list|sweep|seats, through the product's venue commands")
    s.add_argument("op", choices=["up", "down", "list", "sweep", "seats"])
    s.add_argument("--issue", help="up: the issue the venue is built for; its name is keyed by it")
    s.add_argument("--attempt", type=int, default=1, help="up: which attempt at this issue; issue+attempt is the venue's identity")
    s.add_argument("--owner", default="", help="up: who stood it, recorded on the venue")
    s.add_argument("--worktree", help="up: the tree whose code the venue serves (default: cwd's)")
    s.add_argument("--real-llm", dest="real_llm", action="store_true",
                   help="up: OPT IN to the product's real model (venue.set_model); without it no receipt may claim model judgment")
    s.add_argument("--options", default="", help="up: extra text the product's venue.up command takes in {options}")
    s.add_argument("--timeout-min", dest="timeout_min", type=int, default=20, help="up: bound on the venue leg")
    s.add_argument("--venue", help="down/seats: the venue id")
    s.add_argument("--count", type=int, default=1, help="seats: how many browser seats, each signed in on its own")
    s.add_argument("--keep-db", dest="keep_db", action="store_true", help="down: stop the venue but keep its data ({keep} in venue.down)")
    s.add_argument("--older-than-hours", dest="older_than_hours", type=float, default=12.0, help="sweep: age at which a venue with no closed owner is an orphan")
    s.add_argument("--landed", nargs="*", default=[], help="worktrees whose branch landed: their venues are orphans")
    s.add_argument("--apply", action="store_true", help="sweep: actually take them down")
