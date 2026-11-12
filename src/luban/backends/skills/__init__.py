from importlib import import_module
from typing import Any

from luban.backends.skills.base import SkillBackend
from luban.backends.skills.registry import (
    SkillBackendRegistry,
    create_skill_backend,
    register_skill_backend,
    skill_backend_registry,
)

_EXPORTS = {
    "LiberoPi05PolicyBackend": (
        "luban.backends.skills.pi05_libero",
        "LiberoPi05PolicyBackend",
    ),
    "LanguageSkillBackend": (
        "luban.backends.skills.language",
        "LanguageSkillBackend",
    ),
    "LocalPolicyBackend": (
        "luban.backends.skills.local",
        "LocalPolicyBackend",
    ),
    "OpenVLARemotePolicyBackend": (
        "luban.backends.skills.openvla",
        "OpenVLARemotePolicyBackend",
    ),
    "OpenPIRoboCasaPolicyBackend": (
        "luban.backends.skills.openpi",
        "OpenPIRoboCasaPolicyBackend",
    ),
    "OpenPIWebSocketPolicyBackend": (
        "luban.backends.skills.openpi",
        "OpenPIWebSocketPolicyBackend",
    ),
    "RLDXRemotePolicyBackend": (
        "luban.backends.skills.rldx",
        "RLDXRemotePolicyBackend",
    ),
    "XR1LocalPolicyBackend": (
        "luban.backends.skills.xiaomi",
        "XiaomiRobotics1VLAPolicyBackend",
    ),
    "XR1RemotePolicyBackend": (
        "luban.backends.skills.xiaomi_remote",
        "XiaomiRemotePolicyBackend",
    ),
    "XiaomiRobotics1VLAPolicyBackend": (
        "luban.backends.skills.xiaomi",
        "XiaomiRobotics1VLAPolicyBackend",
    ),
    "XiaomiRemotePolicyBackend": (
        "luban.backends.skills.xiaomi_remote",
        "XiaomiRemotePolicyBackend",
    ),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(name) from error
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


__all__ = [
    "LiberoPi05PolicyBackend",
    "LanguageSkillBackend",
    "LocalPolicyBackend",
    "OpenPIRoboCasaPolicyBackend",
    "OpenPIWebSocketPolicyBackend",
    "OpenVLARemotePolicyBackend",
    "RLDXRemotePolicyBackend",
    "XR1LocalPolicyBackend",
    "XR1RemotePolicyBackend",
    "XiaomiRobotics1VLAPolicyBackend",
    "XiaomiRemotePolicyBackend",
    "SkillBackend",
    "SkillBackendRegistry",
    "create_skill_backend",
    "register_skill_backend",
    "skill_backend_registry",
]
