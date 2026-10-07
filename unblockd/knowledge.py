"""Knowledge attached to tickets, so agents get their context instead of being re-told it.

Two kinds, both resolved by `context(store, ticket)`:
- Linked docs: paths in the project repo (the tickets folder's parent), listed in `docs` on the
  board config, on an agent profile, or on a ticket. A path, a folder (its text files) or a glob.
  Links, not copies: they stay current with the repo. Markdown links and `@imports` between docs
  are reported as `links`, the edges of the knowledge graph (not followed automatically).
- Attached files: uploaded into tickets/files/<ID>/<name>, committed with the ticket.

Only text files are read; paths can't leave the project folder.
"""
from __future__ import annotations

import re
from pathlib import Path

from .store import TicketError

TEXT_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".py", ".gd",
            ".js", ".ts", ".html", ".css", ".sh", ".ps1", ".xml", ".tscn", ".tres", ".gdshader", ""}
MAX_FILE = 300_000      # bytes: bigger files are listed, never inlined or uploaded
MAX_FOLDER = 40         # files taken from one folder/glob entry
BUDGET = 60_000         # characters inlined per context bundle; the rest is listed for read_doc
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
LINK_RE = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)|^@(\S+)", re.M)


def root(store) -> Path:
	return store.dir.resolve().parent


def _inside(base: Path, rel: str) -> Path:
	if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
		raise TicketError(f"bad path {rel!r}")
	p = (base / rel.strip().replace("\\", "/").lstrip("/")).resolve()
	if p != base and base not in p.parents:
		raise TicketError(f"{rel!r} is outside the project folder")
	return p


def _is_text(p: Path) -> bool:
	return p.suffix.lower() in TEXT_EXT and p.is_file()


def expand(store, entry: str) -> list[tuple[str, Path | None, str | None]]:
	"""One docs entry -> [(display path, file or None, error or None)]."""
	base = root(store)
	if any(c in entry for c in "*?["):
		try:
			_inside(base, entry.split("*")[0] or ".")
		except TicketError as e:
			return [(entry, None, str(e))]
		hits = sorted(p for p in base.glob(entry.strip().lstrip("/")) if _is_text(p) and base in p.resolve().parents)
		return [(p.relative_to(base).as_posix(), p, None) for p in hits[:MAX_FOLDER]] or [(entry, None, "no files match")]
	try:
		p = _inside(base, entry)
	except TicketError as e:
		return [(entry, None, str(e))]
	if p.is_dir():
		hits = sorted(x for x in p.rglob("*") if _is_text(x) and ".unblockd" not in x.parts)
		return [(x.relative_to(base).as_posix(), x, None) for x in hits[:MAX_FOLDER]] or [(entry, None, "folder has no text files")]
	if not p.exists():
		return [(entry, None, "missing")]
	if not _is_text(p):
		return [(entry, None, "not a text file")]
	return [(p.relative_to(base).as_posix(), p, None)]


def links_of(text: str, here: Path, base: Path) -> list[str]:
	"""Repo-relative targets of markdown links / @imports in a doc that exist in the project."""
	out = []
	for m in LINK_RE.finditer(text):
		target = m.group(1) or m.group(2)
		if not target or "://" in target or target.startswith("mailto:"):
			continue
		try:
			p = (here.parent / target).resolve()
		except (OSError, ValueError):
			continue
		if p.exists() and (base in p.parents):
			rel = p.relative_to(base).as_posix()
			if rel not in out:
				out.append(rel)
	return out


def files_dir(store, tid: str) -> Path:
	store._path(tid)  # validates the id
	return store.dir / "files" / tid


def list_files(store, tid: str) -> list[dict]:
	d = store.dir / "files" / tid
	if not d.is_dir():
		return []
	return [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(d.iterdir()) if p.is_file()]


