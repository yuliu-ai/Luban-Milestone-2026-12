# RoboCasa365固定100回合复现清单

Date: 2026-11-12
Related plan: ../plans/2026-11-12-luban-milestone-release.md

## Changed

按照最新规模固定100个episode，覆盖16类composite_unseen任务。保留历史pretrain场景、任务、episode、种子、官方步数及双分支原步数上限，直接兼容既有run_xr1_dual.py。历史结果和新执行结果分别保存，原20回合计划取消。

## Files

配置为configs/milestone/robocasa365_unseen100.json。公开运行说明在docs/milestone-2026-12.zh-CN.md，同步更新现有计划和TODO。结果本地副本在runs/robocasa365_unseen100_history_20261112/results.json，服务器目录为/data/Liuyu/runs/luban_m17_20261112/unseen100_history。未建立筛选过程报告。

## Verification

读取服务器官方基座summary.json及双分支pair_result.json，按任务、episode和seed匹配，固定100条记录。历史基座0/100，双分支100/100，获胜分支无异常且实际步数均不超过基座预算。配置保留原双分支每分支2.5倍horizon。基座成功率为零，相对增益记为null。

只完成历史数据整理及清单交付，本轮没有启动新的100回合模型测试，不将历史结果记作当前发布版本的实测。数据和文档变更不新增实现镜像测试。另已取回30项内部新组合任务汇总，30/30通过。

`git diff --check`通过，`.venv/bin/python scripts/run_xr1_dual.py --help`正常退出，确认既有入口接受manifest、输出目录和双分支参数。100回合配置已同步到服务器开发源码目录。

## Remaining Work

第三方复现需运行固定清单并使用新产生的结果。整体1.7尚缺有效的相对增益证据及四类真实扰动测试，其他发布、部署和测评文档任务保持进行中。
