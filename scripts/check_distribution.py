"""Check wheel and source-archive installation in independent Python environments."""

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    wheels = list((ROOT / "dist").glob("*.whl"))
    archives = list((ROOT / "dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(archives) != 1:
        raise SystemExit("Build one wheel and one source archive with uv build first")
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"):
        env.pop(name, None)
    if "UV_CACHE_DIR" in env:
        env["UV_CACHE_DIR"] = str(Path(env["UV_CACHE_DIR"]).resolve())
    env["GRADIO_ANALYTICS_ENABLED"] = "False"
    env["HF_HUB_OFFLINE"] = "1"
    reports = ROOT / "runs" / "validation"
    reports.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / ".release"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="distribution-check-", dir=scratch
    ) as folder:
        stage = Path(folder).resolve()
        wheel_env = stage / "wheel-env"
        subprocess.run(
            ["uv", "venv", str(wheel_env), "--python", sys.executable],
            env=env,
            check=True,
        )
        python = wheel_env / "bin" / "python"
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                str(wheels[0].resolve()) + "[chemistry_bench]",
            ],
            env=env,
            check=True,
        )
        smoke = "\n".join(
            [
                "from pathlib import Path",
                "from importlib.resources import files",
                "import luban, omni_atomic_skills",
                "from importlib.metadata import distribution",
                "assert distribution('luban').metadata['Name'] == 'luban'",
                "from chemistry_bench_proto import embodiment_pb2, embodiment_pb2_grpc",
                "assert 'site-packages' in str(Path(luban.__file__).resolve())",
                "assert files('luban').joinpath('py.typed').is_file()",
                "assert files('omni_atomic_skills').joinpath('py.typed').is_file()",
                "from luban.environments.benchmarks import chemistry_bench",
                "resource = files(chemistry_bench)",
                "assert any(p.name.endswith('.yaml') for p in resource.iterdir())",
                "print('Independent wheel imports and packaged resources passed')",
            ]
        )
        subprocess.run([str(python), "-c", smoke], cwd=stage, env=env, check=True)
        subprocess.run(
            [str(python), "-m", "luban", "--help"],
            cwd=stage,
            env=env,
            check=True,
        )
        subprocess.run(
            [
                str(python),
                "-m",
                "luban.examples.offline",
                "--output-dir",
                str(stage / "wheel-demo"),
            ],
            cwd=stage,
            env=env,
            check=True,
        )
        subprocess.run(
            [str(python.parent / "luban"), "--help"],
            cwd=stage,
            env=env,
            check=True,
        )
        source = stage / "source"
        source.mkdir()
        with tarfile.open(archives[0]) as archive:
            archive.extractall(source, filter="data")
        extracted = next(p for p in source.iterdir() if p.is_dir())
        assert not (extracted / ".git").exists()
        for relative in (
            "README.md",
            "README.zh-CN.md",
            "docs/README.zh-CN.md",
            "docs/installation.zh-CN.md",
            "docs/configuration.zh-CN.md",
            "docs/models.zh-CN.md",
            "CODE_OF_CONDUCT.md",
            "CONTRIBUTING.md",
            "LICENSE",
            "NOTICE",
            "THIRD_PARTY_NOTICES.md",
            "SECURITY.md",
            "docs/assets/episode-loop.svg",
            "docs/assets/luban-wordmark.svg",
            "docs/assets/luban-pillars.svg",
            "docs/assets/luban-framework.svg",
            "docs/assets/liutong-framework.svg",
            "docs/assets/liutong-wordmark.svg",
            "docs/assets/liutong-wordmark-dark.svg",
            "docs/assets/spatial-scene-topview.png",
            "docs/reported-results.md",
            "docs/reader-console.md",
            "docs/assets/luban-wordmark-dark.svg",
            "docs/project-page.md",
            "requirements/robocasa-runtime.txt",
            "scripts/check_gpu_setup.py",
            "configs/runs/offline.yaml",
        ):
            assert (extracted / relative).is_file(), f"Source archive lacks {relative}"
        subprocess.run(
            [
                "uv",
                "sync",
                "--frozen",
                "--all-extras",
                "--group",
                "dev",
                "--group",
                "docs",
                "--group",
                "full-test",
                "--python",
                sys.executable,
            ],
            cwd=extracted,
            env=env,
            check=True,
        )
        archive_python = extracted / ".venv" / "bin" / "python"
        junit = reports / "source-archive-tests.xml"
        subprocess.run(
            [str(archive_python), "-m", "pytest", "-ra", "--junitxml=" + str(junit)],
            cwd=extracted,
            env=env,
            check=True,
        )
        skipped = len(ET.parse(junit).getroot().findall(".//testcase/skipped"))
        if skipped:
            raise SystemExit(f"Source archive full test suite skipped {skipped} checks")
        for command in [
            [str(archive_python), "scripts/check_docs.py"],
            [str(archive_python), "scripts/check_catalogs.py"],
            [
                str(archive_python),
                "-m",
                "luban",
                "run",
                "--config",
                "configs/runs/offline.yaml",
            ],
        ]:
            subprocess.run(command, cwd=extracted, env=env, check=True)
    (reports / "distributions.json").write_text(
        json.dumps(
            {
                "wheel": wheels[0].name,
                "source_archive": archives[0].name,
                "wheel_install": "passed",
                "outside_checkout_example": "passed",
                "source_archive_full_tests": "passed",
                "source_archive_skipped": 0,
            },
            indent=2,
        )
        + "\n"
    )
    print("Wheel and source-archive independent installation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
