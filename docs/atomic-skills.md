[English](atomic-skills.md) · [简体中文](atomic-skills.zh-CN.md)

# Atomic skills and the 64-GIF gallery

The library covers 64 skills in nine domains. Every animation has a descriptive name, call identifier and recording type. Click an image to view it at full size. The GIFs preserve the original frames of the 2026-10-27 v2.4 controlled-simulation demonstration, with no invented or interpolated motion.

There are 47 execution skills and 17 perception skills, with perception distributed across P, N and U. See the [evaluation results](reported-results.md#atomic-call-evaluation) for the subsequent v2.5 three-round campaign.

[P Perception](#domain-p) · [M Motion control](#domain-m) · [G Grasping and grippers](#domain-g) · [I Object interaction](#domain-i) · [A Articulated objects](#domain-a) · [T Tool use](#domain-t) · [N Navigation](#domain-n) · [F Flight control](#domain-f) · [U Gimbals and payloads](#domain-u)

[Call contract and composition](#call-contract)

<details>
<summary>Demonstration version, provenance and input conditions</summary>

## Demonstration version and input conditions

P01, P02 and P06 demonstrate detection, segmentation and classification of registered high-saturation colors. P04 decodes depth buffers using known camera parameters, and P09 estimates the state of a marked prismatic mechanism. Their captions describe those demonstrated capabilities. The 2026-10-28 v2.5 campaign uses the restored model-based perception configuration and is reported separately. Simulator captures, recorded outputs and measured trace replays are labeled throughout; playback frames do not contribute trials to success-rate denominators.

[Media provenance, dimensions, frames and hashes](assets/skills/gifs/manifest.json) · [Independent evaluator](https://github.com/yuliu-ai/atomic-skill-evaluator)

</details>

<a id="domain-p"></a>

## P · Perception · 12

<table>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P01.gif"><img src="assets/skills/gifs/P01.gif" width="280" alt="P01 Registered-color object detection"></a><br>
<strong>P01 · Registered-color object detection</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>detect_registered_color_objects</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P02.gif"><img src="assets/skills/gifs/P02.gif" width="280" alt="P02 Registered-color instance segmentation"></a><br>
<strong>P02 · Registered-color instance segmentation</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>segment_registered_color_instances</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P03.gif"><img src="assets/skills/gifs/P03.gif" width="280" alt="P03 6D object pose estimation"></a><br>
<strong>P03 · 6D object pose estimation</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>estimate_object_pose</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P04.gif"><img src="assets/skills/gifs/P04.gif" width="280" alt="P04 Calibrated depth-buffer decoding"></a><br>
<strong>P04 · Calibrated depth-buffer decoding</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>decode_calibrated_depth_buffer</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P05.gif"><img src="assets/skills/gifs/P05.gif" width="280" alt="P05 Finite-horizon object tracking"></a><br>
<strong>P05 · Finite-horizon object tracking</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>track_object_finite</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P06.gif"><img src="assets/skills/gifs/P06.gif" width="280" alt="P06 Registered-color classification"></a><br>
<strong>P06 · Registered-color classification</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>classify_registered_color</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P07.gif"><img src="assets/skills/gifs/P07.gif" width="280" alt="P07 Affordance estimation"></a><br>
<strong>P07 · Affordance estimation</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>estimate_affordances</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P08.gif"><img src="assets/skills/gifs/P08.gif" width="280" alt="P08 Articulation estimation"></a><br>
<strong>P08 · Articulation estimation</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>estimate_articulation</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P09.gif"><img src="assets/skills/gifs/P09.gif" width="280" alt="P09 Marked prismatic-state estimation"></a><br>
<strong>P09 · Marked prismatic-state estimation</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>estimate_marked_prismatic_state</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P10.gif"><img src="assets/skills/gifs/P10.gif" width="280" alt="P10 Force and torque sensing"></a><br>
<strong>P10 · Force and torque sensing</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>read_force_torque</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P11.gif"><img src="assets/skills/gifs/P11.gif" width="280" alt="P11 Contact detection"></a><br>
<strong>P11 · Contact detection</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>detect_contact</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/P12.gif"><img src="assets/skills/gifs/P12.gif" width="280" alt="P12 Robot-state sensing"></a><br>
<strong>P12 · Robot-state sensing</strong><br>
<sub>Simulator capture</sub>
<br><sub><code>read_robot_state</code></sub>
</td>
</tr>
</table>

<a id="domain-m"></a>

## M · Motion control · 11

<table>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/M01.gif"><img src="assets/skills/gifs/M01.gif" width="280" alt="M01 Linear end-effector motion"></a><br>
<strong>M01 · Linear end-effector motion</strong><br>
<sub>Simulator capture</sub>
<br><sub><code>move_ee_linear</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/M02.gif"><img src="assets/skills/gifs/M02.gif" width="280" alt="M02 End-effector waypoint motion"></a><br>
<strong>M02 · End-effector waypoint motion</strong><br>
<sub>Simulator capture</sub>
<br><sub><code>move_ee_waypoints</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/M03.gif"><img src="assets/skills/gifs/M03.gif" width="280" alt="M03 In-place end-effector rotation"></a><br>
<strong>M03 · In-place end-effector rotation</strong><br>
<sub>Simulator capture</sub>
<br><sub><code>rotate_ee_in_place</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/M04.gif"><img src="assets/skills/gifs/M04.gif" width="280" alt="M04 End-effector pose motion"></a><br>
<strong>M04 · End-effector pose motion</strong><br>
<sub>Simulator capture</sub>
<br><sub><code>move_ee_pose</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/M05.gif"><img src="assets/skills/gifs/M05.gif" width="280" alt="M05 Arm joint motion"></a><br>
<strong>M05 · Arm joint motion</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>move_arm_joints</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/M06.gif"><img src="assets/skills/gifs/M06.gif" width="280" alt="M06 Timed end-effector pose hold"></a><br>
<strong>M06 · Timed end-effector pose hold</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>hold_ee_pose</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/M08.gif"><img src="assets/skills/gifs/M08.gif" width="280" alt="M08 Straight base motion"></a><br>
<strong>M08 · Straight base motion</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>move_base_linear</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/M09.gif"><img src="assets/skills/gifs/M09.gif" width="280" alt="M09 In-place base rotation"></a><br>
<strong>M09 · In-place base rotation</strong><br>
<sub>Simulator capture</sub>
<br><sub><code>rotate_base_in_place</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/M10.gif"><img src="assets/skills/gifs/M10.gif" width="280" alt="M10 Controlled robot stop"></a><br>
<strong>M10 · Controlled robot stop</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>stop_robot_motion</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/M11.gif"><img src="assets/skills/gifs/M11.gif" width="280" alt="M11 Synchronized dual-arm motion"></a><br>
<strong>M11 · Synchronized dual-arm motion</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>move_bimanual_poses</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/M12.gif"><img src="assets/skills/gifs/M12.gif" width="280" alt="M12 Finite-horizon visual servoing"></a><br>
<strong>M12 · Finite-horizon visual servoing</strong><br>
<sub>Simulator capture</sub>
<br><sub><code>servo_ee_to_target_finite</code></sub>
</td>
</tr>
</table>

<a id="domain-g"></a>

## G · Grasping and grippers · 8

<table>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/G01.gif"><img src="assets/skills/gifs/G01.gif" width="280" alt="G01 Parallel-gripper width control"></a><br>
<strong>G01 · Parallel-gripper width control</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>set_parallel_gripper_width</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/G03.gif"><img src="assets/skills/gifs/G03.gif" width="280" alt="G03 Force-controlled parallel grasp"></a><br>
<strong>G03 · Force-controlled parallel grasp</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>grasp_parallel_force</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/G04.gif"><img src="assets/skills/gifs/G04.gif" width="280" alt="G04 Dexterous-hand joint positioning"></a><br>
<strong>G04 · Dexterous-hand joint positioning</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>set_hand_joint_positions</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/G05.gif"><img src="assets/skills/gifs/G05.gif" width="280" alt="G05 Parameterized dexterous grasp"></a><br>
<strong>G05 · Parameterized dexterous grasp</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>close_hand_grasp</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/G09.gif"><img src="assets/skills/gifs/G09.gif" width="280" alt="G09 Timed grasp-force regulation"></a><br>
<strong>G09 · Timed grasp-force regulation</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>regulate_grasp_force</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/G10.gif"><img src="assets/skills/gifs/G10.gif" width="280" alt="G10 Timed gripper-state hold"></a><br>
<strong>G10 · Timed gripper-state hold</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>hold_gripper_state</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/G11.gif"><img src="assets/skills/gifs/G11.gif" width="280" alt="G11 Gripper zero calibration"></a><br>
<strong>G11 · Gripper zero calibration</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>calibrate_gripper_zero</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/G12.gif"><img src="assets/skills/gifs/G12.gif" width="280" alt="G12 Gripper step-response test"></a><br>
<strong>G12 · Gripper step-response test</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>step_gripper_response</code></sub>
</td>
</tr>
</table>

<a id="domain-i"></a>

## I · Object interaction · 9

<table>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/I01.gif"><img src="assets/skills/gifs/I01.gif" width="280" alt="I01 Linear object pushing"></a><br>
<strong>I01 · Linear object pushing</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>push_object_linear</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/I02.gif"><img src="assets/skills/gifs/I02.gif" width="280" alt="I02 Linear object pulling"></a><br>
<strong>I02 · Linear object pulling</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>pull_object_linear</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/I04.gif"><img src="assets/skills/gifs/I04.gif" width="280" alt="I04 Planar object rotation"></a><br>
<strong>I04 · Planar object rotation</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>rotate_object_planar</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/I06.gif"><img src="assets/skills/gifs/I06.gif" width="280" alt="I06 Vertical held-object motion"></a><br>
<strong>I06 · Vertical held-object motion</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>translate_held_object_vertical</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/I08.gif"><img src="assets/skills/gifs/I08.gif" width="280" alt="I08 Held-object placement"></a><br>
<strong>I08 · Held-object placement</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>place_held_object</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/I09.gif"><img src="assets/skills/gifs/I09.gif" width="280" alt="I09 Held-object alignment"></a><br>
<strong>I09 · Held-object alignment</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>align_held_object</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/I10.gif"><img src="assets/skills/gifs/I10.gif" width="280" alt="I10 Straight held-object insertion"></a><br>
<strong>I10 · Straight held-object insertion</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>insert_held_object</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/I11.gif"><img src="assets/skills/gifs/I11.gif" width="280" alt="I11 Straight held-object extraction"></a><br>
<strong>I11 · Straight held-object extraction</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>extract_held_object</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/I12.gif"><img src="assets/skills/gifs/I12.gif" width="280" alt="I12 Held-object flipping"></a><br>
<strong>I12 · Held-object flipping</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>flip_held_object</code></sub>
</td>
</tr>
</table>

<a id="domain-a"></a>

## A · Articulated objects · 6

<table>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/A01.gif"><img src="assets/skills/gifs/A01.gif" width="280" alt="A01 Prismatic-joint motion"></a><br>
<strong>A01 · Prismatic-joint motion</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>translate_prismatic_joint</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/A02.gif"><img src="assets/skills/gifs/A02.gif" width="280" alt="A02 Revolute-joint motion"></a><br>
<strong>A02 · Revolute-joint motion</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>rotate_revolute_joint</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/A03.gif"><img src="assets/skills/gifs/A03.gif" width="280" alt="A03 Rotary-control operation"></a><br>
<strong>A03 · Rotary-control operation</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>turn_rotary_control</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/A04.gif"><img src="assets/skills/gifs/A04.gif" width="280" alt="A04 Button pressing"></a><br>
<strong>A04 · Button pressing</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>press_button</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/A05.gif"><img src="assets/skills/gifs/A05.gif" width="280" alt="A05 Discrete-switch positioning"></a><br>
<strong>A05 · Discrete-switch positioning</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>set_discrete_switch</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/A07.gif"><img src="assets/skills/gifs/A07.gif" width="280" alt="A07 Mechanical latch operation"></a><br>
<strong>A07 · Mechanical latch operation</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>set_latch_state</code></sub>
</td>
</tr>
</table>

<a id="domain-t"></a>

## T · Tool use · 2

<table>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/T04.gif"><img src="assets/skills/gifs/T04.gif" width="280" alt="T04 Single tool-wiping stroke"></a><br>
<strong>T04 · Single tool-wiping stroke</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>wipe_tool_stroke</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/T08.gif"><img src="assets/skills/gifs/T08.gif" width="280" alt="T08 Dosed dispensing"></a><br>
<strong>T08 · Dosed dispensing</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>dispense_dosed</code></sub>
</td>
</tr>
</table>

<a id="domain-n"></a>

## N · Navigation · 6

<table>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/N01.gif"><img src="assets/skills/gifs/N01.gif" width="280" alt="N01 Finite-window self-localization"></a><br>
<strong>N01 · Finite-window self-localization</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>estimate_self_pose_finite</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/N02.gif"><img src="assets/skills/gifs/N02.gif" width="280" alt="N02 Local occupancy snapshot"></a><br>
<strong>N02 · Local occupancy snapshot</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>build_local_occupancy_snapshot</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/N03.gif"><img src="assets/skills/gifs/N03.gif" width="280" alt="N03 Traversable-region estimation"></a><br>
<strong>N03 · Traversable-region estimation</strong><br>
<sub>Recorded input / output</sub>
<br><sub><code>infer_navigation_region</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/N04.gif"><img src="assets/skills/gifs/N04.gif" width="280" alt="N04 Landing-patch observation"></a><br>
<strong>N04 · Landing-patch observation</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>observe_landing_patch</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/N05.gif"><img src="assets/skills/gifs/N05.gif" width="280" alt="N05 Ground-path segment following"></a><br>
<strong>N05 · Ground-path segment following</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>follow_ground_path_segment</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/N06.gif"><img src="assets/skills/gifs/N06.gif" width="280" alt="N06 Base docking alignment"></a><br>
<strong>N06 · Base docking alignment</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>align_base_to_dock</code></sub>
</td>
</tr>
</table>

<a id="domain-f"></a>

## F · Flight control · 8

<table>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/F01.gif"><img src="assets/skills/gifs/F01.gif" width="280" alt="F01 Constrained vertical takeoff"></a><br>
<strong>F01 · Constrained vertical takeoff</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>takeoff_vertical</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/F02.gif"><img src="assets/skills/gifs/F02.gif" width="280" alt="F02 Constrained vertical landing"></a><br>
<strong>F02 · Constrained vertical landing</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>land_vertical</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/F03.gif"><img src="assets/skills/gifs/F03.gif" width="280" alt="F03 Finite-duration hover"></a><br>
<strong>F03 · Finite-duration hover</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>hover_finite</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/F04.gif"><img src="assets/skills/gifs/F04.gif" width="280" alt="F04 Local-waypoint flight"></a><br>
<strong>F04 · Local-waypoint flight</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>fly_to_local_waypoint</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/F05.gif"><img src="assets/skills/gifs/F05.gif" width="280" alt="F05 3D trajectory-segment following"></a><br>
<strong>F05 · 3D trajectory-segment following</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>follow_air_trajectory_segment</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/F06.gif"><img src="assets/skills/gifs/F06.gif" width="280" alt="F06 In-place aerial yaw"></a><br>
<strong>F06 · In-place aerial yaw</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>rotate_air_yaw_in_place</code></sub>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/F07.gif"><img src="assets/skills/gifs/F07.gif" width="280" alt="F07 Terrain-following flight"></a><br>
<strong>F07 · Terrain-following flight</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>follow_terrain_segment</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/F08.gif"><img src="assets/skills/gifs/F08.gif" width="280" alt="F08 Controlled aerial braking"></a><br>
<strong>F08 · Controlled aerial braking</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>brake_aircraft_to_hold</code></sub>
</td>
</tr>
</table>

<a id="domain-u"></a>

## U · Gimbals and payloads · 2

<table>
<tr>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/U01.gif"><img src="assets/skills/gifs/U01.gif" width="280" alt="U01 Gimbal orientation control"></a><br>
<strong>U01 · Gimbal orientation control</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>set_gimbal_orientation</code></sub>
</td>
<td width="33%" align="center" valign="top">
<a href="assets/skills/gifs/U02.gif"><img src="assets/skills/gifs/U02.gif" width="280" alt="U02 Pose-tagged image capture"></a><br>
<strong>U02 · Pose-tagged image capture</strong><br>
<sub>Measured trace replay</sub>
<br><sub><code>capture_geotagged_frame</code></sub>
</td>
</tr>
</table>

<a id="call-contract"></a>

## Shared call contract

A skill defines inputs, preconditions, required capabilities, an execution budget, stopping criteria and failure feedback. The planner selects the skill and parameters, the backend generates actions, the environment executes them, and the verifier checks the goal state.

| Prefix | Domain | Count |
| --- | --- | --- |
| P | Perception | 12 |
| M | Motion | 11 |
| G | Grasping and grippers | 8 |
| I | Object interaction | 9 |
| A | Articulated objects | 6 |
| T | Tool use | 2 |
| N | Navigation | 6 |
| F | Flight | 8 |
| U | Gimbals and payloads | 2 |

The engineering list uses G11 for gripper zero calibration and G12 for response self-test. The packaged v2.2 execution catalog retains G07/G08 suction interfaces. These are different operations, so executable IDs must come from the selected catalog version.

## From atomic calls to compositions

A composition binds one step’s outputs to the next step’s inputs while preserving object identity, coordinate frames and state preconditions in a continuous world. Evaluated examples include grasp, lift, align and place; gimbal adjustment followed by pose-tagged capture; and path following followed by a controlled stop. Composition pass rates count complete program executions, separately from independent atomic-call trials.

<details>
<summary>Complete engineering skill index</summary>

## Engineering skill list

| ID | Engineering name | 中文名称 |
| --- | --- | --- |
| P01 | Detect objects | 目标检测 |
| P02 | Segment instances | 物体分割 |
| P03 | Estimate object pose | 物体位姿识别 |
| P04 | Estimate depth | 场景深度感知 |
| P05 | Track object finite | 阶段性目标跟踪 |
| P06 | Classify object attributes | 物体属性识别 |
| P07 | Estimate affordances | 可操作性分析 |
| P08 | Estimate articulation | 物体关节识别 |
| P09 | Estimate object state | 物体状态识别 |
| P10 | Read force torque | 力觉数据读取 |
| P11 | Detect contact | 接触检测 |
| P12 | Read robot state | 机器人状态读取 |
| M01 | Move ee linear | 末端定距直线移动 |
| M02 | Move ee waypoints | 末端多点路径移动 |
| M03 | Rotate ee in place | 末端旋转 |
| M04 | Move ee pose | 末端移动 |
| M05 | Move arm joints | 机械臂关节调节 |
| M06 | Hold ee pose | 末端定时静止 |
| M08 | Move base linear | 底盘直线移动 |
| M09 | Rotate base in place | 底盘原地旋转 |
| M10 | Stop robot motion | 机器人减速停止 |
| M11 | Move bimanual poses | 双臂同步移动 |
| M12 | Servo ee to target finite | 视觉闭环微调 |
| G01 | Set parallel gripper width | 夹爪开合度调节 |
| G03 | Grasp parallel force | 夹爪力控抓取 |
| G04 | Set hand joint positions | 灵巧手关节控制 |
| G05 | Close hand grasp | 灵巧手模式化抓握 |
| G09 | Regulate grasp force | 抓取力保持与调节 |
| G10 | Hold gripper state | 末端工具定时保持 |
| G11 | Gripper zero calibration | 夹爪零点标定 |
| G12 | Gripper step-response self-test | 夹爪响应性能自检 |
| I01 | Push object linear | 物体直线推动 |
| I02 | Pull object linear | 物体直线拉动 |
| I04 | Rotate object planar | 物体平面旋拧 |
| I06 | Translate held object vertical | 抓取物垂直升降 |
| I08 | Place held object | 抓取物放置落位 |
| I09 | Align held object | 抓取物姿态对准 |
| I10 | Insert held object | 抓取物直线插装 |
| I11 | Extract held object | 抓取物直线拔出 |
| I12 | Flip held object | 抓取物空间翻转 |
| A01 | Translate prismatic joint | 滑动机构推拉 |
| A02 | Rotate revolute joint | 铰链机构开合 |
| A03 | Turn rotary control | 旋钮与阀门旋转 |
| A04 | Press button | 按钮按压操作 |
| A05 | Set discrete switch | 多挡位开关调节 |
| A07 | Set latch state | 机械锁扣开合 |
| T04 | Wipe tool stroke | 工具单次擦拭 |
| T08 | Dispense dosed | 工具定量加注 |
| N01 | Estimate self pose finite | 机器人局部定位 |
| N02 | Build local occupancy snapshot | 局部障碍物建图 |
| N03 | Infer navigation region | 地面与低空可通行区检测 |
| N04 | Observe landing patch | 备选降落区地形评估 |
| N05 | Follow ground path segment | 局部路径跟踪行驶 |
| N06 | Align base to dock | 底盘对接对准 |
| F01 | Takeoff vertical | 受控垂直起飞 |
| F02 | Land vertical | 定点精准降落 |
| F03 | Hover finite | 定点定时悬停 |
| F04 | Fly to local waypoint | 单航点直线飞行 |
| F05 | Follow air trajectory segment | 空中局部轨迹跟踪 |
| F06 | Rotate air yaw in place | 悬停机头转向 |
| F07 | Follow terrain segment | 仿地定高飞行 |
| F08 | Brake aircraft to hold | 空中减速悬停 |
| U01 | Set gimbal orientation | 云台角度调节 |
| U02 | Capture geotagged frame | 带位姿图像采集 |

[Versioned execution catalog](../catalogs/v2.2/README.md) · [Demonstration console](https://liutong-luban-project.netlify.app/offline/)

</details>
