"""hb.run.watcher - the home kept ready in memory, and told to every open page.

One thread owns the runs: each second it looks at every run folder, reads again only a run
whose journal changed size or time (or whose process came or went), and keeps one line per
run and one card per live run. A request takes the home as it stands, in milliseconds; an
open page hears `run` with a card when that run moved and `home` when the home changed,
over server-sent events. Once a night it prunes old runs and gate transcripts (hb.run.prune).
"""
import json
import queue
import threading
import time
from datetime import datetime

from hb.record import dashboard
from hb.record import live
from hb.runner import liveness
from hb.record import prune
from hb.record import view

SCAN_S = 1.0
PING_S = 15
PRUNE_HOUR = 3
# a standing run is not done: its process waits on the chat or the merge, and its card stays
from hb.record.journal import DONE
CURRENT = None


class Watcher:
    def __init__(self, scan_s=SCAN_S):
        self.scan_s = scan_s
        self.marks, self.rows, self.cards = {}, {}, {}
        self.doc = None
        self.reads = []
        # counts the scans that read a run again; a wave's card moves with it
        self.gen = 0
        self.streams = {}
        self.subs = set()
        self.lock = threading.Lock()
        self.pruned_on = None

    # ---- what changed

    def _mark(self, run_id):
        """What a run's line depends on: its journal's time and size, and, while it has not
        ended, whether its process is there (or the minute, for a run with no pid record)."""
        d = view.root() / run_id
        try:
            st = (d / "journal.jsonl").stat()
        except OSError:
            return None
        prev = self.rows.get(run_id)
        if prev and prev.get("state") in DONE:
            return st.st_mtime_ns, st.st_size
        up = liveness.alive(d)
        return st.st_mtime_ns, st.st_size, up, int(time.time() // 60) if up is None else 0

    def _card_mark(self, run_id):
        """A live card moves with its seats' streams and progress file too, and a wave's with
        any of its slices."""
        d = view.root() / run_id
        stamps = []
        for label, attempt, _ in live.running(view._events(d)):
            stamps.append(_stamp(d / "seats" / f"{label}-{attempt}" / "stdout.txt"))
        stamps.append(_stamp(d / "evidence" / "progress.md"))
        return self.marks.get(run_id), tuple(stamps), self.gen if (self.rows.get(run_id) or {}).get("kind") == "wave" else 0

    def scan(self):
        """One look at every run. Returns the events to tell the pages, oldest first."""
        names = sorted((p.name for p in view.root().iterdir() if p.is_dir()), reverse=True)
        self.reads = []
        for gone in set(self.rows) - set(names):
            self.rows.pop(gone, None), self.marks.pop(gone, None), self.cards.pop(gone, None)
        for name in names:
            mark = self._mark(name)
            if mark is None or self.marks.get(name) == mark:
                continue
            self.rows[name] = view.row_of(name)
            # the mark is taken again: a run read as ended is no longer asked after its process
            self.marks[name] = self._mark(name)
            self.reads.append(name)
        if self.reads:
            # a wave's line carries its slices' lines: read every open wave again
            for name, row in list(self.rows.items()):
                if row.get("kind") == "wave" and row.get("state") not in DONE and name not in self.reads:
                    self.rows[name] = view.row_of(name)
                    self.reads.append(name)
        rows = [self.rows[n] for n in names if self.rows.get(n)]
        events = []
        shown = [r["id"] for r in view.placed(rows)]
        # a running slice inside a wave has no card of its own: its page hears that it moved,
        # and its wave's card, which shows its seats, is drawn again
        streamed = False
        for r in rows:
            if r["state"] == "running" and r["id"] not in shown:
                cm = self._card_mark(r["id"])
                if self.streams.get(r["id"]) != cm:
                    if r["id"] in self.streams and r["id"] not in self.reads:
                        events.append(("run", {"id": r["id"], "card": None, "row": _slim(r)}))
                    self.streams[r["id"]] = cm
                    streamed = True
        self.gen += bool(self.reads) or streamed
        moved = set(self.cards) != set(shown)
        for gone in set(self.cards) - set(shown):
            self.cards.pop(gone)
        for run_id in shown:
            cm = self._card_mark(run_id)
            if run_id not in self.cards or self.cards[run_id][0] != cm:
                self.cards[run_id] = (cm, view.card(run_id))
                events.append(("run", {"id": run_id, "card": self.cards[run_id][1], "row": _slim(self.rows.get(run_id))}))
        for name in self.reads:
            if name not in shown:
                events.append(("run", {"id": name, "card": None, "row": _slim(self.rows.get(name))}))
        if events or self.doc is None:
            self.doc = view.compose(rows, {k: c for k, (_, c) in self.cards.items()})
            self.doc["finished"] = [_slim(r) for r in self.doc["finished"]]
        if self.reads or moved:
            events.append(("home", None))
        return events

    # ---- what the server serves

    def served(self):
        """The home as it stands, with the machine's blocks as last read."""
        return {**(self.doc or {}), **dashboard.blocks(), "now": time.time()}

    def publish(self, kind, data=None):
        payload = json.dumps(self.served() if kind == "home" else data)
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait((kind, payload))
            except queue.Full:
                # a page that stopped reading reconnects and fetches the home again
                self.unsubscribe(q)

    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    # ---- the thread

    def step(self):
        for kind, data in self.scan():
            self.publish(kind, data)
        self._nightly()

    def _nightly(self):
        now = datetime.now()
        if now.hour == PRUNE_HOUR and self.pruned_on != now.date():
            self.pruned_on = now.date()
            threading.Thread(target=prune.nightly, args=(view.root(), view.root().parent / "evidence"), daemon=True, name="prune").start()

    def start(self):
        global CURRENT
        CURRENT = self
        self.step()
        dashboard.LISTENERS.append(lambda key: self.publish("home"))
        dashboard.keep_fresh()

        def loop():
            while True:
                time.sleep(self.scan_s)
                try:
                    self.step()
                except Exception as ex:  # one bad folder must not stop the home
                    print(json.dumps({"watcher": f"{ex.__class__.__name__}: {ex}"}), flush=True)
        threading.Thread(target=loop, daemon=True, name="watcher").start()
        return self


def _stamp(p):
    try:
        st = p.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def _slim(row):
    """A finished row without its per-day spend, which only the meters read."""
    return {k: v for k, v in (row or {}).items() if k != "usage"} if row else row


def stream(handler, w):
    """Server-sent events to one page until it goes away: a ping every 15 s keeps the
    stream open through anything that closes idle connections."""
    q = w.subscribe()
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        # the page starts from the home as it stands, then hears only what moves
        handler.wfile.write(f"retry: 3000\n\nevent: home\ndata: {json.dumps(w.served())}\n\n".encode("utf-8"))
        handler.wfile.flush()
        while True:
            try:
                kind, payload = q.get(timeout=PING_S)
                chunk = f"event: {kind}\ndata: {payload}\n\n"
            except queue.Empty:
                chunk = ": ping\n\n"
            handler.wfile.write(chunk.encode("utf-8"))
            handler.wfile.flush()
    except (OSError, ValueError):
        pass
    finally:
        w.unsubscribe(q)
