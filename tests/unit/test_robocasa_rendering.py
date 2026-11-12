import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
from types import ModuleType, SimpleNamespace

import pytest

from luban.environments.benchmarks.robocasa import RoboCasaEnvironment
from luban.environments.benchmarks.robocasa._rendering import (
    RENDER_GUARD_VERSION,
    install_render_guard,
)


def fake_bindings(monkeypatch, *, egl=False):
    thread = local()
    damaged = set()
    handles = {}
    native_calls = []

    def current():
        return getattr(thread, "current", None)

    class GL:
        def __init__(self, name):
            self.name = name
            self._context = SimpleNamespace(address=len(handles) + 1)
            handles[self._context.address] = name
            self.fail = False

        def make_current(self):
            if self.fail:
                raise RuntimeError("binding failed")
            if egl and getattr(thread, "api", "opengl") != "opengl":
                raise RuntimeError("client API was not restored")
            thread.current = self.name

        def free(self):
            thread.current = None
            thread.api = "es"
            self._context = None

    if egl:
        GL.__module__ = "robosuite.renderers.context.egl_context"
        egl_module = ModuleType(GL.__module__)

        def get_context():
            name = current()
            address = next((a for a, n in handles.items() if n == name), None)
            return SimpleNamespace(address=address) if address else None

        def make_current(display, draw, read, handle):
            assert (display, draw, read) == ("display", "draw", "read")
            assert thread.api == "opengl"
            thread.current = handles[handle.address]
            return True

        def bind_api(api):
            thread.api = api
            return True

        egl_module.EGL = SimpleNamespace(
            EGL_DRAW="draw",
            EGL_READ="read",
            eglGetCurrentContext=get_context,
            eglQueryAPI=lambda: getattr(thread, "api", "opengl"),
            eglGetCurrentDisplay=lambda: "display",
            eglGetCurrentSurface=lambda surface: surface,
            eglBindAPI=bind_api,
            eglMakeCurrent=make_current,
        )
        monkeypatch.setitem(sys.modules, GL.__module__, egl_module)

    class Context:
        def __init__(self, name):
            self.gl_ctx = GL(name)
            self.con = SimpleNamespace(name=name)
            self.pending = None
            self.barrier = None
            self.nested = None
            self.fail_read = False

        def __del__(self):
            if not hasattr(self, "con"):
                return
            if current() != self.con.name:
                damaged.add(current())
            self.gl_ctx.free()
            del self.con
            del self.gl_ctx

        def deferred(self):
            if self.pending is not None:
                self.pending.__del__()
                self.pending = None
            if self.barrier is not None:
                self.barrier.wait(timeout=5)

        def read_pixels(self, width, height, positional=False):
            self.deferred()
            if self.fail_read:
                raise RuntimeError("read failed")
            rgb = [[0, 0, 0]]
            if positional:
                module.mjr_readPixels(rgb, None, None, self.con)
            else:
                module.mjr_readPixels(rgb=rgb, depth=None, viewport=None, con=self.con)
            return rgb

        def render(self, width, height, positional=False):
            self.deferred()
            if self.nested is not None:
                self.nested.render(width, height)
            if positional:
                module.mjr_render(None, None, self.con)
            else:
                module.mjr_render(viewport=None, scn=None, con=self.con)

        def update_offscreen_size(self, width, height):
            assert current() == self.con.name
            self.deferred()
            assert current() == self.con.name
            return (width, height)

    def pixels(rgb, depth, viewport, con):
        if current() != con.name or con.name in damaged:
            raise RuntimeError("lost or damaged rendering context")
        rgb[0] = [10, 20, 30]
        native_calls.append(con.name)

    module = ModuleType("mujoco")
    module.mjr_readPixels = pixels
    module.mjr_render = lambda viewport, scn, con: pixels([[0, 0, 0]], None, None, con)
    binding = ModuleType("robosuite.utils.binding_utils")
    binding.MjRenderContext = Context
    monkeypatch.setitem(sys.modules, "mujoco", module)
    monkeypatch.setitem(sys.modules, "robosuite.utils.binding_utils", binding)
    return Context, thread, damaged, native_calls, module


@pytest.mark.parametrize("operation", ["read_pixels", "render"])
@pytest.mark.parametrize("positional", [False, True])
def test_deferred_cleanup_control_and_guard(monkeypatch, operation, positional):
    Context, thread, damaged, native_calls, module = fake_bindings(monkeypatch)
    old, active = Context("old"), Context("active")
    active.gl_ctx.make_current()
    active.pending = old
    with pytest.raises(RuntimeError, match="lost or damaged"):
        getattr(active, operation)(1, 1, positional=positional)
    assert "active" in damaged
    damaged.clear()
    counters = install_render_guard()
    active.pending = Context("another-old")
    getattr(active, operation)(1, 1, positional=positional)
    assert damaged == set()
    assert counters["guarded_cleanup"] == 1
    assert (
        counters["guarded_readback" if operation == "read_pixels" else "guarded_draw"]
        == 1
    )
    active.__del__()


