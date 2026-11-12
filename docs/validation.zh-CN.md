[English](validation.md) · [简体中文](validation.zh-CN.md)

# 验证记录

原独立准备验证于 2026-10-07–08 进行。下列为当时实际源码树的结果，父项目测试与历史
catalog 探针不作为本副本的新验收。后续命名迁移的检查在文末单独记录。

## 软件检查

| 检查 | 实际结果 |
| --- | --- |
| 新 Python 3.11.16 环境与依赖缓存 | 安装全部 extra、dev／docs／full-test |
| 完整 CPU pytest | 973 passed，0 skipped，0 failed |
| 无 Git 元数据的独立源码包环境 | 973 passed，0 skipped，0 failed |
| 较早 core-only／dev 环境 | 956 passed，14 个可选跳过；完整组消除跳过 |
| Ruff／format／严格 mypy | 通过，类型检查 163 个源码文件 |
| 配置构造 | 35 个配置，0 个硬失败，显式标记可选服务需求 |
| 全部本地 Markdown 目标与 MkDocs strict | 通过，含根目录、catalog、配置、用户／开发指南和 HTML 链接 |
| V2.1 catalog／bundle | 7236/7236、3862/3862 |
| V2.2 catalog／bundle | 7418/7418、3944/3944 |
| 核心模块、YAML CLI、health | mock 计数环境两次动作通过 |
| checkout 外 wheel 安装 | import、CLI、typed marker、Chemistry YAML／protobuf、离线模块通过 |
| 源码包构建、安装与资源 | 全部 CPU 测试、随包示例通过 |
| 锁定版本漏洞元数据 | 官方 PyPI 查询 95 个精确版本，0 条命中 |
| 凭据检查 | 提供密钥的精确匹配 0；Gitleaks 的 33 个公开 sam3_perception route-ID 误报已审阅 |
| 私人运行路径／大文件 | 0 条实际私人绝对路径，0 个超过 10 MiB 文件 |
| 历史 PNG | 检查 42 个仿真图像／mask，资源条件见第三方声明 |
| 旧个人仓库 CI | `ef4a45c` 完整测试与源码包测试各 973，零跳过；[记录](https://github.com/yuliu-ai/final-agent-github/actions/runs/37719142715) |
| 组织仓库 CI | `04c5e4b` 两套各 973，零跳过；[记录](https://github.com/Luban-Agents/Luban/actions/runs/37723603285)，较新精确提交见 [Actions](https://github.com/Luban-Agents/Luban/actions) |

测试组使用 h5py 3.16.0、Torch 2.14.1+cpu，Dashboard 为 Gradio 6.28.0。
OpenPI 固定真实 SDK 连接本地 WebSocket 对端；Chemistry 连接本地 gRPC 对端，属于软件／协议检查。
核心版本：httpx 0.28.1、AnyIO 4.15.1、NumPy 1.26.4、Pillow 12.3.0、PyYAML 6.0.3、
pytest 9.1.1、Ruff 0.15.21、mypy 1.20.2、grpcio 1.83.0、protobuf 7.35.1、pyzmq 27.1.0、
msgpack 1.2.1、MkDocs 1.6.1。可安装依赖以锁文件为准，没有修改共享 Python 环境。

后续 pip-audit 首次网络超时，之后成功查询全部锁定公开 index 版本的官方漏洞元数据；
较早的 90 包 pip-audit 通过记录单独保留。数据库检查不覆盖根项目、固定 OpenPI 源码、
CPU binary build、模型或复用的外部 GPU 栈，不能证明无漏洞。

HTTP 代理曾拦截两个 loopback WebSocket 测试，设置 `NO_PROXY=localhost,127.0.0.1,::1`
后完整套件通过。catalog 在临时副本运行，不修改历史报告。
源码包包含小型资源、政策文档与 README SVG，不包含环境、凭据、权重或数据集。
首个 hosted run 在测试前发现缺 FFmpeg，workflow 现仅在临时 runner 内安装。
之后[运行 37675885981](https://github.com/yuliu-ai/final-agent-github/actions/runs/37675885981)通过完整流程。
源码包还明确检查贡献、行为、安全与许可文档。

## 真实仿真 A：原生任务与 XR-1

配置为 `configs/runs/robocasa_simple.yaml`、`configs/agents/xr1_simple.yaml`。
任务 `CloseBlenderLid`，target、index 0、seed 7；最多 600 环境步、38 动作块、40 迭代、600 秒，
receding horizon 16。确定性 TaskSkillPlanner 选择原生指令，XR-1 生成动作，
EnvironmentVerifier 使用原生成功反馈；不调用 LLM。

```json
{
  "success": true,
  "termination_reason": "task_success",
  "environment_steps": 396,
  "action_chunks": 25,
  "exceptions": 0
}
```

session `5d67f5f7-d749-440f-98de-899082e812f8` 位于 `runs/robocasa_simple/<session_id>/`。
生成 result、两类 trace、manifest 与视频；视频完整解码，检查了第一帧三视图。
[复现命令](episodes.zh-CN.md)。

## 真实仿真 B：多模态 graph-guided 智能体

配置为 `robocasa_graph_simple.yaml`、`xr1_graph_simple.yaml`。
任务 `ScrubCuttingBoard`，target、index 0、seed 7；600 环境步、38 动作块、80 迭代、600 秒。
XR-1 使用 `task_graph` 模式，GLM-5.3-flash 提供真实图像／JSON 规划与验证。

```json
{
  "success": false,
  "termination_reason": "environment_done",
  "environment_steps": 600,
  "action_chunks": 38,
  "planner_calls": 2,
  "verifier_calls": 3,
  "exceptions": 0
}
```

两个相同 task／seed／预算的真实回合均因 600 步 horizon 结束而未完成任务。
每个回合有两次规划、三次验证、38 个动作块、Runtime 异常为 0。

| Session | 耗时 | 回合后处理 |
| --- | --- | --- |
| `7e6adb49-5182-4089-a731-0496f78a14e8` | 233.48 秒 | 最初 4096-token 反思／建议截断，`finish_reason=length`，两个更新错误 |
| `ab2b88f1-80a3-43a6-9d90-790f572fd7f9` | 345.98 秒 | 16384-token 反思通过，advisory 触及当时 120 秒期限，一个更新错误 |

目录为 `runs/robocasa_graph_simple/traces/<session_id>/`。
首个包含 199 条 Runtime trace、38 个 memory-update、Planner 输入中经 catalog 验证的单节点图、
最终 manifest 和 39 帧视频。Runtime exceptions 与回合后错误不同；前者为零不代表每阶段通过。
两个回合都未应用高层技能／模块更新。后者保留一个反思 proposal、一个 experience candidate、
一个确定性 fallback module candidate。

当前 advisory 使用最后 12 个事件、step／memory 摘要、反馈与产物引用，保持原 trace 不变。
使用第二个 session 记录证据的独立 GLM 请求：输入 12,431 bytes，输出上限 16,384 token，
期限 300 秒，耗时 79.11 秒，共 13,262 token，解析并接受四个 proposal-only 候选，应用零个，
新增环境步零个。这是组件检查，不是新的机器人回合或任务质量提升证据。
当前完整 300 秒后处理配置尚未端到端重新运行，组件与回合结果分别报告。

兼容路径关闭选择性节点调度／恢复，`task_graph_runtime` 为 null。
trace 证明图上下文、模型／策略调用、反馈与记忆更新，不证明独立节点调度部署。
未建立经验晋升、权重训练、跨本体迁移或策略提升结论。
最初 `CloseBlenderLid` 图尝试因默认目录未绑定，在动作前停止；正式入口使用已有
`ScrubCuttingBoard` 绑定。见[高级复现](advanced-episode.zh-CN.md)。

## 外部环境与验证边界

两类仿真复用 RoboCasa 1.0.1 固定源码 `456174f62b89b8fca99eaaf33949c29fec9cfc2a`、
robosuite 1.5.2、MuJoCo 3.3.1、已获许可资产和 XR-1 RoboCasa365 权重。
策略源码归档无 Git 元数据，仅保留部分指纹，没有建立与参考 revision 的完全等价或所有分片摘要。
原记录未从零安装完整 GPU 栈。策略环境为 Python 3.12.14，框架客户端为 Python 3.11.16。
各验收服务自行在空闲 GPU 0／18400 启动并在结束后关闭，未重启或给共享服务发信号。

独立 GLM 文本／mock 示例通过两次规划请求，单次图像／JSON 兼容请求也通过，共 858 token。
凭据由外部私密环境文件读入进程内存，没有把真实密钥或网关配置写入仓库。
未新增真实 LIBERO／OpenPI、RoboTwin、EB-ALFRED、Isaac Sim、SAM3／SpatialLM、实体机器人、
广泛任务矩阵、长期运行或训练。一次成功和两个 horizon 失败不是总体性能结果。

## 私密仓库与来源

交付仓库 `Luban-Agents/Luban` 由组织所有，创建和正常推送已完成，推送前后 API 均确认 Private。
一次全新认证 clone 在独立核心环境通过两个 mock 示例和 health；较早组织 CI 在 `04c5e4b`
通过 973 测试及 973 源码包测试。组织所有者管理；第二成员有明确 Read 权限，
没有 push／triage／maintain／admin，组织 base permission 为 None。

用户选择新仓库，保留个人私密仓库备份，没有迁移或强推其旧历史。
新仓库只包含审阅过的独立历史，旧 main 及祖先未纳入；个人库早期历史中的路径与硬件标识
不属于当前审阅分支。

没有发布公开 Release、Pages 或改变可见性。后续公开前核对[第三方条件](../THIRD_PARTY_NOTICES.zh-CN.md)。
访问与编辑见[指南](access.zh-CN.md)。原始安装／测试报告、依赖、扫描和运行输出保存在被忽略的
私有证据区，来源保留和精确 revision／Private 回执位于交付报告。
参见[来源](provenance.zh-CN.md)及[开发](development.zh-CN.md)。

## `luban` 包命名迁移与双语文档验证（2026-10-08）

安装包、import 命名空间和 CLI 均为 `luban`，源码位于 `src/luban`，SDK 根目录变量为
`LUBAN_ROBOCASA_ROOT`。19 组完整中英指南共用一致的可执行代码块与成对导航。
上游来源及版权归属保留准确记录。

新的独立环境按锁文件安装 all-extras／dev／docs／full-test。迁移后本地套件为 973 passed、
零跳过，lint、格式、严格类型和 35 个配置构造均通过。
GPU 客户端在本任务拥有的仿真环境中重装为 `luban` 软件包，策略环境与权重仍只读复用。
启动前检查了 GPU 4 和 loopback 18408。

使用已有 CloseBlenderLid 配置：target、index 0、seed 7，最多 600 环境步、38 动作块、600 秒。
结果为原生 `task_success`，352 环境步、22 个动作块、0 异常。
session 为 `18c04c6f-a2f8-41ae-9bfa-3bef5ff93773`，在 `runs/robocasa_simple/` 下。
生成结果、两类 trace、manifest 和视频。验收后只关闭自己启动的策略服务，没有修改共享环境或原始源码。

独立分发包／资源检查和最终精确提交的 GitHub CI 保存在本次迁移交付证据中。
上方历史运行保留其原日期和身份，不当作新迁移运行。
