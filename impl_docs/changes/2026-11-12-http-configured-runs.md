# HTTP运行配置入口

Date: 2026-11-12
Related plan: ../plans/2026-11-12-luban-milestone-release.md

## Changed

新增`luban serve --run-config NAME=PATH`和`POST /v1/runs`，网络请求按名称选择服务启动时登记的配置，复用原CLI运行流程。任务成功、失败和轨迹路径来自实际Runtime结果。运行请求串行执行，双分支协调仍由原脚本负责。

源码分发包补入requirement.txt和requirements.txt。同步送测文档的HTTP调用方式和架构说明。

## Files

src/luban/service.py，src/luban/cli.py，tests/unit/test_service.py，MANIFEST.in，docs/milestone-2026-12.zh-CN.md，impl_docs/architecture/overview.md及任务计划。

## Verification

`pytest -q tests/unit/test_service.py`最终12项通过。新增集成用例通过真实HTTP请求加载独立临时配置，分别验证一步失败及三步预算成功，确认轨迹文件保存，并验证客户端不能指定本地路径或覆盖任务。Ruff格式与静态检查通过，mypy检查两个修改模块无错误。

首次运行旧超大请求测试出现客户端Broken pipe，原因是客户端上传期间服务已按Content-Length提前拒绝。测试改为发送超限声明并直接读取响应，验证413语义。未宣称任意HTTP客户端在上传期间都能取得413响应。

## Remaining Work

配置HTTP入口已通过CPU集成验证。32项技能物理后端、实际仿真网络运行、官方四类扰动、50任务对照、双节点部署、Gitee发布及发布后隔离复现待完成。现有双分支脚本尚未通过本接口执行。
