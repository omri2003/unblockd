"""Load test (CORE-40): one board with 20,000 tickets (most of them done, like a real board after a while) and 200 agents
working it at once, on any connector. Not part of `python -m unittest` (it takes minutes); CI runs it on Postgres and
MongoDB in the opt-in workflow .github/workflows/load.yml.

	python -m tests.load                                   # files, in a temporary folder
	python -m tests.load --location "sqlite:///C:/tmp/load.db#load"
	python -m tests.load --location "postgres://u:p@127.0.0.1:5432/db#load" --json load-pg.json
	python -m tests.load --tickets 2000 --agents 20        # a quick run

A database location must not hold that board yet; it is dropped at the end (--keep keeps it). Phases:
1. seed: --tickets tickets through create_many (50 per call): --open of them todo (every 10th blocked by the one before,
   every 5th touching one of 40 paths, so overlapping claims happen), the rest done.
2. single-agent times on the full board: list / get / next_ready / needs (GET /api/needs) / claim + release, both right
   after the caches are emptied (= a server restart) and warm (median of several).
3. swarm: --agents threads, each with its own Store (its own connection on a database), released together. Each agent
   creates one ticket, then up to --rounds times: claim_next -> heartbeat -> add_note -> complete. A "try again" (the
   board lock was busy for 10 s) is retried with backoff and counted.
4. checks: no ticket handed to two agents (agents' own records and the activity log's claim events), every note written
   is on its ticket, every created id is distinct and exists, every completed ticket is done by the agent that claimed it.
Exit code 1 when a check fails. Agents that gave up (the lock stayed busy through every retry) are counted apart
(`agents_gave_up`): that is capacity, not a wrong answer. Prints one JSON report (also written to --json).
"""
import argparse
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time

from unblockd import connectors, files, store as st, web
from unblockd.errors import TicketError

PATHS = [f"src/mod{i}/*.py" for i in range(40)]


def ms(s: float) -> float:
	return round(s * 1000, 1)


def timed(fn, n: int = 1) -> tuple:
	"""(first call, median of the n calls) in ms, and the last result."""
	times, out = [], None
	for _ in range(n):
		t0 = time.perf_counter()
		out = fn()
		times.append(time.perf_counter() - t0)
	return ms(times[0]), ms(statistics.median(times)), out


def empty_caches() -> None:
	"""What a fresh server process starts with: every module-level cache empty."""
	files._RAW.clear()
	files._LOG.clear()
	st._DEC.clear()
	for name in ("sql", "mongo"):
		mod = sys.modules.get(f"unblockd.{name}")
		if mod is not None:
			mod._CACHE.clear()


def retry(fn, stats: dict, tries: int = 8):
	"""fn(), retrying a busy board lock ("try again") with backoff; anything else raises."""
	for i in range(tries):
		try:
			return fn()
		except TicketError as e:
			if "try again" not in str(e) or i == tries - 1:
				raise
			stats["retries"] += 1
			time.sleep(min(2.0, 0.05 * 2 ** i) * (0.5 + random.random()))


def pct(xs: list, p: float) -> float:
	if not xs:
		return 0.0
	xs = sorted(xs)
	return ms(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))])


def seed(s: st.Store, n: int, open_n: int) -> None:
	"""n tickets written straight through the connector (bulk_load on a database, one file each on files), shaped exactly
	like Store.create's: two templates are made by Store.create on a scratch folder board, then copied with new ids.
	Why not create_many: on files every write re-derives the whole board, so seeding 20,000 that way is quadratic (4,000
	tickets took 11 minutes, CORE-40); the times that matter (create on a full board) are measured in single()."""
	tmp = tempfile.mkdtemp(prefix="unblockd-tpl-")
	try:
		t = st.Store(tmp)
		todo = t.backend.read(t.create({"title": "x", "body": "x" * 200}, prefix="LD")["id"])
		done = t.backend.read(t.create({"title": "x", "body": "x" * 200, "status": "done"}, prefix="LD")["id"])
	finally:
		shutil.rmtree(tmp, ignore_errors=True)
	items = []
	for i in range(n):
		it = json.loads(json.dumps(todo if i < open_n else done))
		it.update(id=f"LD-{i + 1}", title=f"load ticket {i}")
		if i < open_n:
			if i % 5 == 0:
				it["touches"] = [PATHS[i % len(PATHS)]]
			if i % 10 == 9:
				it["blocked_by"] = [f"LD-{i}"]  # waits on the ticket before it
		items.append(it)
	b = s.backend
	if hasattr(b, "bulk_load"):
		b.bulk_load(None, items, [], [])
	else:
		for it in items:
			b.write(it)


