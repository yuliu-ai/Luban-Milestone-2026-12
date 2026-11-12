# 64 项技能进度总览

目录版本 `2.2.0` ｜ 生成方式：`python3 add_skill_progress.py`（版本号由 `maturity` 推导，勿手改）

## 版本号与阶段对照

| 版本 | 阶段 | 含义 |
|---|---|---|
| `0.1.0` | `declared` | 只有契约定义 |
| `0.2.0` | `backend_bound` | 有可运行后端 |
| `0.3.0` | `verified` | 有可独立复核的证据 |
| `0.4.0` | `evolved` | 有经配对验证的候选 |
| `1.0.0` | `promoted` | 候选已批准为执行版本 |

## 阶段分布

| 阶段 | 项数 |
|---|---|
| `declared` | 55 |
| `backend_bound` | 1 |
| `verified` | 8 |
| `evolved` | 0 |
| `promoted` | 0 |

## 全部技能

| ID | 名称 | scope | 版本 | 阶段 | 最近更新 | 后端 | 下一步 / 卡点 |
|---|---|---|---|---|---|---|---|
| A01 | 移动直线关节对象 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| A02 | 转动旋转关节对象 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| A03 | 旋转控制件 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| A04 | 按压按钮 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| A05 | 离散开关挡位调节 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| A07 | 机械卡扣状态调节 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| F01 | 多旋翼受限垂直起飞 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| F02 | 指定区域受限垂直降落 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| F03 | 有限时域位置悬停 | `closed_loop_hold` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| F04 | 单一局部航点飞行 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| F05 | 三维有限轨迹段跟随 | `closed_loop_hold` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| F06 | 空中定点偏航调整 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| F07 | 有限航段离地高度跟随 | `closed_loop_hold` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| F08 | 空中受控制动至保持 | `closed_loop_hold` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| G01 | 平行夹爪定宽调节 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| G03 | 平行夹爪力控抓取 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| G04 | 灵巧手关节定位 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| G05 | 灵巧手参数化抓取闭合 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| G07 | 吸盘吸附 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| G08 | 吸盘解吸 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| G09 | 定时抓取力调节 | `closed_loop_hold` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| G10 | 末端执行器定时保持 | `closed_loop_hold` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| I01 | 物体直线推动 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| I02 | 物体直线拉动 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| I04 | 物体平面旋转 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| I06 | 被持物体竖直移动 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| I08 | 被持物体落座 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| I09 | 被持物体位姿对准 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| I10 | 被持物体直线插入 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| I11 | 被持物体直线拔出 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| I12 | 被持物体翻转 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| M01 | 末端参数化直线移动 | `actuation_segment` | `0.3.0` | `verified` | 2026-09-13 | robocasa365 真实执行（gym.make + OSC_POSE delta 闭环，simulator env python）+ sam3（独立视觉核验） | 在固定 identity 下做配对实验，达标后升 evolved（0.4.0） |
| M02 | 末端有限航点路径移动 | `actuation_segment` | `0.3.0` | `verified` | 2026-09-13 | robocasa365 真实执行（gym.make + OSC_POSE delta 闭环，simulator env python）+ sam3（独立视觉核验） | 在固定 identity 下做配对实验，达标后升 evolved（0.4.0） |
| M03 | 末端原位旋转 | `actuation_segment` | `0.3.0` | `verified` | 2026-09-13 | robocasa365 真实执行（gym.make + OSC_POSE delta 闭环，simulator env python）+ sam3（独立视觉核验） | 在固定 identity 下做配对实验，达标后升 evolved（0.4.0） |
| M04 | 末端位姿移动 | `actuation_segment` | `0.3.0` | `verified` | 2026-09-13 | robocasa365 真实执行（gym.make + OSC_POSE delta 闭环，simulator env python）+ sam3（独立视觉核验） | 在固定 identity 下做配对实验，达标后升 evolved（0.4.0） |
| M05 | 机械臂关节移动 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| M06 | 末端定时保持 | `closed_loop_hold` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| M08 | 移动底盘直线移动 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| M09 | 移动底盘原地旋转 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| M10 | 机器人受控停止 | `closed_loop_hold` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| M11 | 双臂同步位姿移动 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| M12 | 有限时域视觉伺服 | `closed_loop_hold` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| N01 | 有限窗口机器人自主定位 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| N02 | 局部占据空间快照生成 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| N03 | 地面可通行区与空中净空区估计 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| N04 | 候选着陆区几何观测 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| N05 | 地面有限路径段跟随 | `closed_loop_hold` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| N06 | 底盘局部对接位姿对准 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| P01 | 物体检测 | `perception_observation` | `0.3.0` | `verified` | 2026-09-12 | sam3（GPU 推理，端口 18200）+ molmo2（第二 provider，端口 18300） | 在固定 identity 下做配对实验，达标后升 evolved（0.4.0） |
| P02 | 实例分割 | `perception_observation` | `0.3.0` | `verified` | 2026-09-13 | sam3（GPU 推理 + --mask-out-dir 掩码落盘，端口 18200） | 在固定 identity 下做配对实验，达标后升 evolved（0.4.0） |
| P03 | 物体六维位姿估计 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| P04 | 场景深度估计 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| P05 | 有限时域目标跟踪 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| P06 | 物体属性识别 | `perception_observation` | `0.3.0` | `verified` | 2026-09-13 | molmo2（VLM VQA 问答分支，task.mode=vqa） | 在固定 identity 下做配对实验，达标后升 evolved（0.4.0） |
| P07 | 可操作性估计 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| P08 | 可动关节估计 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| P09 | 对象状态估计 | `perception_observation` | `0.2.0` | `backend_bound` | 2026-09-13 | molmo2（VLM VQA 问答分支，task.mode=vqa，与 P06 同机制） | 环境真值交叉核验两轮诚实失败（2026-09-13）：v1 裁剪仅得 9px 条带；v2 定位根因——singlecabinet_1 门铰链（sim qpos 真值可用）body 原点投影 u≈-466 不在 agentview_left 视野内，59 个 cabinet 检测歧义致 Molmo2 答非所问（双态样本判反、passed=false；留档 runs/p09_groundtruth_v1/、runs/p09_groundtruth/）；下一步：选视野内（≥30px 余量）关节重做或换 eye-in-hand 相机 |
| P10 | 力与力矩读取 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| P11 | 接触检测 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| P12 | 机器人状态读取 | `perception_observation` | `0.3.0` | `verified` | 2026-09-13 | robocasa365 真实观测（gym.make，simulator env python）+ sam3（独立视觉核验） | 在固定 identity 下做配对实验，达标后升 evolved（0.4.0） |
| T04 | 工具单次擦拭行程 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| T08 | 执行器定量分配 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| U01 | 观测云台姿态调节 | `actuation_segment` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
| U02 | 单次位姿关联图像采集 | `perception_observation` | `0.1.0` | `declared` | — | — | 绑定可运行后端（执行器 + 验证器）后升 backend_bound（0.2.0） |
