# T01切割与四项补充技能正常测试

Date: 2026-11-12
Related plan: ../plans/2026-11-12-luban-milestone-release.md

## Changed

新增T01离散材料连接断裂工装，刀具接触功达到材料连接断裂能后解除连接，MuJoCo继续积分碎片运动。独立验证使用刀具终点、六片实际分离间隙、峰值接触力和工具附着状态。沿用表9-13接口，不用其他技能替换。

## Files

src/luban/environments/atomic/cutting.py、cutting.xml，scripts/milestone/cut_check.py，固定32项清单及送测说明。

## Verification

本地和Linux均三次succeeded。Linux结果位于/data/Liuyu/runs/luban_fixed32_20261112/t01。将材料断裂能提高为10焦耳的反例判为失败。Ruff、mypy与diff空白检查通过。

目前G07、A09、T01、T03在Linux独立开发环境均各三次正常测试成功，总计12/12。每项验证的具体工况和模型分别记录在协议文件与对应变更记录，未据此声称全部本体或整个技能库完成验收。32项统一96次测试仍未执行。

## Remaining Work

1.8已按用户选择保留Luban+XR-1，在VLABench先导后构建指定50项对比集。API模型列表已返回HTTP200且包含用户指定模型。GPU限制为物理1、2。新VLABench源码克隆进行中，尚未运行模型回合。后续包括原有28项接入、96次统一测试、未见复合任务、1.7长程与四类扰动、完整Linux双节点部署、Gitee发布与发布后隔离复现。
