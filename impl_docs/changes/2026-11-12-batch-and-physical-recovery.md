# VLABench并行批次与四类物理扰动恢复

Date: 2026-11-12
Related plan: ../plans/2026-11-12-luban-milestone-release.md

## Changed

增加VLABench批量入口、固定回合配置与实际结果收集。按最新要求配置GPU0、1、2各5个回合worker，总计15个。模型服务共享，Luban回合内部维持双Agent。支持接续已有结果和明确指定的运行中进程，切换调度时保留已运行回合。策略无效动作超限归类为正常任务失败，基础设施异常和缺失结果单独保留。

增加Panda动力学中的目标位置突变、外力推搡、RGB-D遮挡和执行机构抖动。Luban使用DefaultAgent、TaskSkillPlanner、EnvironmentVerifier和M12视觉伺服，Actor仅使用实际图像估计和自身状态。目标丢失时保持当前位置，大误差时采用0.12米/秒上限，接近目标后减至0.06米/秒。独立原始状态判定恢复，并保留单调时钟、连续稳定窗口、图像和轨迹。共享RGB-D相机从脚本移入可选环境层，既有原子技能复用该实现。

## Files

入口为scripts/milestone/vlabench_batch.py、vlabench_collect.py、recovery_check.py，分类修正在vlabench_dual_check.py。配置位于configs/milestone/vlabench50.json和configs/agents/recovery_visual_servo.yaml。环境为src/luban/environments/atomic/tracking_recovery.py，动作生成位于backends/skills/visual_servo.py，相机位于environments/atomic/native/rgbd_camera.py。同步更新公共说明、架构、计划和TODO。

## Verification

真实书籍任务前4个配对回合中Luban为4/4，OpenVLA为0/4，无运行异常，总墙钟227秒，其余46回合仍在运行。服务器进程快照确认19101、19102、19103三个服务各有5个回合worker，配置记录total_workers=15。

recovery_four_fast的四类物理扰动恢复分别为1.7107、7.0546、1.3578、1.9267秒，均满足扰动实际生效、连续稳定条件、无机械臂碰撞且小于10秒。40牛顿未造成超容差偏移的首轮及200牛顿慢控制器12.2768秒的失败记录保留。结果副本位于runs/recovery_four_fast_linux_20261112/summary.json。

视觉伺服遮挡保持及分段运动上限单元测试2项通过，双分支正常失败与异常分类回归测试1项通过，比较结果缺失判定测试1项通过。新动作生成、恢复环境和共享相机3个文件的focused mypy通过，相关Ruff检查通过。未重跑整个96次原子技能测试，本次控制改动属于新增恢复路径，原有限M12入口仅改用共享相机。

## Remaining Work

完成50回合配对及最终送测集合、依赖与资源获取、一键部署测试、两套独立环境、服务长测、Gitee发布与两份测评Word。历史100回合基座为零，相对增益证据仍不能由该集合给出。四类恢复证据仅覆盖上述视觉伺服配置，不扩大为XR-1长程策略恢复结果。
