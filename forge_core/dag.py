"""DAG decision logic — pure, offline-testable.

The premature-verification bug (runs #30/#34/#35/#36) was a DAG semantic
error: `verify` was wired to fire after slot-6 via `always()`, regardless
of what INDEX.json said. The build was classification=sliced (incomplete),
yet the gate ran, restored out/, and died on "no ROM zip" — a confusing
red herring that cost 30 minutes of restore every time.

Contract (forge.yml implements exactly this):
  * verify/publish run ONLY when INDEX[target].done == true
    (done is set by cmd_slice only when a valid ROM zip exists).
  * when NOT done, the conveyor decides:
      - classification 'sliced'  -> re-dispatch the workflow (next run
        resumes at the banked state; slots are idempotent and early-exit
        once done). Unlimited 350-min job walls, ~6 slices per run.
      - classification 'capacity'-> RED. Refusing to loop is the fix for
        the storage deadlock: a slice that died on disk pressure will die
        identically next time; burning 30 min/loop forever is the old bug.
      - classification 'error'   -> RED (needs human triage of the
        forensics log).
      - slice count >= cap       -> RED (budget exhausted honestly).
"""
from __future__ import annotations

from typing import Dict, List, Optional

DEFAULT_MAX_SLICES = 24
PHASES = ("slice", "verify", "fail")


def next_action(target: Dict, max_slices: int = DEFAULT_MAX_SLICES) -> Dict[str, str]:
    """target: an INDEX targets[key] record. Returns {phase, reason}."""
    done = bool(target.get("done"))
    cls = str(target.get("last_classification", "") or "")
    slice_n = int(target.get("slice", 0) or 0)

    if done:
        return {"phase": "verify",
                "reason": "INDEX done=true — run the 14-point hard gate"}
    if cls == "capacity":
        return {"phase": "fail",
                "reason": "last slice stopped on DISK CAPACITY — refusing to "
                          "re-dispatch (storage deadlock guard); grow the "
                          "volume or prune the working set"}
    if cls == "error":
        return {"phase": "fail",
                "reason": "last slice ended classification=error — triage "
                          "the forensics log before continuing"}
    if cls == "done":
        # rc==0 but no ROM zip was recorded — treat as an error, not success
        return {"phase": "fail",
                "reason": "INDEX says done but no ROM zip was banked — "
                          "profile/target mismatch"}
    if slice_n >= max_slices:
        return {"phase": "fail",
                "reason": f"slice budget exhausted ({slice_n}/{max_slices}) "
                          f"without done — raise max_slices or inspect "
                          f"PROGRESS: lines"}
    return {"phase": "slice",
            "reason": f"resume: dispatch next run at slice {slice_n + 1} "
                      f"(last classification: {cls or 'cold'})"}


def finalize_classification(classification: str, rom_zip: Optional[object],
                             stop_reason: str = "") -> Dict[str, str]:
    """The done-requires-zip rule (the second half of the premature-
    verification bug): engine rc==0 is NOT sufficient — `done` is only
    real when a flashable zip actually materialized in the product dir."""
    if classification == "done" and not rom_zip:
        return {"classification": "error",
                "reason": "build exited 0 but produced no ROM zip "
                          "(target override or packaging profile issue)"}
    return {"classification": classification, "reason": stop_reason}


def mining_matrix(mining: bool = True, candidates: int = 20,
                  cap: int = 24) -> List[str]:
    """Matrix fan-out list for slot jobs. 1 row when mining is off."""
    if not mining:
        return ["solo"]
    n = max(1, min(int(candidates), cap))
    return [f"c{i:02d}" for i in range(1, n + 1)]


def lock_tag(key: str, run_id: str, slot: str) -> str:
    """Lock release tag for one (run, slot) pair. Flat name: FsStore's
    list_tags() only scans one level."""
    return f"lock-{key}-r{run_id}-s{slot}"
