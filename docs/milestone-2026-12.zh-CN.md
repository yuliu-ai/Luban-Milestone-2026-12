# 2026 年 12 月里程碑：送测与复现说明

本文对应《具有环境任务泛化能力的通才智能体 2026 年 12 月里程碑第三方测试方案》中由本仓库承担的四项指标：1.2（原子技能库）、1.7（Planner-Verifier-Actor 通才框架）、1.8（对标 OpenVLA）和 6.1（开源发布与平台集成）。每项给出被测对象、判定口径、执行命令和结果文件。命令均在仓库根目录执行。

| 指标 | 用例 | 本仓库提供的入口 | 结果文件 |
|---|---|---|---|
| 1.2 单技能调用 | TC-102-A | [`configs/milestone/skills_32.json`](../configs/milestone/skills_32.json) | 每次调用的 `NN_<技能>.json` |
| 1.2 零样本组合泛化 | TC-102-B | 本文第 1.2 节协议 | 任务级判定表 |
| 1.7 演化增益 | TC-107-A | `scripts/run_xr1_dual.py` | `pair_result.json` |
| 1.7 扰动自愈 | TC-107-B | `scripts/milestone/recovery_check.py` | 各类 `physics/recovery.json` |
| 1.7 / 6.1 开源与部署 | TC-107-B、TC-601-A | `scripts/milestone/deploy_check.sh` | `deploy_result.json` |
| 1.8 对标 OpenVLA | TC-108-A | `scripts/milestone/benchmark_compare.py` | `comparison.md` |
| 6.1 平台调用与耗时 | TC-601-B | `luban serve` + `scripts/milestone/platform_check.py latency` | `latency.md` |
| 6.1 连续运行与异常 | TC-601-C | `scripts/milestone/platform_check.py soak / faults` | `soak.md`、`faults.md` |

