# A09分离嵌合盖物理工装

Date: 2026-11-12
Related plan: ../plans/2026-11-12-luban-milestone-release.md

## Changed

新增MuJoCo嵌合盖接触工装，通过既有SkillRuntime调用归档A09原接口。控制器驱动单轴Cartesian stage，已抓持前提采用受力上限为60牛顿的约束模型，超限解除。独立验证直接读取仿真状态，检查间隙、座口接触、抓持误差、驱动力与行程。

## Files

src/luban/environments/atomic/fitted_lid.py、fitted_lid.xml，scripts/milestone/lid_check.py，requirements/atomic-fixtures.txt。同步包资源、送测清单、使用说明及实施状态。

## Verification

Mac本地Python3.11及MuJoCo3.3.1运行三次独立初始化，三次均succeeded。间隙0.028459米，峰值驱动力16.589牛顿，仿真执行时长4.621秒。0.2牛顿驱动力上限反例保持fitted且失败，0.2牛顿抓持承载反例断开附着且失败。Ruff与mypy通过。

初版摩擦夹爪模型出现滑脱，未计成功。已抓持约束工装的初版控制器存在瞬态行程超限，独立验证判失败，提高速度阻尼后通过。保存初版超行程结果、最终三次结果与两个反例至runs/milestone_a09_20261112。初步摩擦夹爪探索脚本位于/tmp/luban_lid_probe.py，未纳入交付实现。

## Remaining Work

当前仅覆盖已抓持、注册+Z轴的嵌合盖分离工况，未验证抓取建立过程、其他分离轴或全部适用本体。需要Linux复现并纳入统一96次评测。G07、T01、T03物理后端、四类扰动、复合任务、50任务对照和开源部署仍待完成。
