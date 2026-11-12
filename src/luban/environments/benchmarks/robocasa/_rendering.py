"""Process-local protection for robosuite rendering context lifetimes."""

import importlib
import sys
from threading import Lock, local
from typing import Any, cast

RENDER_GUARD_VERSION = "egl-context-lifecycle-v3"
_INSTALL_LOCK = Lock()
_MARKER = "_luban_render_guard"


def _saved_egl_state(context: Any) -> tuple[Any, Any, Any, Any, Any, Any] | None:
    module_name = type(context).__module__
    if not module_name.endswith(".egl_context"):
        return None
    module = sys.modules.get(module_name)
    egl = getattr(module, "EGL", None)
    if egl is None:
        return None
    previous = egl.eglGetCurrentContext()
    owned = getattr(context, "_context", None)
    return (
        egl,
        egl.eglQueryAPI(),
        egl.eglGetCurrentDisplay(),
        egl.eglGetCurrentSurface(egl.EGL_DRAW),
        egl.eglGetCurrentSurface(egl.EGL_READ),
        # The context being destroyed must never be rebound after destruction.
        previous
        if previous and not (owned and previous.address == owned.address)
        else None,
    )


def install_render_guard() -> dict[str, int]:
    """Install once per loaded SDK class; import optional SDKs only on demand."""
    mujoco: Any = importlib.import_module("mujoco")
    binding = importlib.import_module("robosuite.utils.binding_utils")
    context_class = binding.MjRenderContext
    with _INSTALL_LOCK:
        installed = context_class.__dict__.get(_MARKER)
        if installed is not None:
            if installed["version"] != RENDER_GUARD_VERSION:
                raise RuntimeError("an incompatible Luban render guard exists")
            return cast(dict[str, int], installed["counters"])

        active = local()
        count_lock = Lock()
        counters = {
            "guarded_cleanup": 0,
            "guarded_partial_cleanup": 0,
            "guarded_readback": 0,
            "guarded_draw": 0,
            "guarded_buffer_update": 0,
            "restored_egl_context": 0,
        }
        original_destroy = context_class.__del__
        original_read = context_class.read_pixels
        original_render = context_class.render
        original_pixels = mujoco.mjr_readPixels
        original_draw = mujoco.mjr_render

        def record(name: str) -> None:
            with count_lock:
                counters[name] += 1

        def destroy(self: Any) -> None:
            context = getattr(self, "gl_ctx", None)
            con = getattr(self, "con", None)
            if context is None and con is None:
                return
            if context is None:
                raise RuntimeError("cannot release rendering resources without owner")
            saved = _saved_egl_state(context)
            try:
                if con is None:
                    # SDK initialization may fail before MjrContext is allocated.
                    context.free()
                    del self.gl_ctx
                    record("guarded_partial_cleanup")
                    return
                context.make_current()
                record("guarded_cleanup")
                original_destroy(self)
            finally:
                if saved is not None:
                    egl, api, display, draw_surface, read_surface, previous = saved
                    # eglReleaseThread also resets the selected client API.
                    if not egl.eglBindAPI(api):
                        raise RuntimeError("failed to restore the EGL client API")
                    if previous is not None:
                        if not egl.eglMakeCurrent(
                            display, draw_surface, read_surface, previous
                        ):
                            raise RuntimeError(
                                "failed to restore the active EGL context"
                            )
                        record("restored_egl_context")

        def read(self: Any, *args: Any, **kwargs: Any) -> Any:
            previous = getattr(active, "reader", None)
            active.reader = self
            try:
                self.gl_ctx.make_current()
                return original_read(self, *args, **kwargs)
            finally:
                active.reader = previous

        def pixels(*args: Any, **kwargs: Any) -> Any:
            reader = getattr(active, "reader", None)
            con = kwargs.get("con", args[3] if len(args) > 3 else None)
            if reader is not None and con is reader.con:
                # Pixel-buffer allocation can invoke deferred SDK destructors.
                reader.gl_ctx.make_current()
                record("guarded_readback")
            return original_pixels(*args, **kwargs)

        def render(self: Any, *args: Any, **kwargs: Any) -> Any:
            previous = getattr(active, "renderer", None)
            active.renderer = self
            try:
                self.gl_ctx.make_current()
                return original_render(self, *args, **kwargs)
            finally:
                active.renderer = previous

        def draw(*args: Any, **kwargs: Any) -> Any:
            renderer = getattr(active, "renderer", None)
            con = kwargs.get("con", args[2] if len(args) > 2 else None)
            if renderer is not None and con is renderer.con:
                renderer.gl_ctx.make_current()
                record("guarded_draw")
            return original_draw(*args, **kwargs)

        original_buffer_update = getattr(context_class, "update_offscreen_size", None)

        def buffer_update(self: Any, *args: Any, **kwargs: Any) -> Any:
            self.gl_ctx.make_current()
            record("guarded_buffer_update")
            assert original_buffer_update is not None
            return original_buffer_update(self, *args, **kwargs)

        context_class.__del__ = destroy
        context_class.read_pixels = read
        context_class.render = render
        if original_buffer_update is not None:
            context_class.update_offscreen_size = buffer_update
        mujoco.mjr_readPixels = pixels
        mujoco.mjr_render = draw
        setattr(
            context_class,
            _MARKER,
            {"version": RENDER_GUARD_VERSION, "counters": counters},
        )
        return counters
