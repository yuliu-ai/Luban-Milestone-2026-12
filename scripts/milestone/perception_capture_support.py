import numpy as np

from luban.environments.atomic.native.simulator_reference import matrix_quaternion


def geom_mask(simulator, segmentation, root_body):
    model = simulator.sim.model
    root = model.body_name2id(root_body)
    bodies = {root}
    for body in range(model.nbody):
        parent = body
        while parent and parent not in bodies:
            parent = int(model.body_parentid[parent])
        if parent in bodies:
            bodies.add(body)
    geoms = [
        index for index in range(model.ngeom) if int(model.geom_bodyid[index]) in bodies
    ]
    return np.isin(segmentation[:, :, 1], geoms) & (segmentation[:, :, 0] == 5), geoms


def rgb_segmentation(session, camera):
    simulator = session.simulator
    raw = simulator.sim.render(width=256, height=256, camera_name=camera)
    rgb = np.asarray(session.observation["video." + camera])
    if rgb.ndim == 4:
        rgb = rgb[-1]
    errors = [
        float(np.mean(np.abs(raw.astype(float) - rgb))),
        float(np.mean(np.abs(raw[::-1].astype(float) - rgb))),
    ]
    flipped = errors[1] < errors[0]
    if min(errors) > 1:
        raise ValueError("Raw RGB cannot be aligned to the SDK observation")
    segmentation = simulator.sim.render(
        width=256, height=256, camera_name=camera, segmentation=True
    )
    if flipped:
        segmentation = segmentation[::-1]
    return (
        rgb,
        segmentation,
        {"raw_render_flipped": flipped, "rgb_alignment_mae": min(errors)},
    )


def bounding_box(mask):
    y, x = np.nonzero(mask)
    if not len(x):
        raise ValueError("Ground-truth object is not visible in this view")
    return [int(x.min()), int(y.min()), int(x.max() + 1), int(y.max() + 1)]


def aim(session, target, eye, camera="robot0_agentview_left"):
    m, d = session.simulator.sim.model, session.simulator.sim.data
    ci = m.camera_name2id(camera)
    parent = int(m.cam_bodyid[ci])
    R = np.asarray(d.xmat[parent]).reshape(3, 3)
    back = np.asarray(eye) - target
    back /= np.linalg.norm(back)
    right = np.cross([0, 0, 1], back)
    right /= np.linalg.norm(right)
    up = np.cross(back, right)
    local = R.T @ np.column_stack([right, up, back])
    q = matrix_quaternion(local)
    m.cam_pos[ci] = R.T @ (np.asarray(eye) - d.xpos[parent])
    m.cam_quat[ci] = [q[3], *q[:3]]
    m.cam_fovy[ci] = 45
    session.simulator.sim.forward()


def capture(session):
    sim = session.simulator.sim
    return sim.render(width=512, height=512, camera_name="robot0_agentview_left")[
        ::-1
    ], sim.render(
        width=512, height=512, camera_name="robot0_agentview_left", segmentation=True
    )[::-1]