def single(loc: str) -> dict:
	out = {}
	empty_caches()
	s = st.Store(loc)
	ids = [t["id"] for t in s.list()]
	pick = random.Random(1).sample(ids, 20)
	for name, fn, n in (
		("list", s.list, 5),
		("get", lambda: s.get(random.choice(pick)), 20),
		("next_ready", lambda: s.next_ready(), 5),
		("needs", lambda: web._needs([("load", s, {})]), 5),
	):
		empty_caches()
		s = st.Store(loc)
		cold, _, _ = timed(fn)
		_, warm, _ = timed(fn, n)
		out[name] = {"cold_ms": cold, "warm_ms": warm}

	def claim_release():
		t = s.claim("solo-1")
		s.release(t["id"], "solo-1", "load test")
	empty_caches()
	s = st.Store(loc)
	cold, _, _ = timed(claim_release)
	_, warm, _ = timed(claim_release, 10)
	out["claim_release"] = {"cold_ms": cold, "warm_ms": warm}

	def create_delete():
		s.delete(s.create({"title": "one more"}, prefix="ONE")["id"])
	empty_caches()
	s = st.Store(loc)
	cold, _, _ = timed(create_delete)
	_, warm, _ = timed(create_delete, 5)
	out["create_delete"] = {"cold_ms": cold, "warm_ms": warm}
	return out


def swarm(loc: str, agents: int, rounds: int) -> dict:
	barrier = threading.Barrier(agents)
	rec = {a: {"claimed": [], "notes": [], "created": [], "done": [], "errors": [], "stops": []} for a in range(agents)}
	lat = {k: [] for k in ("create", "claim", "heartbeat", "note", "complete")}
	stats = {"retries": 0}
	lock = threading.Lock()

	def op(kind, fn):
		t0 = time.perf_counter()
		r = retry(fn, stats)
		with lock:
			lat[kind].append(time.perf_counter() - t0)
		return r

	def agent(a: int):
		name, r = f"load-{a}", rec[a]
		try:
			s = st.Store(loc)  # its own Store (and, on a database, its own connection), like a separate worker
			barrier.wait()
			r["created"].append(op("create", lambda: s.create({"title": f"made by {name}"}, prefix="NEW", agent=name))["id"])
			for i in range(rounds):
				conflicts = 0
				while True:
					t = op("claim", lambda: s.claim_next(name))
					if t.get("ticket", 1) is not None or t.get("reason") != "conflict" or conflicts >= 5:
						break
					conflicts += 1
					time.sleep(0.2)
				if t.get("ticket", 1) is None:
					r["stops"].append(t["reason"])
					break
				tid = t["id"]
				r["claimed"].append(tid)
				op("heartbeat", lambda: s.heartbeat(tid, name, f"round {i}"))
				text = f"note {name} {tid} {i}"
				op("note", lambda: s.add_note(tid, name, text))
				r["notes"].append((tid, text))
				op("complete", lambda: s.complete(tid, name, f"done by {name}"))
				r["done"].append(tid)
		except Exception as e:  # noqa: BLE001 - counted and reported (agents_gave_up)
			r["errors"].append(f"{type(e).__name__}: {e}"[:300])
		finally:
			for name_ in ("sql", "mongo"):
				mod = sys.modules.get(f"unblockd.{name_}")
				if mod is not None and hasattr(mod, "disconnect"):
					try:
						mod.disconnect()
					except Exception:  # noqa: BLE001
						pass

	threads = [threading.Thread(target=agent, args=(a,)) for a in range(agents)]
	t0 = time.perf_counter()
	for th in threads:
		th.start()
	for th in threads:
		th.join()
	wall = time.perf_counter() - t0
	ops = sum(len(v) for v in lat.values())
	return {"rec": rec, "wall_s": round(wall, 1), "ops": ops, "ops_per_s": round(ops / wall, 1), "retries": stats["retries"],
		"latency_ms": {k: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95), "max": pct(v, 100)} for k, v in lat.items()}}


