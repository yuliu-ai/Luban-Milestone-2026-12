"""Plan new fixed-table skill sequences and execute each in one continuous world."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

from luban.backends.llm.openai_compatible import (
    OpenAICompatibleLLMBackend,
)
from luban.environments.atomic.native.bullet_ground_backend import (
    GroundBackend,
    GroundEnvironment,
    GroundVerifier,
)
from luban.environments.atomic.native.bullet_ground_plant import (
    BulletGroundPlant,
)
from luban.environments.atomic.native.bullet_interaction_backend import (
    InteractionBackend,
    InteractionEnvironment,
    InteractionVerifier,
)
from luban.environments.atomic.native.bullet_manipulation_backend import (
    BulletManipulationBackend,
    BulletManipulationEnvironment,
    BulletManipulationVerifier,
)
from luban.environments.atomic.native.bullet_manipulation_plant import (
    BulletManipulationPlant,
)
from luban.environments.atomic.native.guards import AtomicSemanticGuard
from omni_atomic_skills import (
    ExecutionContext,
    JsonlEventSink,
    SkillRegistry,
    SkillRuntime,
)

ROOT = Path(__file__).resolve().parents[2]

MENUS = {
    "arm": {
        "G01": "将平行夹爪张开到78毫米，需要目标宽度大于当前宽度，也可在落座后释放工件。",
        "G03": "抓住当前夹爪之间的工件并建立附着。",
        "G09": "将已抓工件的夹持力调到任务指定值，需要有效附着。",
        "I02": "将已抓工件沿注册方向拉动。",
        "I06": "将已抓工件沿重力反方向抬升任务指定高度。",
        "I09": "把已抓工件对准任务指定目标上方的位姿。",
        "I08": "将已抓工件落座到桌面，仍保持抓持。",
    },
    "ground": {
        "M08": "完成任务中的下一段直线移动，每调用一次只执行一段。",
        "M10": "受控停止底盘，停止后本回合不再允许移动。",
    },
}


def plan_tasks(tasks, output):
    backend = OpenAICompatibleLLMBackend(
        base_url=os.environ["LUBAN_API_BASE_URL"],
        model=os.environ["LUBAN_LLM_MODEL"],
        omit_temperature=True,
        timeout_seconds=180,
    )
    try:
        with output.open("x") as stream:
            for index, task in enumerate(tasks, 1):
                prompt = (
                    "依据任务目标选择技能并排序，技能的场景输入参数已配置。"
                    '只返回JSON对象，格式为{"skills":[技能编号,...]}，最多8步。'
                    "只用给出的技能，不给解释或完成判定。\n可用技能：\n"
                    + json.dumps(MENUS[task["domain"]], ensure_ascii=False)
                    + "\n任务目标：\n"
                    + task["goal"]
                )
                started = time.monotonic()
                row = {"id": task["id"], "prompt": prompt, "skills": None}
                try:
                    response = backend.complete(
                        {
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 4096,
                            "response_format": {"type": "json_object"},
                            "extra_body": {"reasoning_effort": "medium"},
                        }
                    )
                    text = response["choices"][0]["message"]["content"]
                    row.update(
                        skills=json.loads(text)["skills"],
                        response=text,
                        usage=response.get("usage"),
                        model=response.get("model"),
                    )
                except Exception as error:
                    row["error"] = f"{type(error).__name__}: {error}"
                row["wall_seconds"] = time.monotonic() - started
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                print(
                    f"[组合规划 {index}/{len(tasks)} {task['id']}] {row['skills']}",
                    flush=True,
                )
    finally:
        backend.close()


def check_goal(task, plant, initial, steps):
    final = plant.current
    ids = [row["skill"] for row in steps]
    if task["domain"] == "ground":
        direction = np.array(
            [np.cos(initial["imu_yaw_rad"]), np.sin(initial["imu_yaw_rad"])]
        )
        expected = (
            np.array(initial["raw_pose"][:2]) + sum(task["distances_m"]) * direction
        )
        error = float(np.linalg.norm(np.array(final["raw_pose"][:2]) - expected))
        speed = float(np.linalg.norm(final["raw_velocity_mps"]))
        checks = {
            "segments": ids == ["M08"] * len(task["distances_m"]) + ["M10"],
            "position": error <= task["position_tolerance_m"],
            "stopped": speed <= 0.02,
            "stop_resource": plant.stop_gate,
            "no_collision": not any(r["external_contacts"] for r in plant.trace),
        }
        metrics = {"position_error_m": error, "speed_mps": speed}
    else:
        sequences = {
            "place_release": ["G03", "I06", "I09", "I08", "G01"],
            "force_before_lift": ["G03", "G09", "I06", "I09", "I08", "G01"],
            "force_after_align": ["G03", "I06", "I09", "G09", "I08", "G01"],
            "open_then_place": ["G01", "G03", "I06", "I09", "I08", "G01"],
            "force_after_lift": ["G03", "I06", "G09", "I09", "I08", "G01"],
        }
        remaining = iter(ids)
        ordered = all(skill in remaining for skill in sequences[task["family"]])
        initial_position = np.array(initial["object"]["pose"][:3])
        expected = initial_position + [*task["align_delta_m"][:2], 0]
        position_error = float(
            np.linalg.norm(np.array(final["object"]["pose"][:3]) - expected)
        )
        lift = (
            max(row["object"]["pose"][2] for row in plant.trace) - initial_position[2]
        )
        speed = float(np.linalg.norm(final["object"]["linear_velocity_mps"]))
        supported = any(
            contact[2] == plant.support and contact[9] > 1e-4
            for contact in final["object"]["contacts"]
        )
        released = (
            final["arms"]["right"]["width_m"] > 0.07
            and final["arms"]["right"]["gripper_contact_force_n"] < 0.1
        )
        checks = {
            "ordered_goal_operations": ordered,
            "position": position_error <= task["position_tolerance_m"],
            "lift": lift >= task["lift_m"] - 0.006,
            "supported": supported,
            "released": released,
            "stable": speed <= 0.02,
        }
        metrics = {
            "position_error_m": position_error,
            "maximum_lift_m": float(lift),
            "final_speed_mps": speed,
        }
    return {
        "passed": bool(all(checks.values())),
        "checks": {k: bool(v) for k, v in checks.items()},
        "metrics": metrics,
    }


def execute_task(task, sequence, directory):
    sys.path.insert(0, str(ROOT / "scripts/milestone/native"))
    from run_catalog_ground import inputs_for as ground_inputs
    from run_catalog_interactions import inputs_for as interaction_inputs
    from run_catalog_panda import inputs_for as hand_inputs

    known = json.loads(
        (ROOT / "configs/milestone/known_composite_sequences.json").read_text()
    )["programs"]
    allowed = MENUS[task["domain"]]
    if (
        not isinstance(sequence, list)
        or not 2 <= len(sequence) <= 8
        or any(not isinstance(s, str) or s not in allowed for s in sequence)
        or len(set(sequence)) < 2
    ):
        return {"passed": False, "reason": "invalid_generated_program", "steps": []}
    if sequence in [program["steps"] for program in known]:
        return {"passed": False, "reason": "sequence_is_in_known_catalog", "steps": []}
    registry = SkillRegistry.load_json_files(
        [ROOT / "configs/milestone/native_catalogs/v2.2.json"]
    )
    runtime = SkillRuntime(
        registry, event_sink=JsonlEventSink(directory / "events.jsonl")
    )
    runtime.register_guard("default", AtomicSemanticGuard())
    routes = {}

    def register(key, environment, backend, verifier, ids):
        runtime.register_environment(key, environment)
        for skill in ids:
            contract = registry.resolve(skill)
            runtime.register_backend(
                contract.executor.adapter_key, backend(), replace_existing=True
            )
            runtime.register_verifier(
                contract.verifier.adapter_key,
                verifier(environment),
                replace_existing=True,
            )
            routes[skill] = key

    plant = None
    steps = []
    try:
        if task["domain"] == "ground":
            plant = BulletGroundPlant(seed=task["seed"], friction=task["friction"])
            environment = GroundEnvironment(plant, directory)
            register(
                "ground", environment, GroundBackend, GroundVerifier, ["M08", "M10"]
            )
        else:
            plant = BulletManipulationPlant(
                object_between_fingers=True, coupled_fingers=True
            )
            shift = np.array([*task["layout_shift_xy_m"], 0.0])
            for body in [*plant.bodies.values(), plant.support, plant.object_body]:
                position, quaternion = plant.sim.getBasePositionAndOrientation(body)
                plant.sim.resetBasePositionAndOrientation(
                    body, (np.array(position) + shift).tolist(), quaternion
                )
            for arm in plant.base_positions:
                plant.base_positions[arm] = (
                    np.array(plant.base_positions[arm]) + shift
                ).tolist()
            plant.sim.changeDynamics(
                plant.object_body,
                -1,
                mass=0.03 * task["mass_scale"],
                lateralFriction=task["object_friction"],
            )
            plant.sim.changeDynamics(
                plant.support, -1, contactStiffness=5000, contactDamping=10
            )
            plant.support_compliance = {"stiffness_npm": 5000, "damping_ns_per_m": 10}
            for _ in range(30):
                plant.step()
            holder = BulletManipulationEnvironment(plant)
            interaction = InteractionEnvironment(holder)
            register(
                "hand",
                holder,
                BulletManipulationBackend,
                BulletManipulationVerifier,
                ["G01", "G03", "G09"],
            )
            register(
                "object",
                interaction,
                InteractionBackend,
                InteractionVerifier,
                ["I02", "I06", "I08", "I09"],
            )
        initial = plant.current
        (directory / "scenario.json").write_text(
            json.dumps(
                {"task": task, "initial": initial, "model": plant.model_identity()},
                ensure_ascii=False,
            )
        )
        segment = 0
        for index, skill in enumerate(sequence, 1):
            if task["domain"] == "ground":
                inputs = ground_inputs(skill, environment, {})
                if skill == "M08":
                    inputs["distance_m"] = task["distances_m"][segment]
                    inputs["position_tolerance_m"] = 0.005
                    segment += 1
            else:
                inputs = (
                    hand_inputs(skill, plant, holder)
                    if skill.startswith("G")
                    else interaction_inputs(skill, plant, holder, interaction, initial)
                )
                if skill == "I06":
                    inputs["height_m"] = task["lift_m"]
                elif skill == "G09":
                    inputs["target_force_n"] = task["force_n"]
                elif skill == "I09":
                    pose = initial["object"]["pose"]
                    inputs["target_object_pose"] = [
                        *(np.array(pose[:3]) + task["align_delta_m"]).tolist(),
                        *pose[3:],
                    ]
            contract = registry.resolve(skill)
            result = runtime.execute(
                skill,
                inputs,
                environment_key=routes[skill],
                context=ExecutionContext(
                    episode_id=task["id"],
                    capabilities=frozenset(contract.requires_capabilities),
                ),
            ).to_dict()
            row = {"skill": skill, "inputs": inputs, "result": result}
            steps.append(row)
            (directory / f"step-{index}-{skill}.json").write_text(
                json.dumps(row, ensure_ascii=False)
            )
            print(
                f"  [{task['id']} 步骤 {index}/{len(sequence)} {skill}] "
                f"{'成功' if result['status'] == 'succeeded' else '失败'}",
                flush=True,
            )
            if result["status"] != "succeeded":
                return {
                    "passed": False,
                    "reason": "atomic_skill_failed",
                    "failed_skill": skill,
                    "verification": result.get("verification"),
                    "steps": [s["skill"] for s in steps],
                }
            if task["domain"] == "arm" and skill == "G03":
                interaction.calibrate_attachment()
        verdict = check_goal(task, plant, initial, steps)
        return {
            "passed": verdict["passed"],
            "goal_verification": verdict,
            "steps": sequence,
            "new_sequence": True,
        }
    finally:
        if plant is not None:
            (directory / "physics.json").write_text(
                json.dumps({"trace": plant.trace, "commands": plant.command_trace})
            )
            plant.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "run"])
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "configs/milestone/unseen30_tasks.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plans", type=Path)
    parser.add_argument("--task-id", action="append")
    args = parser.parse_args()
    tasks = json.loads(args.manifest.read_text())["tasks"]
    if args.task_id:
        tasks = [task for task in tasks if task["id"] in args.task_id]
    if args.command == "plan":
        plan_tasks(tasks, args.output)
        return
    plans = {
        row["id"]: row for row in map(json.loads, args.plans.read_text().splitlines())
    }
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for index, task in enumerate(tasks, 1):
        print(f"[新组合任务 {index}/{len(tasks)} {task['id']}] 开始", flush=True)
        directory = args.output / task["id"]
        directory.mkdir()
        started = time.monotonic()
        try:
            row = execute_task(task, plans[task["id"]]["skills"], directory)
        except Exception as error:
            row = {
                "passed": False,
                "reason": "execution_error",
                "error": f"{type(error).__name__}: {error}",
            }
        row.update(
            id=task["id"],
            domain=task["domain"],
            wall_seconds=time.monotonic() - started,
        )
        (directory / "result.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2)
        )
        rows.append(row)
        print(
            f"[新组合任务 {task['id']}] {'成功' if row['passed'] else '失败'}",
            flush=True,
        )
        (args.output / "results.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2)
        )
    passed = sum(row["passed"] for row in rows)
    summary = {
        "total": len(rows),
        "passed": passed,
        "rate": passed / len(rows),
        "zero_shot_threshold_met": passed / len(rows) > 0.5,
        "all_tasks_successful": passed == len(rows),
        "tasks": rows,
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    print(
        f"[组合汇总] {passed}/{len(rows)} 成功，通过率 {passed / len(rows):.2%}",
        flush=True,
    )
    raise SystemExit(0 if summary["all_tasks_successful"] else 1)


if __name__ == "__main__":
    main()