def attach(store, tid: str, name: str, content: str) -> dict:
	if not NAME_RE.match(name or ""):
		raise TicketError(f"bad file name {name!r}: letters, digits, . _ - (max 100)")
	if Path(name).suffix.lower() not in TEXT_EXT:
		raise TicketError(f"{name}: only text files can be attached ({', '.join(sorted(e for e in TEXT_EXT if e))})")
	if not isinstance(content, str):
		raise TicketError("content must be text")
	data = content.encode("utf-8")
	if len(data) > MAX_FILE:
		raise TicketError(f"{name} is {len(data)} bytes; the limit is {MAX_FILE}. Link it with docs instead.")
	store.get(tid)  # must exist
	d = files_dir(store, tid)
	d.mkdir(parents=True, exist_ok=True)
	(d / name).write_bytes(data)
	store._log("attach", tid, None, name)
	return {"name": name, "bytes": len(data)}


def detach(store, tid: str, name: str) -> dict:
	if not NAME_RE.match(name or ""):
		raise TicketError(f"bad file name {name!r}")
	p = files_dir(store, tid) / name
	if not p.is_file():
		raise TicketError(f"{tid} has no file {name}")
	p.unlink()
	store._log("detach", tid, None, name)
	return {"name": name, "removed": True}


def read_file(store, tid: str, name: str) -> str:
	if not NAME_RE.match(name or ""):
		raise TicketError(f"bad file name {name!r}")
	p = files_dir(store, tid) / name
	if not p.is_file():
		raise TicketError(f"{tid} has no file {name}")
	return p.read_text(encoding="utf-8", errors="replace")


def read_doc(store, path: str) -> dict:
	base = root(store)
	p = _inside(base, path)
	if not p.is_file():
		raise TicketError(f"no file {path} in the project")
	if not _is_text(p):
		raise TicketError(f"{path} is not a text file")
	if p.stat().st_size > MAX_FILE:
		raise TicketError(f"{path} is too big to read here ({p.stat().st_size} bytes)")
	text = p.read_text(encoding="utf-8", errors="replace")
	return {"path": p.relative_to(base).as_posix(), "content": text, "links": links_of(text, p, base)}


def sources(store, t: dict) -> list[tuple[str, str]]:
	"""(docs entry, where it came from) for a ticket: board, its agent profile, the ticket."""
	out = [(e, "board") for e in store.config.get("docs", [])]
	prof = next((a for a in store.config["agents"] if a["name"] == t.get("agent")), None)
	if prof:
		out += [(e, f"agent:{prof['name']}") for e in prof.get("docs", [])]
	out += [(e, "ticket") for e in t.get("docs", [])]
	return out


def with_context(store, t: dict | None) -> dict | None:
	"""A freshly claimed ticket plus its context bundle (None passes through): what a worker reads first."""
	return {**t, "context": context(store, t["id"])} if t else None


def context(store, tid: str, budget: int | None = None, inline: bool = True) -> dict:
	"""Everything an agent should read before working a ticket, in order: board docs, agent
	profile docs, ticket docs, then files attached to the ticket. Inlined until `budget`
	characters; the rest is listed (call read_doc). Missing or bad entries are listed with an error."""
	if budget is None:
		budget = int(store.config.get("context_budget") or BUDGET)
	t = store.get(tid)
	base = root(store)
	docs, seen, left = [], set(), budget
	for entry, src in sources(store, t):
		for rel, p, err in expand(store, entry):
			if rel in seen:
				continue
			seen.add(rel)
			item = {"path": rel, "source": src}
			if err:
				item["error"] = err
			else:
				size = p.stat().st_size
				item["chars"] = size
				if size > MAX_FILE:
					item["error"] = "too big to inline; read it in parts in the repo"
				else:
					text = p.read_text(encoding="utf-8", errors="replace")
					item["links"] = links_of(text, p, base)
					if inline and len(text) <= left:
						item["content"], left = text, left - len(text)
			docs.append(item)
	for f in list_files(store, tid):
		item = {"path": f"tickets/files/{tid}/{f['name']}", "file": f["name"], "source": "attached", "chars": f["bytes"]}
		if inline and f["bytes"] <= left:
			item["content"] = read_file(store, tid, f["name"])
			left -= len(item["content"])
		docs.append(item)
	return {"ticket": tid, "docs": docs, "inlined_chars": budget - left,
	        "note": "Read these before working the ticket. Items without content: read_doc(path)."}