公开源码位于 [Luban-Milestone-2026-12](https://github.com/yuliu-ai/Luban-Milestone-2026-12)，使用独立仓库的 `main` 分支。先获取源码，再执行下方部署和测试命令：

```bash
git clone https://github.com/yuliu-ai/Luban-Milestone-2026-12.git
cd Luban-Milestone-2026-12
```

## 核心服务依赖安装

使用Python 3.11，在仓库根目录执行以下命令。`requirements.txt`固定框架核心依赖，`requirement.txt`提供相同安装入口。模型权重与仿真后端按[环境安装与资源](installation.zh-CN.md)分别准备。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirement.txt
.venv/bin/python -m luban.examples.offline --output-dir runs/install_check
.venv/bin/luban health --agent-config configs/agents/offline.yaml
.venv/bin/luban serve --host 0.0.0.0 --port 8765
```

服务运行后，在另一个终端发出请求：

```bash
curl --fail http://127.0.0.1:8765/health
curl --fail -H 'Content-Type: application/json' \
  -d '{"task":{"name":"increment"},"max_steps":3}' \
  http://127.0.0.1:8765/v1/episodes/run
```

此命令验证核心离线闭环和HTTP接口。32项物理技能、XR-1及OpenVLA需要各自的运行资源与测试入口。

## 全套环境部署和一键测试

完整评测使用Linux、兼容CUDA13.0的NVIDIA驱动和三张GPU。本次模型环境分别使用CUDA13.0、12.8和12.1的Torch轮子。仿真运行需要系统提供EGL、OSMesa、OpenGL及FFmpeg，Ubuntu可由管理员安装 `python3-venv libegl1 libosmesa6 libgl1 libglib2.0-0 ffmpeg`。数据目录应位于容量充足的磁盘。SAM3权重需要先在模型站点获得访问权限，并通过 `HF_TOKEN` 或已登录的下载客户端读取授权。

```bash
python3 scripts/milestone/bootstrap.py --root /data/luban-eval
```

部署程序逐阶段创建六套独立环境，分别用于原生技能、RoboCasa、VLABench、XR-1、OpenVLA及感知模型。依赖文件位于 `requirements/milestone-*.txt`，原生环境使用 `requirements/atomic-native-lock.txt`。模型版本的Torch与Transformers相互隔离。程序获取SDK、仿真资产和六项模型资源，打印阶段进度并保存安装日志，完成后生成 `/data/luban-eval/deployment.json`。仅准备CPU原生路径时可追加 `--profiles native`。六环境部署与完整测试入口已通过下文所列Linux隔离复现。

RoboCasa当前上游主分支的包版本为1.0.1，v1.0标签仍是1.0.0。部署采用主分支源码，robosuite采用v1.5.2。VLABench、SAM3与RRT仓库没有可用发布标签，下载入口分别使用main、main和develop。源码保留在部署目录的 `sdk/` 中，不会在测试时自动更新。模型来源固定为XiaomiRobotics的RoboCasa365与VLABench权重、VLABench的OpenVLA参考权重、Qwen3-VL-8B-Instruct、SAM3和Depth-Anything-V2-Metric-Indoor-Small-hf。

已有资源的机器可用 `--reuse-resources existing_resources.json` 指定SDK根目录及模型路径，解释器环境仍会独立创建。该JSON中的 `models` 接受 `xr1_robocasa`、`xr1_vlabench`、`openvla`、`qwen3vl`、`sam3`、`metric_depth`，其中SAM3填写 `sam3.pt` 文件路径，其余填写模型目录。SDK路径键为 `robocasa_root`、`robosuite_root`、`vlabench_root`、`rrt_root` 和可选 `sam3_root`。需要复用已安装Python解释器时，`interpreters` 按 `3.10`、`3.11`、`3.12` 映射到解释器路径。资源复用不会共享虚拟环境的site-packages。

先在当前终端设置 `OPENAI_API_KEY`、`LUBAN_API_BASE_URL`、`LUBAN_LLM_MODEL`，然后运行：

```bash
/data/luban-eval/envs/native/bin/python scripts/milestone/run_suite.py \
  --deployment /data/luban-eval/deployment.json \
  --output runs/milestone_full \
  --gpus 0,1,2 --workers-per-gpu 5
```

程序依次执行固定32项技能的96次调用、30项新组合、四类扰动、RoboCasa100个配对回合、VLABench50个配对回合及HTTP服务测试。每组会输出逐项进度和最终判定，顶层 `summary.json` 汇总各组结果。模型服务按阶段启动与关闭，只清理该命令创建的进程。长程对比每卡5个回合worker，VLABench三个策略服务各5个worker，Luban回合内部使用两个Agent分支。资源监测记录CPU、负载及可用内存。

可通过 `--suite recovery`、`--suite robocasa100` 等选项单独执行一组，也可一次指定多组。允许值为 `atomic32 unseen30 recovery robocasa100 vlabench50 service`。输出目录每次使用新路径。服务组执行100次调用、异常用例及3700秒稳定性测试。默认全套测试的服务调用来自本机，跨机器接入耗时应另外指定真实调用地址。

## 1.2 通用具身原子技能库

### 单技能调用通过率（TC-102-A）

送测清单为 [`configs/milestone/skills_32.json`](../configs/milestone/skills_32.json)，严格对应固定表9-13的六类32项，包含原表API、输入输出和适用本体。每项独立执行3次，至少2次执行成功且独立验证通过，全部32项均需满足单项判据。整体调用通过率 `P_skill = 成功调用次数 / 96`，要求至少90%。失败、超时和异常均计入分母。

G07吸盘吸附、A09分离嵌合盖、T01工具单次切割行程及T03定量倾倒均已提供仿真工装入口，Linux每项三次正常用例通过，验证范围见下文。四项模型感知、两项SDK运动和M12视觉伺服也已各三次通过，整套命令已完成统一复测，96/96成功，32项均3/3。G01、I06和A05采用原表API `open_parallel_gripper`、`lift_held_object`、`toggle_lever`，原生适配器已落实张开、抬升和旋转拨杆语义，25项原生技能在Linux三次测试共75/75通过。原四项替代技能不计入表9-13。

技能运行环境分三类：`native`（26项PyBullet / MuJoCo物理技能，CPU即可）、`robocasa`（M01、M03，使用独立RoboCasa解释器）和 `model`（P01、P02、P04、P09，需要 Qwen3-VL-8B-Instruct、SAM3 与米制深度模型服务，单卡约 40 GB 空闲显存）。

### 零样本组合泛化成功率（TC-102-B）

内部固定清单为 [`unseen30_tasks.json`](../configs/milestone/unseen30_tasks.json)，包含15项机械臂操作组合和15项多段底盘移动组合，均使用固定32项中的技能。任务改变物体质量、摩擦、场景布局、目标位移或移动距离。规划器只接收目标及可用技能目录，不接收参考序列或评判字段，不做额外微调。生成的序列须与已登记25组组合程序不同，每项单次规划，随后在连续物理环境中执行，步骤之间不重置。

每一步必须通过技能验证，最终还需满足独立原始物理状态判据，包括位姿、抬升、落座、释放、顺序、停稳或碰撞条件。当前内部固定任务集结果为30/30，成功率100%。这批记录属于内部验证，第三方现场封存盲测应单独记录。2026-11-04旧九项组合的原始结果为3/9。

## 1.7 Planner-Verifier-Actor 通才框架

### 发布形态

本版本以“双分支”智能体作为发布形态。每个回合并行运行两个独立的 Planner-Verifier-Actor 分支：任务分解器生成任务图，Planner 每 8 个动作块根据多视角图像、上一次规划和 Verifier 结论更新给策略的语言指令，Verifier 比较执行前后视图并判定 `completed / in_progress / failed / uncertain`，Actor 为外部 VLA 策略（参考配置为 XR-1）。两个分支共享仿真种子、使用不同的策略随机种子，先由环境判定成功的分支结束另一个。分支数可设为 1。

```bash
export LUBAN_API_BASE_URL=...   # OpenAI 兼容接口
export LUBAN_LLM_MODEL=...      # 规划与验证所用模型
python scripts/run_xr1_dual.py --manifest manifest.json --output-dir runs/dual --port 18400 --gpu 1 --branches 2
```

智能体配置见 [`configs/agents/robocasa365_dual_branch.yaml`](../configs/agents/robocasa365_dual_branch.yaml)，用法细节见 [episodes](episodes.zh-CN.md)。

### 任务分解与执行演化增益（TC-107-A）

固定清单包含100个回合，见 [`robocasa365_unseen100.json`](../configs/milestone/robocasa365_unseen100.json)，覆盖官方 `target50` 中全部16类 `composite_unseen` 任务。unseen 指任务类别，环境采用RoboCasa 1.0.1的 `pretrain` 场景。双方使用相同任务、初始种子和官方每分支步数上限。基座直接执行原始任务指令，Luban采用两个Planner-Verifier-Actor分支，分支0与基座的策略种子推进方式对齐。

```bash
python scripts/milestone/robocasa_batch.py \
  --manifest configs/milestone/robocasa365_unseen100.json \
  --output runs/robocasa365_unseen100 \
  --gpus 0,1,2 --ports 18410,18411,18412 --workers-per-gpu 5
```

该命令在已启动三个XR-1服务时执行双方的100个回合。全套测试入口会自动启动服务。每个回合保存双方结果与 `comparison.json`，整个批次保存 `paired_results.jsonl` 和 `summary.json`。基础设施异常单独统计，相对增益在100对结果完整后计算。

公开仓库源码已在隔离环境完成该固定清单的双方重新执行，实测基座43/100、Luban53/100，100对结果完整，配对异常为0，整批耗时6966.17秒。成功率提高10个百分点。按评测书 `E_coll = (SR_闭环 − SR_基准) / SR_基准` 计算，相对增益为23.26%，达到不低于3%的要求。结果描述本次指定100回合，原始配对结果与轨迹保留在运行目录中。

### 扰动自愈（TC-107-B）

按末尾官方汇总表执行四类动态扰动：目标位置突变、外力推搡、视觉偶发遮挡、执行机构抖动。计时从扰动发生开始，到系统重新恢复稳定执行状态结束，各类均要求 `T_recov < 10 s`。记录实际环境状态、注入时刻、恢复时刻和持续稳定窗口。

对应末尾四类扰动的物理入口为 `scripts/milestone/recovery_check.py`，配置为 [`recovery_visual_servo.yaml`](../configs/agents/recovery_visual_servo.yaml)。它使用Luban DefaultAgent、TaskSkillPlanner、EnvironmentVerifier和M12视觉伺服，在PyBullet Panda电机动力学环境中运行。Actor依据RGB-D标记估计和机械臂自身状态生成动作，独立判据使用原始物体位姿和末端FK。该配置验证视觉伺服路径的恢复能力，XR-1长程策略使用上一节的独立入口。

```bash
python -m pip install -r requirements/atomic-native.txt
python scripts/milestone/recovery_check.py --output runs/recovery_four
```

四类注入分别为目标真实位移25毫米、末端受200牛顿外力0.2仿真秒、RGB-D遮挡0.3仿真秒，以及幅值20毫米、频率3Hz、持续0.5仿真秒的执行目标抖动。注入前先完成稳定跟踪。恢复要求位置误差不超过4毫米、姿态误差不超过0.07弧度、末端速度不超过0.015米/秒，并持续稳定0.5仿真秒，且无外部机械臂碰撞。恢复耗时用单调时钟从实际注入开始测量，另检查扰动确实造成了超容差偏移或触发遮挡保持。

本轮公开源码Linux复现的四类结果依次为1.728、6.684、1.308、1.835秒，均小于10秒。命令逐类输出进度，保存 `summary.json`、各类的 `physics/recovery.json`、物理轨迹和图像。旧 `fast_recovery.py` 与 `evals/disturbance.py` 中的滑落、运动受阻、通信丢包命令属于另一组用例。

### 开源匿名拉取（TC-107-B 第 3 项）

```bash
bash scripts/milestone/deploy_check.sh https://github.com/yuliu-ai/Luban-Milestone-2026-12.git /tmp/luban_deploy main
```

脚本禁用凭据提示后匿名克隆，执行 `uv sync --frozen`、离线最小入口、健康检查、核心单元测试和服务调用，输出 `deploy_result.json`。

## 1.8 对标 OpenVLA

采用VLABench比较Luban + XR-1与OpenVLA，固定50个回合。当前 [`vlabench50.json`](../configs/milestone/vlabench50.json) 包含官方第一轨道的46个书籍选择回合与4个水果选择回合，每回合200步。两个方法使用相同清单、初态和步数限制，程序按清单实际执行50个回合。

新版固定50回合清单正在修订，修订后将重新执行双方。上一版公开源码隔离复现中，Luban成功44/50（88%），OpenVLA成功0/50（0%），双方各50个回合完整，运行异常为0。对照权重为 [VLABench/openvla-lora](https://huggingface.co/VLABench/openvla-lora)，官方模型卡注明这版参考权重的成功率接近零。结果对应本文固定50回合及所用权重。

在准备好VLABench SDK、资源、两个模型的推理服务和语言模型环境变量后，可执行以下命令。`--sdk-root`填写SDK所在目录。示例中XR-1服务在GPU1，两个相同OpenVLA服务分别在GPU2、GPU0，三个服务各5个回合worker，总计15个。模型权重由服务共享，Luban每个回合worker内部保留两个Agent分支。

```bash
python scripts/milestone/vlabench_batch.py \
  --manifest configs/milestone/vlabench50.json --sdk-root <VLABench源码目录> \
  --output runs/vlabench50 --async-language --workers-per-service 5 \
  --xr1-url http://127.0.0.1:19101 \
  --openvla-url http://127.0.0.1:19102 --openvla-url http://127.0.0.1:19103
python scripts/milestone/vlabench_collect.py \
  --manifest configs/milestone/vlabench50.json \
  --run-dir runs/vlabench50 --output runs/vlabench50/collected
```

程序输出每个方法的回合结果、已完成数量、配对成败和总汇。`--resume`保留既有回合结果，原运行目录和清单须相同。基础设施异常与正常策略失败分别统计，达到无效动作次数上限属于策略执行失败。收集命令可多次指定 `--run-dir`，合并明确分开的批次，同一回合出现多个结果时要求明确选用的来源。

已有比较工具读取清单与两份逐回合结果，必须显式传入本次清单。以下命令用于已有结果汇总，不能代替实际模型运行。

```bash
python scripts/milestone/benchmark_compare.py compare \
  --manifest <50回合清单.json> \
  --test <Luban逐回合结果.jsonl> --baseline <OpenVLA逐回合结果.jsonl> \
  --test-name "Luban + XR-1" --baseline-name "OpenVLA"
```

输出标题为指定50个episode对比结果。缺失记录和基础设施错误显示为未完成，不能记作模型正常执行失败，只有50对完整结果才能判定该项通过。RoboCasa365用于1.7长程复合任务，不再作为本次1.8的送测基准。

## 6.1 开源发布与平台集成

`luban serve` 以标准库 HTTP 服务对外提供核心功能，不需要 GPU 和大模型：

| 接口 | 作用 |
|---|---|
| `GET /health` | 存活、版本、累计成功与拒绝请求数 |
| `POST /v1/episodes/run` | 在内置离线环境上运行一次完整的 Planner-Verifier-Actor 回合 |
| `POST /v1/task-graph/validate` | 校验任务图，可选自动修复 |
| `POST /v1/runs` | 执行启动时登记的运行配置，返回实际结果并保留轨迹 |

```bash
uv sync --frozen
.venv/bin/luban serve --host 0.0.0.0 --port 8765
# 平台接入后：100 次正常调用，配对比较平台调用与直连调用
python scripts/milestone/platform_check.py latency --platform-url http://<平台地址> --direct-url http://<直连地址>:8765
# 连续运行 ≥ 1 小时、≥ 100 次请求
python scripts/milestone/platform_check.py soak --url http://<平台地址> --seconds 3700
# 关键异常注入与安全测试
python scripts/milestone/platform_check.py faults --url http://<直连地址>:8765
```

判据：`SR_call ≥ 95%`、平台接入增加耗时 P95 ≤ 1 s；连续运行期间进程不重启、成功率 ≥ 95%；空请求、非 JSON、未知字段、类型错误、超范围参数、超大请求体、畸形与有环任务图、未知路径均返回 4xx，且之后服务仍可正常调用。本次按已确认方案在同一Linux服务器建立两套独立源码目录及虚拟环境，分别记录部署结果。公开GitHub仓库已在服务器匿名下载，两套六环境安装与全部六组复现均已通过。

### 通过HTTP执行已登记任务

服务器启动时以名称登记运行配置，客户端发送该名称。配置指定Agent、Pipeline、Runtime、Environment和任务，运行限制及输出目录沿用配置。服务端串行执行这些请求，避免同时占用同一仿真资源。双分支协调仍由原双分支入口负责，此接口执行常规运行配置。

```bash
.venv/bin/luban serve --host 0.0.0.0 --port 8765 \
  --run-config offline=configs/runs/offline.yaml
```

另一个终端调用：

```bash
curl --fail -H 'Content-Type: application/json' \
  -d '{"config":"offline"}' http://127.0.0.1:8765/v1/runs
```

响应的`result.success`为任务结果，`result.trace_path`指向服务器保留的轨迹。HTTP 200表示请求已执行，任务可能成功或失败，调用方应读取`result.success`。部署仿真任务时，将登记项替换成已准备资源的运行YAML，并在对应仿真环境启动服务。多个名称可通过重复`--run-config NAME=PATH`登记，网络请求不能指定本地路径或覆盖配置。

### A09分离嵌合盖物理工装

```bash
.venv/bin/python -m pip install -r requirements/atomic-fixtures.txt
.venv/bin/python scripts/milestone/lid_check.py --output runs/a09_three_trials --repeats 3
```

工装使用MuJoCo 3.3.1模拟盖体与嵌合座接触，沿+Z轴施力。输入前提为盖体已被抓持，抓持用带承载上限的约束模型表达，超限即断开。该工装验证分离过程，不验证手指建立抓持过程。独立验证读取盖体几何最低点与座口间隙、接触状态、抓持约束和实际驱动力，要求间隙至少5毫米并保持0.2仿真秒。力限和行程按输入检查。结果包含每次执行的result.json、protocol.json和完整physics_trajectory.json，duration_s为仿真时间。

本地三次执行通过，另测0.2牛顿低力限及0.2牛顿抓持承载，均按预期失败。其他分离轴与本体尚未实测，不计入已验证范围。

### T03定量倾倒物理工装

```bash
.venv/bin/python -m pip install -r requirements/atomic-fixtures.txt
.venv/bin/python scripts/milestone/pour_check.py --output runs/t03_three_trials --repeats 3
```

工装模拟附着于倾倒台的源容器和接收容器，介质为24颗各1克的刚性颗粒。控制器依据已离开源容器的质量及出料口附近物料预测制动期间的流量，以0.1弧度每秒倾倒、1弧度每秒回正。独立验证读取接收区域内颗粒质量、洒漏量、剩余量和真实倾角，并检查归零及沉降。固定送测目标为8克，要求实际接收8克、洒漏0克。Linux独立环境中三次全部通过。

默认仿真时长30秒，包含倾倒、回正及沉降观测。每次保存result.json、protocol.json和100Hz采样轨迹。duration_s为仿真时间，当前验证颗粒介质，液体和其他本体尚未测试。开发期卡滞、超量、欠量结果独立保留，均未计作通过。

### G07吸盘吸附与终端进度

```bash
.venv/bin/python scripts/milestone/suction_check.py --output runs/g07_three_trials --repeats 3 --lift-probe
```

吸盘接触与刚体动力学使用MuJoCo，气腔使用集中参数压力模型，抽气与漏气改变气腔压力，压差乘有效面积形成吸盘与物体的相互作用力。独立验证依据模拟压力状态、关闭泵后的漏气率和压力承载状态判定。该入口验证仿真吸附，不代表硬件真空传感器测试。Linux三次全部通过，附加承载检查三次均使物体抬升约9.93毫米。增大漏气率的反例判为失败。

原子技能入口逐次输出表9-13中的技能序号、技能名、当前重复次数和成功或失败，末尾输出总次数、成功数、失败数及通过率。JSON原始结果与轨迹单独保存。模型对比工具逐任务打印方法名称及成功、失败或未完成，并分别列出仅Luban成功和仅OpenVLA成功的任务。缺失结果不会被表述为模型任务失败，也不能使整项对比判为通过。

### T01工具单次切割行程

```bash
.venv/bin/python scripts/milestone/cut_check.py --output runs/t01_three_trials --repeats 3
```

刀具固定于Cartesian stage，通过实际接触驱动材料分离。材料采用六段离散连接，刀具接触功累计达到每连接0.004焦耳时断裂，随后由MuJoCo积分碎片运动。独立验证要求刀具行程完成至少99%、全部六片与支撑材料产生至少5毫米间隙、接触力不超过输入上限、工具附着保持。该模型验证规定材料工况的切割与分离，未模拟连续材料有限元断裂。Linux三次正常测试全部成功，提高断裂能至10焦耳的反例失败。

### 原生物理技能批量测试

下面的入口覆盖固定清单中26项CPU原生物理技能，包括M12视觉伺服和新增的四项。先完成的25项三次测试75/75通过，M12随后完成3/3。M01、M03由SDK入口执行，P01、P02、P04、P09由模型入口执行，统一入口依次运行三个组。

```bash
python3.11 -m venv .venv-native
.venv-native/bin/python -m pip install -r requirements/atomic-native.txt
.venv-native/bin/python scripts/milestone/native_check.py --output runs/native_three_trials
```

每项独立建立环境并执行三次。抓取、抬升等准备动作保存在该次轨迹中，只将指定目标技能计入被测次数。终端按表9-13序号显示每次结果，`results.json`保存实际判定，`summary.json`汇总本次覆盖项数、调用次数和成功率。结果目录需要使用新路径。单项诊断可添加`--skill G01`或其他已支持ID，`--repeats 1`仅用于诊断。

G01的目标开口为78毫米，初始约70毫米，执行器与独立验证均检查张开方向。I06限定重力反方向抬升。A05采用MuJoCo旋转拨杆及被动双稳态档位，通过接触移动至目标档位，输入`speed_scale`控制速度比例。其余原生物理适配器来自本项目AtomicSkillSoftware64归档版本，依赖置于可选安装文件，框架核心不导入仿真器。

75次批量结果使用Linux既有原生依赖环境运行当前源码。另新建Python 3.11独立环境，按requirements/atomic-native.txt安装后，M08、G01、G04、A05和T03五项代表调用全部通过。两套独立源码与虚拟环境部署已完成，公开仓库源码的完整复现另行记录。macOS测试中T03实际接收6克，按原8克判据记录为失败，当前定量倾倒结果仅对已测Linux环境成立。

### 完整32项统一入口

原生环境按requirements/atomic-native.txt安装，RoboCasa环境按[环境安装与资源](installation.zh-CN.md)准备。感知服务需要Qwen3-VL-8B-Instruct、SAM3及Depth Anything V2 Metric Small的权重和对应模型运行环境。模型环境的一键安装由本文的bootstrap.py提供，三种服务已在新建隔离环境中完成加载和96次统一技能调用。

启动三种感知服务。下面每条服务命令分别在一个终端执行，模型路径和解释器由部署环境填写。GPU编号按机器资源选择，本次验证使用物理GPU1。

```bash
mkdir -p runs/perception_services
printf '{"used":0,"limit":500}\n' > runs/perception_services/model_budget.json
CUDA_VISIBLE_DEVICES=1 <模型环境python> scripts/milestone/serve_perception.py \
  --provider qwen3vl --model-path <Qwen3-VL-8B权重目录> --port 19026 \
  --output runs/perception_services/qwen --budget runs/perception_services/model_budget.json
CUDA_VISIBLE_DEVICES=1 <模型环境python> scripts/milestone/serve_perception.py \
  --provider sam3 --model-path <sam3.pt路径> --port 19028 \
  --output runs/perception_services/sam --budget runs/perception_services/model_budget.json
CUDA_VISIBLE_DEVICES=1 <模型环境python> scripts/milestone/serve_perception.py \
  --provider metric_depth --model-path <米制深度模型目录> --port 19014 \
  --output runs/perception_services/depth --budget runs/perception_services/model_budget.json
```

服务显示ready后，运行统一入口。该命令重新生成3个感知场景，随后逐项执行26项原生技能、2项SDK运动技能和4项模型感知技能，每项3次。原生环境和SDK环境解释器可以通过参数替换。

```bash
.venv-native/bin/python scripts/milestone/atomic32_check.py run \
  --gpu 1 --sdk-python .venv-robocasa/bin/python --output runs/atomic32_acceptance
```

结果目录包含分组轨迹、每次结果、最终results.json及summary.json。整体通过要求96次记录完整、每项至少2/3、总体成功率至少90%，并满足本次补充要求：G07、A09、T01、T03各3/3。已有独立批次也可用collect子命令合并，但同一技能同一次编号不能重复计入。

M12通过RGB-D相机每6个控制步更新目标位姿，控制频率60Hz。目标为已知标记物体上方的注册偏移位姿，机械臂使用真实电机执行。验证器根据原始物体位姿与末端正运动学检查位置和朝向，不使用控制器自报误差。位置验收容差为4毫米，控制器提前收紧至其80%用于停止判断，原验收阈值保持不变。

P01采用Qwen3-VL定位与SAM3区域处理，P02在给定检测框条件下验证实例分割，P09根据柜门图像估计状态，独立参考来自仿真场景标签。P04只将RGB交给米制深度模型，使用事先冻结的相机尺度标定，参考深度仅供独立验证。感知输入中的机械臂停放和柜门开闭设置是测试场景初始化，不计作动作技能成功。

### 已完成的两次独立部署和服务记录

两套独立源码目录各自完成六类环境安装，均未启用系统共享site-packages。安装复用已下载的SDK、模型权重、仿真资产和包缓存。第一套完成全部六组评测，第二套完成核心闭环、健康检查、真实物理恢复任务及五类模型或仿真环境导入检查。完整安装使用本文前面的bootstrap.py。仅执行原生核心部署验收时可用以下命令，`--venv`和`--output`分别使用未占用目录：

```bash
python3.11 scripts/milestone/install.py \
  --venv /data/luban-independent-a/venv --profile native \
  --output runs/independent-a
```

真实物理配置通过 `--run-config physical=configs/runs/recovery_service.yaml` 登记。100次跨网络调用全部成功，往返耗时减去同次核心函数执行时长后的P95为0.506秒，最大值0.563秒，实测传输路径为SSH隧道。本轮公开源码在服务器本机连续运行3706.04秒，153/153次真实任务调用成功，期间无重启或连接失败。17项关键异常测试全部通过，注入后正常物理调用成功。本轮本机100次调用也全部成功，本机接入额外耗时P95为0.005804秒。

```bash
/data/luban-independent-a/venv/bin/luban serve --host 0.0.0.0 --port 8765 \
  --run-config physical=configs/runs/recovery_service.yaml
python scripts/milestone/platform_check.py latency \
  --platform-url http://<调用地址>:8765 --config physical --output runs/service-latency
python scripts/milestone/platform_check.py soak \
  --url http://127.0.0.1:8765 --config physical --seconds 3700 \
  --interval 20 --sample-every 5 --output runs/service-soak
```
