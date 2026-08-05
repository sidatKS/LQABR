"""Persisted run state — the bridge from step 7 to step 8.

Step 7 sends one email per lead, tagged with the correlation token, and
"persisted run state mapping object_id + run_id to that lead and its
message IDs". Step 8 receives an engagement event hours or days later,
carrying that same token, and has to resolve it back to a specific lead
and message. This module is that mapping.

Layout deviation, called out deliberately: the v2 tree lists
outreach.py / events.py / enums.py / observability.py under `src/`. Run
state is written by outreach (step 7) and read by events (step 8), so
putting it in either would make one import the other. It lives in its own
module instead — the tree in the doc is marked "proposed … to be confirmed
before code changes", and this is the confirmation ask.

Storage: one JSON file per run under ``LQABR_EMAIL_RUNSTATE_DIR``, written
atomically and fsynced.

  *** DEPLOYMENT: VM WITH A PERSISTENT DISK ***

  This store is a plain directory of JSON files, held by the agent host
  itself. That is a deliberate choice, and it is correct ONLY on a host
  whose filesystem is real and durable — a VM (or GKE with a
  PersistentVolume), not a serverless container.

  Why the host matters: `opened` and `clicked` events legitimately arrive
  DAYS after the send. The state written at step 7 has to still be on disk
  when step 8 asks for it. On Cloud Run the writable filesystem is
  in-memory and dies with the instance — on any restart, new revision, or
  scale-to-zero — so a run's state would be gone long before its events
  came back. On a VM with a persistent disk the same code is sound,
  because the disk outlives the process.

  Two things the VM deployment must get right:

  1. PUT THE DIRECTORY OUTSIDE THE CODE TREE. A deploy that replaces the
     checkout (git pull, a new release directory, a fresh rsync) would
     take `agents/email/.runstate` with it and silently orphan every
     in-flight run. Set LQABR_EMAIL_RUNSTATE_DIR to something like
     /var/lib/lqabr/email/runstate, on the persistent disk, owned by the
     service account the agent runs as. The repo-local default below is
     for local development only.

  2. THE AGENT AND THE WEBHOOK ARE TWO PROCESSES. entrypoint.sh starts
     either `adk api_server` (steps 3-7) or `uvicorn service_app` (steps
     8-9) — never both. On one VM they share this directory, so a lock
     that is only good inside one process is not enough: the webhook
     applying a `clicked` event and the agent recording another lead's
     send would read-modify-write the same file and one update would
     vanish. Every mutation below therefore takes an flock on the run's
     lock file, which the kernel honours ACROSS processes, in addition to
     the in-process lock that keeps threads within one process honest.

  `RunStateStore` remains the seam. If this ever does need to move to a
  network store, swap the class — the four methods below are the whole
  contract, and no caller touches a path.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

try:                                    # POSIX only; absent on Windows dev boxes
    import fcntl
except ImportError:                     # pragma: no cover - non-POSIX fallback
    fcntl = None                        # type: ignore[assignment]

#: Where a VM install should keep state: on the persistent disk, outside the
#: code tree, so a redeploy cannot delete in-flight runs.
SYSTEM_DIR = Path("/var/lib/lqabr/email/runstate")

#: Local-development fallback. Inside the checkout, and git-ignored.
REPO_DIR = Path(__file__).resolve().parents[1] / ".runstate"


def default_dir() -> Path:
    """Resolve the state directory.

    ``LQABR_EMAIL_RUNSTATE_DIR`` wins outright — that is what the VM unit
    file sets. Otherwise use the system directory if it has already been
    provisioned, and fall back to the repo-local one for local dev."""
    configured = os.environ.get("LQABR_EMAIL_RUNSTATE_DIR")
    if configured:
        return Path(configured)
    if SYSTEM_DIR.is_dir():
        return SYSTEM_DIR
    return REPO_DIR


DEFAULT_DIR = REPO_DIR      # kept for callers that imported it by name


class RunStateError(RuntimeError):
    """The state directory is unusable. Fail the run rather than send email
    whose engagement events could never be attributed to a lead."""


@dataclass
class LeadRunRecord:
    """What one lead's send looks like in run state."""

    object_id: str
    email: str
    message_id: str = ""
    skill: str = ""
    subject: str = ""
    sent_at: str = ""
    status: Optional[str] = None          # resolved MailgunEvent value, if any
    delivered: bool = False               # has a `delivered` event landed?
    clicked: bool = False
    campaign_complete: bool = False
    terminal: bool = False
    events: list = field(default_factory=list)   # append-only arrival trail

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "LeadRunRecord":
        """Rebuild a record from persisted JSON, tolerant of older schemas.

        Run state is long-lived by design — an `opened`/`clicked` event can
        arrive days after the send, so a file on disk may predate a field
        rename. A blunt ``LeadRunRecord(**raw)`` then dies with
        ``unexpected keyword argument`` on the stale key and the whole run
        becomes unreadable forever. This maps the legacy ``contact_id`` name
        to today's ``object_id`` and drops any other unknown key, so an old
        run still resolves. The next write-back persists the current schema,
        so the file self-heals on first use."""
        data = dict(raw)
        legacy_object_id = data.pop("contact_id", None)   # renamed -> object_id
        if legacy_object_id is not None and not data.get("object_id"):
            data["object_id"] = legacy_object_id
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class RunStateStore:
    """Reads and writes the object_id + run_id -> leads mapping."""

    def __init__(self, directory: Optional[os.PathLike] = None) -> None:
        self._dir = Path(directory) if directory is not None else default_dir()
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        return self._dir

    # ------------------------------------------------------------- plumbing
    def _path(self, object_id: str, run_id: str) -> Path:
        safe = f"{_slug(object_id)}__{_slug(run_id)}.json"
        return self._dir / safe

    @contextmanager
    def _exclusive(self, object_id: str, run_id: str) -> Iterator[None]:
        """Hold both locks for one read-modify-write.

        The threading lock covers threads inside this process; the flock
        covers the OTHER process — agent and webhook run separately and
        share this directory on a VM. Without the flock, one of two
        concurrent updates to the same run file is silently lost."""
        with self._lock:
            if fcntl is None:                       # pragma: no cover
                yield
                return
            self._dir.mkdir(parents=True, exist_ok=True)
            lock_path = self._path(object_id, run_id).with_suffix(".lock")
            with lock_path.open("a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self, object_id: str, run_id: str) -> Dict[str, Any]:
        path = self._path(object_id, run_id)
        if not path.exists():
            return {"object_id": object_id, "run_id": run_id,
                    "started_at": _now(), "leads": {}, "messages": {}}
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _write(self, state: Dict[str, Any]) -> None:
        """Atomic replace, fsynced.

        The rename is atomic, but on a VM a hard reboot or power loss can
        still lose data the kernel had only buffered. fsync on the file,
        then on the directory, is what makes the write survive that — and
        losing a send's state means its engagement events can never be
        attributed to a lead."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(state["object_id"], state["run_id"])
        # Unique per process: two processes sharing one temp name would
        # rename it out from under each other. The flock already serialises
        # this, but a colliding scratch file is a sharp edge worth removing.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        _fsync_dir(self._dir)

    # -------------------------------------------------------------- preflight
    def ensure_writable(self) -> Path:
        """Prove the directory works BEFORE any email goes out.

        Called at run start (step 3). A misconfigured or unwritable path
        must stop the run here — not at step 7, and certainly not silently
        at step 8 days later when an event arrives and there is nothing to
        resolve it against."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            probe = self._dir / ".writable-probe"
            probe.write_text(_now(), encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise RunStateError(
                f"run-state directory {self._dir} is not writable: {exc}. "
                "Engagement events arrive days after a send and are resolved "
                "from this directory, so a run must not start without it. "
                "Set LQABR_EMAIL_RUNSTATE_DIR to a path on the persistent disk."
            ) from exc
        return self._dir

    # ------------------------------------------------------------- step 7
    def record_send(self, object_id: str, run_id: str, record: LeadRunRecord) -> LeadRunRecord:
        """Persist one lead's send so a late event can still find it."""
        with self._exclusive(object_id, run_id):
            state = self._read(object_id, run_id)
            record.sent_at = record.sent_at or _now()
            state["leads"][record.object_id] = record.to_dict()
            if record.message_id:
                state["messages"][record.message_id] = record.object_id
            self._write(state)
        return record

    # ------------------------------------------------------------- step 8
    def resolve(self, object_id: str, run_id: str, *, message_id: str = "",
                lead_object_id: str = "") -> Optional[LeadRunRecord]:
        """Find the lead an incoming event belongs to.

        Returns None when the token matches no run state — the design notes
        that case has no path today, so the caller must flag it loudly
        rather than guess a lead."""
        with self._exclusive(object_id, run_id):
            path = self._path(object_id, run_id)
            if not path.exists():
                return None
            state = self._read(object_id, run_id)

        resolved_object = lead_object_id
        if not resolved_object and message_id:
            resolved_object = state.get("messages", {}).get(message_id, "")
        if not resolved_object:
            leads = state.get("leads", {})
            if len(leads) == 1:
                # A run that sent to exactly one lead is unambiguous even
                # when Mailgun echoes back a message id we never stored.
                resolved_object = next(iter(leads))
        if not resolved_object:
            return None

        raw = state.get("leads", {}).get(resolved_object)
        return LeadRunRecord.from_raw(raw) if raw else None

    def update(self, object_id: str, run_id: str, record: LeadRunRecord) -> LeadRunRecord:
        """Write back a record after an event has been applied."""
        with self._exclusive(object_id, run_id):
            state = self._read(object_id, run_id)
            state["leads"][record.object_id] = record.to_dict()
            if record.message_id:
                state["messages"][record.message_id] = record.object_id
            self._write(state)
        return record

    # -------------------------------------------------------------- reading
    def summary(self, object_id: str, run_id: str) -> Dict[str, Any]:
        with self._exclusive(object_id, run_id):
            return self._read(object_id, run_id)

    def runs_for_object(self, object_id: str) -> list:
        """Every ``(object_id, run_id)`` whose run state holds this lead.

        Lets a caller that has only the lead's object id — not a run id — pull
        that lead's latest Mailgun status: find the runs it was sent in here,
        then fetch each. Read-only and best-effort: an unreadable or partial
        state file is skipped rather than failing a status check."""
        object_id = str(object_id)
        found: list = []
        if not self._dir.is_dir():
            return found
        for path in sorted(self._dir.glob("*.json")):
            try:
                with path.open(encoding="utf-8") as handle:
                    state = json.load(handle)
            except (OSError, ValueError):        # ValueError covers JSONDecodeError
                continue
            if str(object_id) in (state.get("leads") or {}):
                run_object_id = state.get("object_id")
                run_id = state.get("run_id")
                if run_object_id and run_id:
                    found.append((run_object_id, run_id))
        return found


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so the rename itself is durable, not just the file."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:                              # pragma: no cover
        return
    try:
        os.fsync(fd)
    except OSError:                              # pragma: no cover - not all FS support it
        pass
    finally:
        os.close(fd)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(value))[:120]
