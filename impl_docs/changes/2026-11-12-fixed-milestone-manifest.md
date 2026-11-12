# 固定送测清单与基础依赖入口

Date: 2026-11-12
Related plan: ../plans/2026-11-12-luban-milestone-release.md

## Changed

按用户Word表9-13恢复32项固定技能及API，加入原表输入输出和适用本体。G07、A09、T01、T03恢复，移除送测清单中的四项替代技能。整体通过率分母纠正为96次调用，另要求32项各至少成功2次。缺失执行后端标为pending。

基础依赖由uv.lock导出至requirements/base.txt，增加requirements.txt及requirement.txt。更新送测说明的安装命令。按用户选择，1.7扰动采用末尾官方汇总表的目标位置突变、外力推搡、视觉遮挡、执行机构抖动。

## Files

configs/milestone/skills_32.json，requirements/base.txt，requirements.txt，requirement.txt，docs/milestone-2026-12.zh-CN.md，impl_docs/TODO.md及关联计划。

## Verification

通过uv venv --python 3.11 .venv建立本地隔离环境，uv pip install --python .venv/bin/python -r requirement.txt安装11个包成功。离线示例成功完成两步，luban health四个组件均healthy。git diff --check通过。清单读取自Word原表32行。

## Remaining Work

四项物理后端、三个API名称适配、96次实际调用、未见复合任务、正式四类扰动、50个atomic-seen对比、Linux双节点与Gitee发布后隔离复现尚未完成。本次安装验证在macOS进行，不计作6.1的Linux两次部署。