def test_install_is_idempotent(monkeypatch):
    Context, thread, damaged, native_calls, module = fake_bindings(monkeypatch)
    first = install_render_guard()
    hooks = (Context.__del__, Context.render, Context.read_pixels, module.mjr_render)
    assert install_render_guard() is first
    assert hooks == (
        Context.__del__,
        Context.render,
        Context.read_pixels,
        module.mjr_render,
    )


def test_cleanup_preserves_foreign_egl_context_and_api(monkeypatch):
    Context, thread, damaged, native_calls, module = fake_bindings(
        monkeypatch, egl=True
    )
    old, active = Context("old"), Context("active")
    active.gl_ctx.make_current()
    counters = install_render_guard()
    old.__del__()
    assert thread.current == "active"
    assert thread.api == "opengl"
    assert damaged == set()
    assert counters["restored_egl_context"] == 1
    active.__del__()
    assert thread.current is None
    assert thread.api == "opengl"


def test_partial_initialization_preserves_foreign_context(monkeypatch):
    Context, thread, damaged, native_calls, module = fake_bindings(
        monkeypatch, egl=True
    )
    partial, active = Context("partial"), Context("active")
    del partial.con
    active.gl_ctx.make_current()
    counters = install_render_guard()
    partial.__del__()
    partial.__del__()
    assert not hasattr(partial, "gl_ctx")
    assert thread.current == "active"
    assert counters["guarded_partial_cleanup"] == 1
    active.__del__()


def test_binding_failure_does_not_call_sdk_resource_cleanup(monkeypatch):
    Context, thread, damaged, native_calls, module = fake_bindings(monkeypatch)
    old, active = Context("old"), Context("active")
    active.gl_ctx.make_current()
    install_render_guard()
    old.gl_ctx.fail = True
    with pytest.raises(RuntimeError, match="binding failed"):
        old.__del__()
    assert hasattr(old, "con")
    assert damaged == set()
    old.gl_ctx.fail = False
    old.__del__()
    active.__del__()


def test_resize_binds_owner_and_survives_deferred_cleanup(monkeypatch):
    Context, thread, damaged, native_calls, module = fake_bindings(
        monkeypatch, egl=True
    )
    active, another = Context("active"), Context("another")
    another.gl_ctx.make_current()
    counters = install_render_guard()
    active.pending = Context("retired")
    assert active.update_offscreen_size(512, 256) == (512, 256)
    assert counters["guarded_buffer_update"] == 1
    assert thread.current == "active"
    active.__del__()
    another.__del__()


def test_nested_rendering_restores_outer_context(monkeypatch):
    Context, thread, damaged, native_calls, module = fake_bindings(monkeypatch)
    outer, inner = Context("outer"), Context("inner")
    outer.nested = inner
    counters = install_render_guard()
    outer.render(1, 1)
    assert native_calls == ["inner", "outer"]
    assert counters["guarded_draw"] == 2
    outer.__del__()
    inner.__del__()


def test_failed_read_clears_active_reader(monkeypatch):
    Context, thread, damaged, native_calls, module = fake_bindings(monkeypatch)
    active = Context("active")
    install_render_guard()
    active.fail_read = True
    with pytest.raises(RuntimeError, match="read failed"):
        active.read_pixels(1, 1)
    thread.current = "standalone"
    rgb = [[0, 0, 0]]
    module.mjr_readPixels(rgb, None, None, SimpleNamespace(name="standalone"))
    assert rgb == [[10, 20, 30]]
    active.__del__()


@pytest.mark.parametrize("operation", ["read_pixels", "render"])
def test_independent_render_threads_keep_their_bindings(monkeypatch, operation):
    Context, thread, damaged, native_calls, module = fake_bindings(monkeypatch)
    counters = install_render_guard()
    barrier = Barrier(2)

    def run(name):
        active = Context(name)
        active.pending = Context(f"retired-{name}")
        active.barrier = barrier
        getattr(active, operation)(1, 1)
        active.__del__()
        return name

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(run, ["thread-a", "thread-b"])) == {"thread-a", "thread-b"}
    assert damaged == set()
    assert counters["guarded_cleanup"] == 4
    assert (
        counters["guarded_readback" if operation == "read_pixels" else "guarded_draw"]
        == 2
    )