def check(loc: str, n: int, sw: dict) -> dict:
	empty_caches()
	s = st.Store(loc)
	rec = sw["rec"].values()
	problems = []
	owner = {}
	for a, r in sw["rec"].items():
		for tid in r["claimed"]:
			if tid in owner:
				problems.append(f"double claim: {tid} by load-{owner[tid]} and load-{a}")
			owner[tid] = a
	claims = {}
	for e in s.activity(limit=10 ** 7):
		if e.get("event") == "claim" and str(e.get("agent", "")).startswith("load-"):
			claims[e["ticket"]] = claims.get(e["ticket"], 0) + 1
	problems += [f"double claim in the log: {tid} claimed {k} times" for tid, k in claims.items() if k > 1]
	created = [i for r in rec for i in r["created"]]
	if len(set(created)) != len(created):
		problems.append(f"duplicate id: {len(created) - len(set(created))} created ids repeat")
	tickets = {t["id"]: t for t in s.list()}
	if len(tickets) != n + len(created):
		problems.append(f"{len(tickets)} tickets on the board, expected {n} + {len(created)}")
	problems += [f"created {i} is missing" for i in created if i not in tickets]
	for a, r in sw["rec"].items():
		for tid, text in r["notes"]:
			if not any(x.get("text") == text for x in tickets[tid].get("notes", [])):
				problems.append(f"lost note on {tid}: {text!r}")
		for tid in r["done"]:
			t = tickets[tid]
			if t["status"] != "done" or (t.get("result") or {}).get("by") != f"load-{a}":
				problems.append(f"{tid} completed by load-{a} reads {t['status']} by {(t.get('result') or {}).get('by')}")
	errors = [e for r in rec for e in r["errors"]]  # an agent that gave up (busy lock after every retry) is capacity, not a wrong answer
	gave_up = {}
	for e in errors:
		gave_up[e] = gave_up.get(e, 0) + 1
	stops = {}
	for r in rec:
		for x in r["stops"]:
			stops[x] = stops.get(x, 0) + 1
	return {"claimed": len(owner), "notes": sum(len(r["notes"]) for r in rec), "created": len(created),
		"completed": sum(len(r["done"]) for r in rec), "agents_gave_up": len(errors), "gave_up_with": gave_up, "stops": stops,
		"problems": problems[:50], "problem_count": len(problems)}


def main(argv=None) -> int:
	ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	ap.add_argument("--location", help="board location (folder or database URL#board); default a temporary folder")
	ap.add_argument("--tickets", type=int, default=20000)
	ap.add_argument("--open", type=int, default=1000, help="how many of them are todo (the rest done)")
	ap.add_argument("--agents", type=int, default=200)
	ap.add_argument("--rounds", type=int, default=2, help="tickets each agent claims and completes at most")
	ap.add_argument("--json", help="also write the report here")
	ap.add_argument("--keep", action="store_true", help="keep the board (default: a database board is dropped, a temp folder removed)")
	a = ap.parse_args(argv)
	tmp = None
	loc = a.location
	if not loc:
		tmp = tempfile.mkdtemp(prefix="unblockd-load-")
		loc = os.path.join(tmp, "tickets")
		os.makedirs(loc)
	elif not connectors.is_db(loc):
		os.makedirs(loc, exist_ok=True)
	report = {"location": connectors.describe(loc), "tickets": a.tickets, "open": a.open, "agents": a.agents, "rounds": a.rounds,
		"python": sys.version.split()[0], "platform": sys.platform}
	try:
		s = st.Store(loc)
		if s.list():
			print(f"{connectors.describe(loc)} already has tickets: use an empty board", file=sys.stderr)
			return 2
		t0 = time.perf_counter()
		seed(s, a.tickets, a.open)
		report["seed_s"] = round(time.perf_counter() - t0, 1)
		print(f"seeded {a.tickets} in {report['seed_s']} s", file=sys.stderr)
		report["single"] = single(loc)
		print(f"single: {json.dumps(report['single'])}", file=sys.stderr)
		sw = swarm(loc, a.agents, a.rounds)
		report["check"] = check(loc, a.tickets, sw)
		sw.pop("rec")
		report["swarm"] = sw
	finally:
		if not a.keep:
			if tmp:
				shutil.rmtree(tmp, ignore_errors=True)
			elif connectors.is_db(loc):
				try:
					connectors.open_backend(loc, create=False).drop()
				except TicketError:
					pass
	text = json.dumps(report, indent=2)
	print(text)
	if a.json:
		with open(a.json, "w", encoding="utf-8") as f:
			f.write(text + "\n")
	return 1 if report.get("check", {}).get("problem_count") else 0


if __name__ == "__main__":
	sys.exit(main())
