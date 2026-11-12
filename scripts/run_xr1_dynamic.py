"""Dynamically dispatch XR-1 paired episodes across evaluation workers."""

import argparse
import json
import sqlite3
from pathlib import Path

from run_xr1_dual import run_pair

p = argparse.ArgumentParser()
p.add_argument("--database", type=Path, required=True)
p.add_argument("--output-dir", type=Path, required=True)
p.add_argument("--gpu", type=int, required=True)
p.add_argument("--port", type=int, required=True)
p.add_argument("--branches", type=int, choices=(1, 2), default=2)
p.add_argument("--simulator-info", action="store_true")
args = p.parse_args()
connection = sqlite3.connect(args.database, timeout=60, isolation_level=None)
while True:
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(
        "SELECT id, case_json FROM episodes WHERE status='pending' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        connection.execute("COMMIT")
        break
    connection.execute("UPDATE episodes SET status='running' WHERE id=?", (row[0],))
    connection.execute("COMMIT")
    case = json.loads(row[1])
    result = run_pair(
        case, args.output_dir, args.port, args.gpu, args.branches, args.simulator_info
    )
    connection.execute(
        "UPDATE episodes SET status=? WHERE id=?", (result["status"], row[0])
    )
    print(
        json.dumps(
            {
                "task": case["task"],
                "episode": case["episode"],
                "status": result["status"],
            }
        ),
        flush=True,
    )
