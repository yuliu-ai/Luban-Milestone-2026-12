"""Four feedback-controlled Panda recovery scenes with live camera observations."""

import argparse
import json
import multiprocessing as mp
import shutil
import socket
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from luban.backends.skills.visual_servo import VisualServoSkillBackend
from luban.environments.atomic.native.bullet_manipulation_plant import (
    BulletManipulationPlant,
)
from luban.environments.atomic.native.marked_rgbd_backend import align_marker_cloud
from luban.environments.atomic.native.rgbd_camera import SceneRGBDIO
from luban.observability.live_view import LiveViewPublisher

CASES = {
    "occlusion": [0.3, 0.6, 1.0],
    "slip": [0.35, 0.45, 0.55],
    "obstacle": [0.4, 0.8, 1.2],
    "udp_loss": [0.3, 0.6, 1.0],
}


def coordinator(sock):
    # Separate coordinator and execution processes exchange sequenced UDP feedback.
    while True:
        msg, addr = sock.recvfrom(1024)
        if msg == b"stop":
            break
        seq, measured_x = json.loads(msg)
        sock.sendto(json.dumps([seq, measured_x + 0.002]).encode(), addr)


def save_video(frames, output):
    # Repeat the last observed camera frame on a 25 Hz presentation clock.
    width, height = frames[0][1].size
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        "25",
        "-i",
        "pipe:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]
    with subprocess.Popen(cmd, stdin=subprocess.PIPE) as proc:
        for index, (stamp, image, caption) in enumerate(frames):
            end = frames[index + 1][0] if index + 1 < len(frames) else stamp + 0.04
            count = round((end - frames[0][0]) * 25) - round(
                (stamp - frames[0][0]) * 25
            )
            canvas = Image.new("RGB", (width, height))
            canvas.paste(image)
            ImageDraw.Draw(canvas).rectangle(
                (0, height - 38, width, height), fill="black"
            )
            ImageDraw.Draw(canvas).multiline_text(
                (5, height - 35), caption, fill="white"
            )
            proc.stdin.write(canvas.tobytes() * max(count, 0))
        proc.stdin.close()
        if proc.wait():
            raise RuntimeError(f"Video encoding failed: {output}")


