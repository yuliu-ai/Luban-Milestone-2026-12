import importlib
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from luban.environments.base import Environment
from luban.exceptions import ActionRangeError, ConfigError, EnvironmentError
from luban.serialization import to_jsonable

_ACTION_DIMS = {
    "action.end_effector_position": 3,
    "action.end_effector_rotation": 3,
    "action.gripper_close": 1,
    "action.base_motion": 4,
    "action.control_mode": 1,
}
_ACTION_RANGE_TOLERANCE = 0.05


def _git_metadata(path: Path) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tracked_status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return {"commit": commit, "tracked_dirty": bool(tracked_status.strip())}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "tracked_dirty": None}


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


class RoboCasaEnvironment(Environment):
    def __init__(
        self,
        robocasa_root: str | Path | None = None,
        enable_render: bool = True,
        render_gpu_device_id: int = 0,
        camera_height: int = 256,
        camera_width: int = 256,
        available_skills: list[str] | None = None,
        action_range_policy: str = "reject",
        expose_privileged_object_state: bool = False,
        expose_simulator_state: bool = False,
        environment_factory: Callable[..., Any] | None = None,
    ) -> None:
        if available_skills is not None and (
            not isinstance(available_skills, list)
            or not available_skills
            or any(
                not isinstance(skill, str) or not skill for skill in available_skills
            )
            or len(set(available_skills)) != len(available_skills)
        ):
            raise ConfigError(
                "available_skills must be a non-empty list of unique strings"
            )
        if action_range_policy not in {"reject", "official_controller_clip"}:
            raise ValueError(
                "action_range_policy must be 'reject' or 'official_controller_clip'"
            )
        if not isinstance(expose_privileged_object_state, bool):
            raise ValueError("expose_privileged_object_state must be a boolean")
        root = robocasa_root or os.environ.get(
            "LUBAN_ROBOCASA_ROOT", "benchmarks/RoboCasa"
        )
        self.robocasa_root = Path(root).expanduser().resolve()
        self.enable_render = enable_render
        self.render_gpu_device_id = render_gpu_device_id
        self.camera_height = camera_height
        self.camera_width = camera_width
        self.available_skills = (
            list(available_skills) if available_skills is not None else None
        )
        self.action_range_policy = action_range_policy
        self.expose_privileged_object_state = expose_privileged_object_state
        self.expose_simulator_state = expose_simulator_state
        self._simulator_scene: dict[str, Any] | None = None
        self.environment_factory = environment_factory
        self.env: Any = None
        self.task_name: str | None = None
        self.episode_index = 0
        self.task_seed = 0
        self.max_episode_steps = 0
        self.environment_steps = 0
        self._robocasa: Any = None
        self._render_guard_counters: dict[str, int] | None = None
        self._action_context: dict[str, Any] = {}

    def reset(self, task: Any) -> dict[str, Any]:
        if not isinstance(task, Mapping):
            raise EnvironmentError("RoboCasa task must be a mapping")
        task_name = task.get("name")
        split = task.get("split")
        if not isinstance(task_name, str) or not task_name:
            raise EnvironmentError("RoboCasa task requires a non-empty name")
        if split not in {"pretrain", "target"}:
            raise EnvironmentError("RoboCasa split must be 'pretrain' or 'target'")
        seed = task.get("seed", 0)
        if not isinstance(seed, int):
            raise EnvironmentError("RoboCasa task seed must be an integer")

        self.close()
        self._load_sdk()
        try:
            self._ensure_render_guard()
            if self.environment_factory is not None:
                self.env = self.environment_factory(
                    task_name=task_name,
                    split=split,
                    enable_render=self.enable_render,
                    render_gpu_device_id=self.render_gpu_device_id,
                    camera_height=self.camera_height,
                    camera_width=self.camera_width,
                )
            else:
                gym = importlib.import_module("gymnasium")
                self.env = gym.make(
                    f"robocasa/{task_name}",
                    disable_env_checker=True,
                    split=split,
                    enable_render=self.enable_render,
                    render_gpu_device_id=self.render_gpu_device_id,
                    camera_heights=self.camera_height,
                    camera_widths=self.camera_width,
                )
            self._ensure_render_guard()
            observation, _ = self.env.reset(seed=seed)
        except Exception as error:
            self.close()
            raise EnvironmentError(
                f"Failed to reset RoboCasa task {task_name!r}: {error}"
            ) from error

        self.task_name = task_name
        self.episode_index = int(task.get("episode_index", 0))
        self.task_seed = seed
        self.max_episode_steps = int(
            task.get("horizon") or self.get_task_horizon(task_name)
        )
        self.environment_steps = 0
        self._action_context = {
            "task": task_name,
            "episode": self.episode_index,
            "seed": seed,
        }
        decorated = self._decorate_observation(observation)
        if self.expose_simulator_state:
            decorated["simulator_object_state"] = self.simulator_state()
        return decorated

    def set_action_context(self, context: Mapping[str, Any] | None) -> None:
        if context is None:
            self._action_context = {}
            return
        if not isinstance(context, Mapping):
            raise TypeError("RoboCasa action context must be a mapping")
        self._action_context = dict(context)

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        if self.env is None or self.task_name is None:
            raise EnvironmentError("RoboCasa environment must be reset before execute")
        if not isinstance(action, Mapping):
            raise EnvironmentError("RoboCasa action must be a dictionary")
        try:
            import numpy as np
        except ImportError as error:
            raise EnvironmentError("RoboCasa environment requires NumPy") from error

        extra = sorted(set(action) - set(_ACTION_DIMS))
        missing = sorted(set(_ACTION_DIMS) - set(action))
        if missing or extra:
            raise EnvironmentError(
                f"Invalid RoboCasa action keys; missing={missing}, extra={extra}"
            )
        chunk: dict[str, Any] = {}
        horizons: set[int] = set()
        for key, width in _ACTION_DIMS.items():
            array = np.asarray(action[key])
            if array.ndim == 3 and array.shape[0] == 1:
                array = array[0]
            if array.ndim != 2 or array.shape[1] != width:
                raise EnvironmentError(
                    f"RoboCasa action {key!r} must be [T,{width}], got {array.shape}"
                )
            if (
                not np.issubdtype(array.dtype, np.floating)
                or not np.isfinite(array).all()
            ):
                raise EnvironmentError(
                    f"RoboCasa action {key!r} must contain finite floats"
                )
            horizons.add(int(array.shape[0]))
            chunk[key] = array
        if len(horizons) != 1:
            raise EnvironmentError("RoboCasa action keys must share one chunk horizon")
        chunk_horizon = next(iter(horizons), 0)
        if chunk_horizon <= 0:
            raise EnvironmentError("RoboCasa action chunk must not be empty")
        if execute_steps is not None and execute_steps <= 0:
            raise EnvironmentError("execute_steps must be positive")
        steps_to_execute = min(chunk_horizon, execute_steps or chunk_horizon)

        raw_action = np.concatenate([chunk[key] for key in _ACTION_DIMS], axis=1)
        raw_action_artifact = self._save_raw_action_artifact(raw_action)
        clip_audit = self._apply_action_range_policy(
            chunk, raw_action, raw_action_artifact
        )
        task_success = False
        done = False
        observation: Any = None
        last_info: dict[str, Any] = {}
        executed_steps = 0
        observation_sequence: list[dict[str, Any]] = []
        for step_index in range(steps_to_execute):
            step_action = {key: value[step_index] for key, value in chunk.items()}
            try:
                observation, _, terminated, truncated, info = self.env.step(step_action)
            except Exception as error:
                raise EnvironmentError(
                    "RoboCasa action execution failed at chunk step "
                    f"{step_index}: {error}"
                ) from error
            executed_steps += 1
            self.environment_steps += 1
            decorated_observation = self._decorate_observation(observation)
            observation_sequence.append(decorated_observation)
            last_info = dict(info) if isinstance(info, Mapping) else {}
            task_success = task_success or bool(last_info.get("success", False))
            done = bool(terminated or truncated)
            if self.environment_steps >= self.max_episode_steps:
                done = True
            if task_success or done:
                break

        if self.expose_simulator_state:
            observation_sequence[-1]["simulator_object_state"] = self.simulator_state()

        return {
            "observation": observation_sequence[-1],
            "observation_sequence": observation_sequence,
            "task_success": task_success,
            "task_progress": 1.0 if task_success else 0.0,
            "last_action_success": True,
            "done": done or task_success,
            "executed_steps": executed_steps,
            "environment_steps": self.environment_steps,
            "max_episode_steps": self.max_episode_steps,
            "env_info": to_jsonable(last_info),
            **clip_audit,
            "env_feedback": (
                "task_success"
                if task_success
                else "episode_horizon"
                if done
                else "action_chunk_executed"
            ),
        }

    def resolve_task_set(self, task_set: str) -> list[str]:
        self._load_sdk()
        registry_module = importlib.import_module("robocasa.utils.dataset_registry")
        registry = registry_module.TASK_SET_REGISTRY
        if task_set not in registry:
            available = ", ".join(sorted(registry))
            raise EnvironmentError(
                f"Unknown RoboCasa task_set {task_set!r}; available: {available}"
            )
        return list(dict.fromkeys(registry[task_set]))

    def get_task_horizon(self, task_name: str) -> int:
        self._load_sdk()
        module = importlib.import_module("robocasa.utils.dataset_registry_utils")
        return int(module.get_task_horizon(task_name))

    def metadata(self) -> dict[str, Any]:
        self._load_sdk()
        robocasa_git = _git_metadata(self.robocasa_root)
        robosuite = importlib.import_module("robosuite")
        robosuite_file = getattr(robosuite, "__file__", None)
        if not robosuite_file:
            raise EnvironmentError("Cannot resolve the installed robosuite module")
        robosuite_module = Path(robosuite_file).resolve()
        robosuite_root = robosuite_module.parent.parent
        return {
            "root": str(self.robocasa_root),
            "module_file": str(Path(self._robocasa.__file__).resolve()),
            **robocasa_git,
            "robosuite": {
                "root": str(robosuite_root),
                "module_file": str(robosuite_module),
                **_git_metadata(robosuite_root),
            },
            "versions": {
                "robocasa": _package_version("robocasa"),
                "robosuite": _package_version("robosuite"),
                "mujoco": _package_version("mujoco"),
            },
            "expose_privileged_object_state": self.expose_privileged_object_state,
            "rendering_guard": self.rendering_guard_metadata(),
        }

    def rendering_guard_metadata(self) -> dict[str, Any]:
        from ._rendering import RENDER_GUARD_VERSION

        return {
            "enabled": self._render_guard_counters is not None,
            "version": (
                RENDER_GUARD_VERSION
                if self._render_guard_counters is not None
                else None
            ),
            "scope": "process",
            "counters": dict(self._render_guard_counters or {}),
        }

    def _ensure_render_guard(self) -> None:
        if not self.enable_render:
            return
        if (
            self.environment_factory is not None
            and "robosuite.utils.binding_utils" not in sys.modules
        ):
            # A custom factory may use a non-MuJoCo test or environment backend.
            return
        from ._rendering import install_render_guard

        self._render_guard_counters = install_render_guard()

    def privileged_object_state(self, *, frame_index: int) -> dict[str, Any]:
        """Return an explicitly labelled MuJoCo object-state snapshot.

        This channel is useful for simulator-only diagnosis and recovery. It is
        deliberately separate from RGB observations because it is unavailable on
        a real robot without an external state-estimation system.
        """
        if not self.expose_privileged_object_state:
            raise EnvironmentError("privileged object state exposure is disabled")
        if self.env is None:
            raise EnvironmentError("RoboCasa environment is not initialized")
        raw = getattr(self.env, "unwrapped", self.env)
        sim = getattr(raw, "sim", None)
        body_ids = getattr(raw, "obj_body_id", None)
        objects = getattr(raw, "objects", {})
        if sim is None or not isinstance(body_ids, Mapping):
            raise EnvironmentError(
                "RoboCasa task does not expose MuJoCo object body identifiers"
            )
        records: list[dict[str, Any]] = []
        for logical_name, body_id_value in sorted(
            body_ids.items(), key=lambda item: str(item[0])
        ):
            body_id = int(body_id_value)
            model_object = (
                objects.get(logical_name) if isinstance(objects, Mapping) else None
            )
            model_name = getattr(model_object, "name", None) or str(logical_name)
            position = sim.data.body_xpos[body_id]
            orientation = sim.data.body_xquat[body_id]
            cvel = sim.data.cvel[body_id]
            records.append(
                {
                    "logical_name": str(logical_name),
                    "model_name": str(model_name),
                    "body_id": body_id,
                    "position_xyz": [float(value) for value in position],
                    "orientation_wxyz": [float(value) for value in orientation],
                    "linear_velocity_xyz": [float(value) for value in cvel[3:6]],
                    "angular_velocity_xyz": [float(value) for value in cvel[0:3]],
                }
            )
        return {
            "schema_version": "robocasa-mujoco-object-state-v1",
            "source_class": "simulator_privileged",
            "frame_index": int(frame_index),
            "environment_steps": int(self.environment_steps),
            "objects": records,
        }

    def simulator_state(self) -> dict[str, Any]:
        """Expose scene configuration and complete current MuJoCo kinematics."""
        import json

        raw = getattr(self.env, "unwrapped", self.env)
        model, data = raw.sim.model, raw.sim.data
        if self._simulator_scene is None:
            metadata = json.loads(
                json.dumps(
                    raw.get_ep_meta(),
                    default=lambda value: (
                        value.tolist() if hasattr(value, "tolist") else str(value)
                    ),
                )
            )
            self._simulator_scene = {
                "episode_configuration": metadata,
                "mujoco_model_xml": model.get_xml(),
            }
        return {
            "source": "privileged_simulator_state",
            "task": self.task_name,
            "episode_index": self.episode_index,
            "seed": self.task_seed,
            "environment_steps": self.environment_steps,
            "horizon": self.max_episode_steps,
            **self._simulator_scene,
            "time": float(data.time),
            "joint_names": [model.joint_id2name(i) for i in range(model.njnt)],
            "joint_qpos_addresses": model.jnt_qposadr.tolist(),
            "joint_dof_addresses": model.jnt_dofadr.tolist(),
            "qpos": data.qpos.tolist(),
            "qvel": data.qvel.tolist(),
            "actuator_controls": data.ctrl.tolist(),
            "actuator_states": data.act.tolist(),
            "joint_accelerations": data.qacc.tolist(),
            "sensor_data": data.sensordata.tolist(),
            "sensor_names": [model.sensor_id2name(i) for i in range(model.nsensor)],
            "actuator_names": [model.actuator_id2name(i) for i in range(model.nu)],
            "applied_joint_forces": data.qfrc_applied.tolist(),
            "applied_body_forces": data.xfrc_applied.tolist(),
            "external_body_forces": data.cfrc_ext.tolist(),
            "mocap_positions": data.mocap_pos.tolist(),
            "mocap_quaternions_wxyz": data.mocap_quat.tolist(),
            "user_data": data.userdata.tolist(),
            "body_names": [model.body_id2name(i) for i in range(model.nbody)],
            "body_positions": data.body_xpos.tolist(),
            "body_quaternions_wxyz": data.body_xquat.tolist(),
            "body_velocities": data.cvel.tolist(),
            "geometry_names": [model.geom_id2name(i) for i in range(model.ngeom)],
            "geometry_positions": data.geom_xpos.tolist(),
            "geometry_orientations": data.geom_xmat.tolist(),
            "geometry_sizes": model.geom_size.tolist(),
            "geometry_body_ids": model.geom_bodyid.tolist(),
            "geometry_types": model.geom_type.tolist(),
            "contacts": [
                {
                    "geom1": int(data.contact[i].geom1),
                    "geom2": int(data.contact[i].geom2),
                    "distance": float(data.contact[i].dist),
                    "position": data.contact[i].pos.tolist(),
                    "frame": data.contact[i].frame.tolist(),
                }
                for i in range(data.ncon)
            ],
            "site_names": [model.site_id2name(i) for i in range(model.nsite)],
            "site_positions": data.site_xpos.tolist(),
            "site_orientations": data.site_xmat.tolist(),
            "camera_names": [model.camera_id2name(i) for i in range(model.ncam)],
            "camera_positions": data.cam_xpos.tolist(),
            "camera_orientations": data.cam_xmat.tolist(),
        }

    def close(self) -> None:
        if self.env is not None:
            self.env.close()
        self.env = None
        self.task_name = None
        self._action_context = {}
        self._simulator_scene = None

    def _load_sdk(self) -> None:
        if self._robocasa is not None:
            return
        if not self.robocasa_root.is_dir():
            raise EnvironmentError(
                f"RoboCasa root does not exist: {self.robocasa_root}"
            )
        root = str(self.robocasa_root)
        if root in sys.path:
            sys.path.remove(root)
        sys.path.insert(0, root)
        robosuite_root = self.robocasa_root / "robosuite"
        if (robosuite_root / "robosuite" / "__init__.py").is_file():
            robosuite_path = str(robosuite_root)
            if robosuite_path in sys.path:
                sys.path.remove(robosuite_path)
            sys.path.insert(0, robosuite_path)
        try:
            self._robocasa = importlib.import_module("robocasa")
        except ImportError as error:
            raise EnvironmentError(f"Failed to import RoboCasa: {error}") from error

    def _decorate_observation(self, observation: Any) -> dict[str, Any]:
        if not isinstance(observation, Mapping):
            raise EnvironmentError("RoboCasa observation must be a dictionary")
        result = dict(observation)
        result["available_skills"] = (
            list(self.available_skills)
            if self.available_skills is not None
            else [self.task_name]
            if self.task_name
            else []
        )
        result["task_name"] = self.task_name
        result["episode_index"] = self.episode_index
        return result

    @staticmethod
    def _context_string(context: Mapping[str, Any], key: str) -> str | None:
        value = context.get(key)
        return value if isinstance(value, str) and value else None

    def _save_raw_action_artifact(self, raw_action: Any) -> str | None:
        import numpy as np

        action_array = np.ascontiguousarray(raw_action.copy())
        artifact_dir = self._action_context.get("artifact_dir")
        if not isinstance(artifact_dir, (str, Path)) or not str(artifact_dir):
            return None
        chunk_index = self._action_context.get("candidate_chunk_index", 0)
        try:
            chunk_index = int(chunk_index)
        except (TypeError, ValueError):
            chunk_index = 0
        output_dir = Path(artifact_dir).expanduser() / "raw_actions"
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            artifact_path = (
                output_dir / f"candidate-chunk-{chunk_index:06d}-{uuid.uuid4().hex}.npz"
            )
            with artifact_path.open("xb") as file:
                np.savez_compressed(file, action=action_array)
        except FileExistsError:
            return None
        except OSError as error:
            raise EnvironmentError(
                f"Failed to save raw RoboCasa action artifact: {output_dir}"
            ) from error
        return str(artifact_path.resolve())

    def _apply_action_range_policy(
        self,
        chunk: dict[str, Any],
        raw_action: Any,
        raw_action_artifact: str | None,
    ) -> dict[str, Any]:
        import numpy as np

        if self.action_range_policy == "reject":
            self._raise_if_action_out_of_range(chunk, raw_action, raw_action_artifact)
            return {
                "action_range_policy": "reject",
                "action_clipped": False,
                "action_clipped_value_count": 0,
                "action_clipped_fields": [],
                "max_preclip_range_excess": 0.0,
            }

        clipped_fields: list[str] = []
        clipped_value_count = 0
        max_excess = 0.0
        for key in _ACTION_DIMS:
            array = chunk[key]
            out_of_range = (array < -1.0) | (array > 1.0)
            count = int(np.count_nonzero(out_of_range))
            if not count:
                continue
            clipped_fields.append(key)
            clipped_value_count += count
            max_excess = max(
                max_excess,
                float(np.max(np.maximum(np.abs(array[out_of_range]) - 1.0, 0.0))),
            )
            chunk[key] = np.clip(array, -1.0, 1.0)
        return {
            "action_range_policy": "official_controller_clip",
            "action_clipped": bool(clipped_value_count),
            "action_clipped_value_count": clipped_value_count,
            "action_clipped_fields": clipped_fields,
            "max_preclip_range_excess": max_excess,
        }

    def _raise_if_action_out_of_range(
        self,
        chunk: Mapping[str, Any],
        raw_action: Any,
        raw_action_artifact: str | None,
    ) -> None:
        import numpy as np

        violations: list[dict[str, Any]] = []
        for key in _ACTION_DIMS:
            array = chunk[key]
            indices = np.argwhere(
                (array < -1.0 - _ACTION_RANGE_TOLERANCE)
                | (array > 1.0 + _ACTION_RANGE_TOLERANCE)
            )
            for row, dimension in indices.tolist():
                actual_value = float(array[row, dimension])
                excess = max(
                    (-1.0 - _ACTION_RANGE_TOLERANCE) - actual_value,
                    actual_value - (1.0 + _ACTION_RANGE_TOLERANCE),
                    0.0,
                )
                violations.append(
                    {
                        "field": key,
                        "row": int(row),
                        "dimension": int(dimension),
                        "actual_value": actual_value,
                        "low": -1.0,
                        "high": 1.0,
                        "tolerance": _ACTION_RANGE_TOLERANCE,
                        "excess": float(excess),
                    }
                )
        if not violations:
            return
        first = violations[0]
        context = self._action_context
        task_value = context.get("task") or self.task_name or ""
        raise ActionRangeError(
            task=str(task_value),
            episode=int(context.get("episode", self.episode_index)),
            seed=int(context.get("seed", self.task_seed)),
            candidate_chunk_index=int(context.get("candidate_chunk_index", 0)),
            raw_action=raw_action,
            raw_action_artifact=raw_action_artifact,
            execution_id=self._context_string(context, "execution_id"),
            attempt_id=self._context_string(context, "attempt_id"),
            episode_attempt_id=self._context_string(context, "episode_attempt_id"),
            run_id=self._context_string(context, "run_id"),
            violations=violations,
            **first,
        )
