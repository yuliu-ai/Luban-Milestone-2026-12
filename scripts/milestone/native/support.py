"""Result storage and actual invocation counting for native skill trials."""

import fcntl
import json
from pathlib import Path
from typing import Any


def save_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def reserve_skill_call(path: Path) -> int:
    with path.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        ledger = json.load(stream)
        if ledger["used"] >= ledger["limit"]:
            raise ValueError("Actual skill invocation budget exhausted")
        ledger["used"] += 1
        stream.seek(0)
        stream.truncate()
        json.dump(ledger, stream, indent=2)
        stream.write("\n")
        stream.flush()
        return ledger["used"]