def measure(kind, parameter, label, path, video=None, hd=False):
    plant = BulletManipulationPlant(
        object_between_fingers=True, markers=kind == "occlusion"
    )
    sim = plant.sim
    observer = (
        BulletManipulationPlant(
            object_between_fingers=True, markers=kind == "occlusion", egl_observer=True
        )
        if hd
        else None
    )
    start = np.array(plant.ee_pose("right"))
    obj = plant.object_body
    floor_z = plant.current["object"]["pose"][2]
    view = sim.computeViewMatrix(
        (start[:3] + [0.33, -0.32, 0.23]).tolist(), start[:3], [0, 0, 1]
    )
    proj = sim.computeProjectionMatrixFOV(44, 1, 0.02, 2)
    wide_projection = (
        sim.computeProjectionMatrixFOV(42, 16 / 9, 0.02, 2) if hd else None
    )
    camera = SceneRGBDIO(plant, path) if kind == "occlusion" else None
    servo = VisualServoSkillBackend()
    publisher = LiveViewPublisher.from_environment({"name": label}, kind)
    if kind == "slip":
        sim.changeDynamics(obj, -1, mass=0.10, lateralFriction=0.5)
        plant.gripper_speed_limits["right"] = 0.12
    if kind == "obstacle":
        half = [0.02, 0.02, 0.04]
        shape = sim.createCollisionShape(sim.GEOM_BOX, halfExtents=half)
        vis = sim.createVisualShape(
            sim.GEOM_BOX, halfExtents=half, rgbaColor=[1, 0.35, 0, 1]
        )
        mid = start[:3] + [0.08, 0, 0]
        obstacle = sim.createMultiBody(0, shape, vis, mid + [0, 0.22, 0])
        if observer:
            screen = observer.sim
            mesh = screen.createVisualShape(
                screen.GEOM_BOX, halfExtents=half, rgbaColor=[1, 0.35, 0, 1]
            )
            shown_obstacle = screen.createMultiBody(0, -1, mesh, mid + [0, 0.22, 0])
    process = None
    if kind == "udp_loss":
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        address = server.getsockname()
        process = mp.get_context("spawn").Process(target=coordinator, args=(server,))
        process.start()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(2)
        # Inline link emulator receives actual datagrams on an independent UDP endpoint.
        relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        relay.bind(("127.0.0.1", 0))
        relay.settimeout(2)
    begun = time.monotonic()
    t0 = t1 = stable = None
    effect = False
    hits = drops = 0
    ready = None
    phase = "grip"
    goal = start.copy()
    ok = False
    frames = []
    last_active = False
    picture = None
    observed = None
    try:
        with (path / "trace.jsonl").open("w") as trace:
            for step in range(1800):
                tick = time.monotonic()
                pos = np.array(plant.ee_pose("right"))
                state = plant.current
                block = np.array(state["object"]["pose"][:3])
                contacts = {
                    p[3]
                    for p in state["arms"]["right"]["gripper_contacts"]
                    if p[2] == obj and p[9] > 0.01
                }
                bilateral = {9, 10} <= contacts
                if t0 is None and ready is not None and tick - ready >= 0.5:
                    t0 = tick
                    phase = "injected"
                elapsed = tick - t0 if t0 is not None else -1
                duration = 0.4 if kind == "slip" else parameter
                active = 0 <= elapsed < duration
                evidence = {}
                if kind == "occlusion":
                    if step % 4 == 0 or active != last_active:
                        frame, _ = camera.capture()
                        if active:
                            frame["rgb"][:] = 0
                            frame["depth_buffer"][:] = 1
                            Image.fromarray(frame["rgb"]).save(camera.last_frame_path)
                            np.save(
                                Path(camera.last_frame_path).with_name(
                                    Path(camera.last_frame_path).stem
                                    + "-depth-buffer.npy"
                                ),
                                frame["depth_buffer"],
                            )
                        picture = frame["rgb"]
                        try:
                            observed = np.array(
                                align_marker_cloud(frame, camera.marker_geometry)[
                                    "pose"
                                ]
                            )
                            observed[:3] += [0.025, 0.015, 0.04]
                        except ValueError:
                            observed = None
                    act = servo.predict(
                        {
                            "skill": "M12",
                            "observation": {
                                "ee_pose": pos,
                                "visual_target_pose": observed,
                            },
                        }
                    )
                    goal = np.array(act["pose"])
                    target = block + [0.025, 0.015, 0.04]
                    error = float(np.linalg.norm(pos[:3] - target))
                    from luban.environments.atomic.native.ee_motion_backend import (
                        orientation_error_rad,
                    )

                    angle = float(
                        orientation_error_rad(pos[3:], state["object"]["pose"][3:])
                    )
                    ok = observed is not None and error < 0.004 and angle < 0.07
                    effect |= active and act["mode"] == "hold"
                    evidence.update(error_m=error, angle_rad=angle, mode=act["mode"])
                elif kind == "slip":
                    plant.gripper_force_limits["right"] = parameter if active else 6
                    plant.gripper_width_targets["right"] = 0
                    if t0 is None:
                        if bilateral:
                            goal[2] = floor_z + 0.03
                    else:
                        effect |= block[2] < floor_z + 0.009 and not bilateral
                        if effect and phase == "injected":
                            phase = "open"
                        if phase == "open":
                            plant.gripper_width_targets["right"] = 0.064
                            if state["arms"]["right"]["width_m"] > 0.056:
                                phase = "align"
                        if phase == "align":
                            plant.gripper_width_targets["right"] = 0.064
                            goal[:3] = block
                            if np.linalg.norm(pos[:3] - block) < 0.002:
                                phase = "regrip"
                        if phase == "regrip" and bilateral:
                            phase = "lift"
                        if phase == "lift":
                            goal[2] = floor_z + 0.03
                    ok = (
                        bilateral
                        and block[2] > floor_z + 0.024
                        and np.linalg.norm(state["object"]["linear_velocity_mps"])
                        < 0.015
                    )
                    evidence.update(
                        block_xyz=block.tolist(),
                        bilateral=bilateral,
                        force_limit_n=plant.gripper_force_limits["right"],
                    )
                elif kind == "obstacle":
                    # The translating box crosses the forward travel corridor.
                    y = (
                        0.22
                        if elapsed < 0
                        else max(0, 0.22 - 3 * elapsed)
                        if active
                        else min(0.22, 3 * (elapsed - duration))
                    )
                    sim.resetBasePositionAndOrientation(
                        obstacle, mid + [0, y, 0], [0, 0, 0, 1]
                    )
                    hit = (
                        sim.rayTest(
                            (start[:3] + [0.035, 0, 0]).tolist(),
                            (start[:3] + [0.14, 0, 0]).tolist(),
                        )[0][0]
                        == obstacle
                    )
                    if hit:
                        goal[:3] = pos[:3]
                    else:
                        goal[0] = min(start[0] + 0.14, pos[0] + 0.0025)
                    effect |= hit and active
                    hits += bool(sim.getContactPoints(plant.bodies["right"], obstacle))
                    ok = (
                        not hit
                        and not hits
                        and (
                            abs(pos[0] - start[0] - 0.14) < 0.003
                            if t0
                            else pos[0] > start[0] + 0.002
                        )
                    )
                    evidence.update(
                        path_blocked=hit, contacts=hits, obstacle_y=float(y)
                    )
                else:
                    request = json.dumps([step, pos[0]]).encode()
                    client.sendto(request, relay.getsockname())
                    data, sender = relay.recvfrom(1024)
                    if active:
                        drops += 1
                    else:
                        relay.sendto(data, address)
                        reply, _ = relay.recvfrom(1024)
                        relay.sendto(reply, sender)
                    client.settimeout(0.012)
                    try:
                        seq, x = json.loads(client.recvfrom(1024)[0])
                    except TimeoutError:
                        goal = pos.copy()
                        ok = False
                        acknowledged = False
                        effect |= active
                    else:
                        acknowledged = seq == step
                        goal[0] = min(start[0] + 0.15, x) if acknowledged else pos[0]
                        ok = acknowledged and pos[0] > start[0] + 0.001
                    if active:
                        resume_x = pos[0]
                    if elapsed >= duration:
                        ok = ok and pos[0] > resume_x + 0.001
                    evidence.update(dropped_packets=drops, acknowledged=acknowledged)
                if t0 is None:
                    ready = (ready or tick) if ok else None
                plant.set_ee_target("right", goal.tolist())
                plant.step()
                new_frame = hd or step % 6 == 0 or active != last_active
                if observer is not None:
                    screen = observer.sim
                    arm = plant.bodies["right"]
                    for index, joint in enumerate(
                        sim.getJointStates(arm, list(range(sim.getNumJoints(arm))))
                    ):
                        screen.resetJointState(
                            observer.bodies["right"], index, joint[0]
                        )
                    screen.resetBasePositionAndOrientation(
                        observer.object_body, *sim.getBasePositionAndOrientation(obj)
                    )
                    if kind == "obstacle":
                        screen.resetBasePositionAndOrientation(
                            shown_obstacle, *sim.getBasePositionAndOrientation(obstacle)
                        )
                    picture = np.asarray(
                        screen.getCameraImage(
                            1280,
                            720,
                            view,
                            wide_projection,
                            renderer=screen.ER_BULLET_HARDWARE_OPENGL,
                            flags=screen.ER_NO_SEGMENTATION_MASK,
                        )[2]
                    )[:, :, :3]
                    if kind == "occlusion" and active:
                        picture = np.zeros_like(picture)
                elif kind != "occlusion" and new_frame:
                    picture = np.array(
                        sim.getCameraImage(
                            320, 320, view, proj, renderer=sim.ER_TINY_RENDERER
                        )[2]
                    )[:, :, :3]
                if publisher and picture is not None:
                    publisher.publish({"image.front": picture}, step)
                if t0 is not None and effect and not active and ok:
                    stable = stable or tick
                    if tick - stable >= 0.5:
                        t1 = tick
                else:
                    stable = None
                if (
                    video is not None
                    and picture is not None
                    and (new_frame or t1 or (kind == "occlusion" and step % 4 == 0))
                ):
                    state_text = (
                        "BASELINE"
                        if t0 is None
                        else "INJECTION"
                        if active
                        else "RECOVERED"
                        if t1
                        else "RECOVERING"
                    )
                    detail = (
                        f"ACK={int(acknowledged)} drops={drops}"
                        if kind == "udp_loss"
                        else f"blocked={int(hit)} contacts={hits}"
                        if kind == "obstacle"
                        else f"{phase} grip={int(bilateral)}"
                        if kind == "slip"
                        else act["mode"]
                    )
                    caption = f"{label} {state_text} step={step}   {tick - begun:.2f}s\n{detail}"
                    frames.append((tick, Image.fromarray(picture).copy(), caption))
                trace.write(
                    json.dumps(
                        dict(
                            t=tick,
                            step=step,
                            active=active,
                            effect=bool(effect),
                            ok=bool(ok),
                            ee=pos[:3].tolist(),
                            phase=phase,
                            **evidence,
                        )
                    )
                    + "\n"
                )
                if t1 or elapsed >= 10 or tick - begun > 18:
                    break
                last_active = active
                time.sleep(max(0, plant.step_period - (time.monotonic() - tick)))
    finally:
        if process:
            client.sendto(b"stop", address)
            process.join(2)
            server.close()
            relay.close()
            client.close()
        if publisher:
            publisher.close()
        if observer:
            observer.close()
        plant.close()
    if video is not None:
        save_video(frames, video)
    latency = t1 - t0 if t1 is not None else None
    return dict(
        camera_fps=(len(frames) - 1) / (frames[-1][0] - frames[0][0])
        if len(frames) > 1
        else None,
        sample=label,
        kind=kind,
        parameter=parameter,
        t0=t0,
        t1=t1,
        effect=bool(effect),
        seconds=latency,
        passed=latency is not None and latency < 10,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kind", choices=CASES, action="append")
    parser.add_argument(
        "--record-video",
        action="store_true",
        help="Write H.264 MP4 per sample under videos/",
    )
    parser.add_argument(
        "--hd", action="store_true", help="Render each control step at 1280x720 via EGL"
    )
    args = parser.parse_args()
    if args.record_video and shutil.which("ffmpeg") is None:
        parser.error("--record-video requires ffmpeg on PATH")
    args.output.mkdir(parents=True, exist_ok=False)
    videos = args.output / "videos"
    if args.record_video:
        videos.mkdir()
    rows = []
    for i, (kind, params) in enumerate(CASES.items(), 1):
        if args.kind and kind not in args.kind:
            continue
        for j, param in enumerate(params, 1):
            label = f"D{i}-0{j}"
            path = args.output / label
            path.mkdir()
            video = videos / f"{label}.mp4" if args.record_video else None
            row = measure(kind, param, label, path, video, args.hd)
            if video is not None:
                row["video"] = str(video.relative_to(args.output))
            rows.append(row)
            print(json.dumps(row), flush=True)
            (args.output / "summary.json").write_text(json.dumps(rows, indent=2))
    raise SystemExit(0 if all(r["passed"] for r in rows) else 1)


if __name__ == "__main__":
    main()
