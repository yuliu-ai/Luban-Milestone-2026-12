import numpy as np

from luban.backends.skills.visual_servo import VisualServoSkillBackend


def test_occlusion_holds_measured_pose_instead_of_reusing_stale_target():
    backend = VisualServoSkillBackend()
    measured = [0.4, 0.1, 0.6, 0, 0, 0, 1]
    result = backend.predict(
        {
            "skill": "M12",
            "observation": {
                "ee_pose": measured,
                "visual_target_pose": None,
            },
        }
    )
    assert result["mode"] == "hold"
    assert result["pose"] == measured


def test_recovery_travel_is_bounded_and_decelerates_near_target():
    backend = VisualServoSkillBackend()
    measured = [0.4, 0.1, 0.6, 0, 0, 0, 1]
    for distance, expected in [(0.4, 0.012), (0.02, 0.006), (0.002, 0.002)]:
        target = [0.4 + distance, *measured[1:]]
        result = backend.predict(
            {
                "skill": "M12",
                "observation": {
                    "ee_pose": measured,
                    "visual_target_pose": target,
                },
            }
        )
        np.testing.assert_allclose(result["pose"][0] - measured[0], expected)
        assert result["pose"][1:] == measured[1:]
