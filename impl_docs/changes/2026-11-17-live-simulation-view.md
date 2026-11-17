# 实时仿真画面与独立测试方案

日期：2026-11-17

相关计划：[实时仿真画面输出](../plans/2026-11-17-live-simulation-view.md)

## 变更内容

同步 Runtime 和 VLABench OpenVLA 对照入口可按需发送仿真观测画面。服务器仅监听本机，测试人员通过 SSH 转发在浏览器选择任务或分支并查看步数及更新状态。未启用画面功能时，执行行为保持原样。

新增独立测试方案，列出 32 项原子技能、30 项组合样本和 50 个 VLABench episode，说明扰动场景、部署及画面观看方法。

## 相关文件

实现位于 `src/luban/observability/live_view.py`、`src/luban/runtimes/sync.py` 和 `scripts/milestone/vlabench_actor_check.py`，测试位于 `tests/unit/test_live_view.py`。使用说明位于两个 README 和 `docs/milestone-2026-12.zh-CN.md`，独立方案位于 `docs/milestone-forms/`。

## 验证

画面服务的单元测试、代码格式检查和文档检查通过。真实 VLABench 场景中使用预设动作验证了连续观测画面及浏览器显示，OpenVLA 入口使用模拟接口验证了画面接入，未进行新的模型性能测量。

## 待完成工作

视觉遮挡、抓取滑落、动态障碍和多机通信丢包的新场景尚待接入及实测，其恢复时间不能从既有扰动结果推定。独立运行且未接入图像观测的原子技能工装尚未全部支持画面输出。
