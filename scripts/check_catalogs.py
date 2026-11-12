"""Run bundled catalog and example validators without rewriting source fixtures."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    scratch = ROOT / ".release"
    scratch.mkdir(exist_ok=True)
    reports = []
    with tempfile.TemporaryDirectory(prefix="catalog-check-", dir=scratch) as folder:
        stage = Path(folder)
        shutil.copytree(ROOT / "catalogs", stage / "catalogs")
        for version in ("v2.1", "v2.2"):
            for relative in (
                "validate_catalog.py",
                "skill_bundles/validate_bundles.py",
            ):
                script = stage / "catalogs" / version / relative
                result = subprocess.run(
                    [sys.executable, str(script)],
                    cwd=script.parent,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                report = json.loads(
                    (script.parent / "validation_report.json").read_text()
                )
                reports.append({"validator": f"{version}/{relative}", **report})
                print(f"{version}/{relative}: {result.returncode}")
                print(result.stdout.strip())
                if result.returncode:
                    print(result.stderr)
                    return result.returncode
    output = ROOT / "runs" / "validation"
    output.mkdir(parents=True, exist_ok=True)
    (output / "catalogs.json").write_text(json.dumps(reports, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