@pytest.mark.parametrize("factory", [False, True])
def test_environment_installs_before_initial_observation(
    monkeypatch, tmp_path, factory
):
    fake_bindings(monkeypatch)
    events = []

    class FakeEnvironment:
        def reset(self, seed):
            assert environment.rendering_guard_metadata()["enabled"]
            events.append("reset")
            return {}, {}

        def close(self):
            events.append("close")

    def make(**kwargs):
        assert environment.rendering_guard_metadata()["enabled"]
        return FakeEnvironment()

    gym = ModuleType("gymnasium")
    gym.make = lambda *args, **kwargs: make(**kwargs)
    monkeypatch.setitem(sys.modules, "gymnasium", gym)
    environment = RoboCasaEnvironment(
        robocasa_root=tmp_path, environment_factory=make if factory else None
    )
    environment._robocasa = SimpleNamespace()
    task = {"name": "CloseBlenderLid", "split": "target", "seed": 10, "horizon": 2}
    environment.reset(task)
    counters = environment._render_guard_counters
    environment.reset(task)
    assert environment._render_guard_counters is counters
    assert environment.rendering_guard_metadata()["version"] == RENDER_GUARD_VERSION
    environment.close()
    assert events == ["reset", "close", "reset", "close"]


def test_non_rendering_environment_keeps_optional_sdk_unloaded(monkeypatch, tmp_path):
    monkeypatch.delitem(sys.modules, "robosuite.utils.binding_utils", raising=False)
    environment = RoboCasaEnvironment(robocasa_root=tmp_path, enable_render=False)
    environment._ensure_render_guard()
    assert environment.rendering_guard_metadata()["enabled"] is False


def test_non_mujoco_factory_does_not_require_optional_sdk(monkeypatch, tmp_path):
    monkeypatch.delitem(sys.modules, "robosuite.utils.binding_utils", raising=False)
    environment = RoboCasaEnvironment(
        robocasa_root=tmp_path, environment_factory=lambda **kwargs: None
    )
    environment._ensure_render_guard()
    assert environment.rendering_guard_metadata()["enabled"] is False


def test_stock_cli_installs_guard_without_launcher_hook(monkeypatch, tmp_path):
    import numpy as np
    import yaml

    from luban.backends.skills import SkillBackend
    from luban.cli import run_config
    from luban.environments.benchmarks.robocasa.environment import _ACTION_DIMS

    Context, thread, damaged, native_calls, module = fake_bindings(monkeypatch)
    for name in ("robocasa", "robosuite"):
        sdk = ModuleType(name)
        sdk.__file__ = str(tmp_path / name / "__init__.py")
        monkeypatch.setitem(sys.modules, name, sdk)
    closed = []

    class FakeEnvironment:
        def reset(self, seed):
            assert "_luban_render_guard" in Context.__dict__
            return {}, {}

        def step(self, action):
            return {}, 1.0, False, False, {"success": True}

        def close(self):
            closed.append(True)

    class Policy(SkillBackend):
        def predict(self, inputs):
            return {
                key: np.zeros((1, width), dtype=np.float32)
                for key, width in _ACTION_DIMS.items()
            }

    policies = ModuleType("rendering_cli_test_policy")
    policies.Policy = Policy
    monkeypatch.setitem(sys.modules, policies.__name__, policies)
    gym = ModuleType("gymnasium")
    gym.make = lambda *args, **kwargs: FakeEnvironment()
    monkeypatch.setitem(sys.modules, "gymnasium", gym)
    agent_path = tmp_path / "agent.yaml"
    agent_path.write_text(
        yaml.safe_dump(
            {
                "agent": {"class_path": "luban.agent_core.DefaultAgent"},
                "planner": {"class_path": "luban.agent_core.TaskSkillPlanner"},
                "verifier": {"class_path": "luban.agent_core.EnvironmentVerifier"},
                "memory": {"class_path": "luban.agent_core.InMemoryMemory"},
                "skill_backend": {"class_path": "rendering_cli_test_policy.Policy"},
            }
        )
    )
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        yaml.safe_dump(
            {
                "agent_config": str(agent_path),
                "pipeline": {"class_path": "luban.pipelines.DirectPipeline"},
                "runtime": {
                    "class_path": "luban.runtimes.SyncRuntime",
                    "init_args": {"max_steps": 1, "output_dir": str(tmp_path / "runs")},
                },
                "environment": {
                    "class_path": (
                        "luban.environments.benchmarks.robocasa.RoboCasaEnvironment"
                    ),
                    "init_args": {"robocasa_root": str(tmp_path)},
                },
                "task": {
                    "name": "CloseBlenderLid",
                    "split": "target",
                    "seed": 10,
                    "horizon": 2,
                },
            }
        )
    )
    result = run_config(run_path)
    assert result["success"] is True
    assert "_luban_render_guard" in Context.__dict__
    assert closed == [True]
