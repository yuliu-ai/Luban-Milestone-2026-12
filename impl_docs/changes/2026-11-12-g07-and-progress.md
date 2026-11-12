# G07吸附执行与逐项终端结果

Date: 2026-11-12
Related plan: ../plans/2026-11-12-luban-milestone-release.md

## Changed

新增G07吸盘接触耦合气腔模型，压差产生相互作用力，使用关闭泵后的压力变化测量漏气率，复用原表接口与SkillRuntime。附加承载检查在技能完成后单独移动吸盘，观察物体是否随动。压力为模拟气腔状态，未声称硬件压力测量。

G07、A09、T03入口逐次打印技能在表9-13中的序号、当前轮次、结果及最终汇总。1.8报告器新增逐任务方法结果、仅本方法成功及仅基线成功清单。缺失记录标未完成，避免把没有测试的任务记作模型执行失败。双方未完成全部50项时不能判定通过。

## Files

src/luban/environments/atomic/suction.py与suction.xml，scripts/milestone/suction_check.py、progress.py、lid_check.py、pour_check.py、benchmark_compare.py，tests/unit/test_milestone_comparison.py及清单和送测说明。

## Verification

Linux独立环境运行suction_check.py三次成功，三次附加承载检查通过，物体抬升约9.93毫米。证据位于guo-4090的/data/Liuyu/runs/luban_fixed32_20261112/g07。Mac三次成功，真空度51.088kPa，关闭泵后漏气率0.511kPa/s，目标50kPa和2kPa/s上限均满足。漏气反例24.925kPa/s，判为失败。

报告器回归测试通过，覆盖50项全部缺失不得通过，以及完整配对结果中单独列出OpenVLA失败、Luban成功任务。此测试使用合成结果行，未运行模型，不是指标1.8实测数据。Ruff与G07模块mypy通过。

## Remaining Work

T01物理切割、32项统一运行、复合任务、1.7长程及官方四类扰动、1.8正式对照、Gitee发布及发布后双节点复现待完成。用户将1.7指定到RoboCasa365。1.8仍需选择双方策略与基准，不能直接把已有OpenVLA-OFT或官方LIBERO数字当作本次50项结果。
