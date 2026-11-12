[English](installation.md) · [简体中文](installation.zh-CN.md)

# 环境安装与资源准备

## 算力与存储要求

框架核心、CPU离线示例、目录查询和历史结果查看使用Python 3.11即可，相关流程不要求本机GPU。通过远程API调用模型时，模型推理算力由服务端承担，本机仿真与模型服务的资源分别计算。

仿真与策略部署的资源取决于所选环境、模型和并发数。现有RoboCasa与XR-1单回合实例在一张RTX4090 48GB上同时推理和渲染，曾观察到约13GB显存占用，该数值属于实例观测，最低显存尚未测定。

完整64项原子技能与25组组合的本地评测使用独立的[atomic-skill-evaluator](https://github.com/yuliu-ai/atomic-skill-evaluator)交付包。当前配置按两张48GB级NVIDIA GPU准备，启动时每张卡须至少有40,000MiB空闲显存、没有其他CUDA计算进程，利用率低于10%。配套冻结运行包要求Linux x86_64、glibc≥2.35及NVIDIA driver≥580，Ubuntu 22.04为已验证系统。该配置同时驻留三路Qwen3-VL-8B及SAM3、深度模型，单张20GB或24GB卡超出现有完整入口的运行范围。

首次安装为下载缓存与运行环境保留至少120GiB空闲空间，默认三轮各50次在环境安装完成后还会检查约226GB可用输出空间，因此整机部署建议初始预留350GB。2026-10-28的191GiB节点使用已配置的兼容环境和专用逐轮无损归档流程完成三轮，该实例的磁盘占用对应其归档方式，通用入口按保留全部原始输出估算空间。

完整评测入口默认仿真并发数为4，支持1至8，本次三轮实测使用8。CPU核数与系统内存目前没有自动校验门槛，也未做最低配置测试，部署时应结合模型服务和并发规模配置主机资源。


## 核心环境

安装包支持 Python >=3.11,<3.12。已验证 Ubuntu 22.04.5 / Python 3.11.16。
CPU 示例无需 CUDA、机器人、权重、仿真资产或 API。核心依赖为 httpx、AnyIO、NumPy、
Pillow、PyYAML；锁文件固定已验证版本，没有导出共享服务器的整个环境。

```bash
uv sync --frozen --no-dev --python 3.11
uv run --frozen --no-dev python -m luban.examples.offline
```

开发环境使用 `uv sync --frozen --group dev`。在 checkout 内运行，uv 创建 `.venv`，
不安装到 Conda base 或系统 Python。Conda 也可提供解释器。

在单独 Python 3.11 venv 中安装分发包：

```bash
python -m pip install /path/to/luban-0.1.0-py3-none-any.whl
python -m luban.examples.offline --output-dir ./wheel-demo
```

wheel 包含模块示例及 Chemistry YAML／protobuf 资源。源码配置、catalog、脚本在
checkout 与源码分发包内，不应假设它们存在于 wheel 旁边。仅安装 wheel 时运行包内模块示例，
或提供自己的 YAML。

## 可选客户端

只安装所需 extra；重新 `uv sync` 而不带该 extra，会将它从托管环境移除。

```bash
uv sync --frozen --extra xr1-client
uv sync --frozen --extra rldx-client
uv sync --frozen --extra openpi
uv sync --frozen --extra chemistry_bench
uv sync --frozen --extra dashboard
```

XR-1／RLDX 增加 msgpack、pyzmq；OpenPI 使用 `pyproject.toml` 固定的上游归档，需要
GitHub 网络。Chemistry 增加 grpcio、protobuf，Isaac Sim 按 NVIDIA 条款另装。
Dashboard 增加 Gradio，可用 `luban dashboard` 浏览本地结果，默认 loopback。
这些 extra 不安装仿真器、策略服务或权重。

## RoboCasa 仿真环境

创建独立 Python 3.11 环境。适配器使用 RoboCasa365 的 Gymnasium wrapper 和
12 维移动操作动作。下列命令安装推理侧仿真依赖，模型推理与训练依赖分别管理：

```bash
uv venv .venv-robocasa --python 3.11
export SIM_PYTHON="$PWD/.venv-robocasa/bin/python"
uv pip install --python "$SIM_PYTHON" -e '.[xr1-client]' -r requirements/robocasa-runtime.txt
mkdir -p third_party/checkouts
git clone https://github.com/robocasa/robocasa.git third_party/checkouts/robocasa
git -C third_party/checkouts/robocasa checkout 456174f62b89b8fca99eaaf33949c29fec9cfc2a
export LUBAN_ROBOCASA_ROOT="$PWD/third_party/checkouts/robocasa"
```

适配器通过 `LUBAN_ROBOCASA_ROOT` 导入固定源码，不要求安装 RoboCasa 的训练依赖。
运行依赖文件选择 NumPy 2.2.5、MuJoCo 3.3.1 和固定官方 robosuite 1.5.2 源码。
PyPI 的 robosuite 1.5.2 wheel 强制安装 Mink 0.0.5（要求 NumPy <2），与 RoboCasa 冲突；
固定源码将该 IK 依赖改为可选。示例使用 PandaOmron 控制器，不使用 Mink 的 GR1 控制器。

按[RoboCasa 官方安装说明](https://robocasa.ai/docs/build/html/introduction/installation.html)
获取资产。资产不是 Python 包。如已有兼容资产，可链接到自己拥有的 SDK checkout：

```bash
bash scripts/link_robocasa_assets.sh /path/to/existing/assets \
  "$LUBAN_ROBOCASA_ROOT/robocasa/models/assets"
```

不要把其他人的 checkout 作为目标。资产条件见[第三方声明](../THIRD_PARTY_NOTICES.zh-CN.md)。
该推理环境曾在新的独立环境中完成真实 `CloseBlenderLid` 回合。
这里不包含 LeRobot／Tianshou 训练工具；数据处理或训练使用另一个完整上游环境。
仿真环境与 CPU all-extras 测试环境分开，后者的 OpenPI 客户端要求 NumPy <2。

在自己管理的 Ubuntu 主机上，可安装无界面共享库和视频工具：

```bash
sudo apt-get update
sudo apt-get install -y libegl1 libgl1 libgles2 ffmpeg
```

EGL 渲染需要 NVIDIA 驱动和 EGL／OpenGL 库，视频需要带 H.264 编码器的 FFmpeg。
用 `ffmpeg -encoders` 确认 `libx264`，用 `nvidia-smi` 检查 GPU。
实测使用单张 RTX 4090 48 GB 同时推理和渲染，观察到约 13 GB 占用；
尚未测定最低显存，不能将该观察值当作保证。

## XR-1 策略服务

按[官方部署说明](https://github.com/XiaomiRobotics/Xiaomi-Robotics-1/blob/0dd7aef8dc87296246aae812a1f59ccb708e5546/docs/DEPLOYMENT.md)
建立独立策略环境。已测试的服务为 Python 3.12.14、Torch 2.8.0+cu128、Transformers 4.57.1。
策略环境与权重被只读复用，仿真／客户端环境新装；六通智能体 安装包使用 Python 3.11。

官方源码获取参考为 `XiaomiRobotics/Xiaomi-Robotics-1@0dd7aef8dc87296246aae812a1f59ccb708e5546`。
既有策略源码归档没有 Git 元数据，已记录部分文件指纹，但未证明与该参考逐字等价。

将[RoboCasa365 checkpoint](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa365)
放在 Git 外，记录所选 Hub revision 并保留 LICENSE。原验证复用已有 checkpoint，没有下载权重。
源码启动器通过 `PYTHONPATH` 加载本项目契约，可在独立策略环境运行：

```bash
# XR1_PYTHON: absolute interpreter path in the upstream deployment environment.
# XR1_MODEL_PATH: absolute directory containing model shards and tokenizer files.
export XR1_PYTHON=/absolute/path/to/policy-env/bin/python
export XR1_MODEL_PATH=/absolute/path/to/Xiaomi-Robotics-1-RoboCasa365
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
CUDA_VISIBLE_DEVICES=0 PYTHONDONTWRITEBYTECODE=1 \
  "$XR1_PYTHON" scripts/serve_xiaomi_policy.py \
  --model-path "$XR1_MODEL_PATH" --host 127.0.0.1 --port 18400 \
  --device cuda:0 --instruction-mode planner_subtask --policy-seed-mode episode_step
```

策略环境还需轻量导入依赖、pyzmq、msgpack；Torch／CUDA／Transformers 兼容性按上游要求。
不要把仅支持 Python 3.11 的项目分发包装入 Python 3.12；3.12 验证的是这个源码启动路径。

其他外部资源包括 LIBERO／ClawVLA／LeRobot、RoboTwin、EmbodiedBench／AI2-THOR、
SpatialLM、Isaac Sim。本项目未新增它们的完整实测。
旧 `eb-alfred` extra 因固定过时依赖被移除，源码适配器仍可接入单独维护的上游环境。

旧感知探针为可选功能：`scripts/serve_perception.py` 要求 `--checkpoint` 或
`SAM3_CHECKPOINT`；`serve_perception_molmo2.py` 要求 `--model-path` 或 `MOLMO2_MODEL_PATH`。
SDK、模型及访问条件见第三方声明，它们不是快速开始的前提。

## 运行前资源检查

在仓库根目录设置好上述解释器与资源变量后：

```bash
export CUDA_VISIBLE_DEVICES=0 MUJOCO_EGL_DEVICE_ID=0
export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl
"$SIM_PYTHON" scripts/check_gpu_setup.py --mode simulator --check-render
"$XR1_PYTHON" scripts/check_gpu_setup.py --mode policy
```

仿真检查导入依赖、SDK、资产、视频工具，并实际渲染 64×64 MuJoCo 图像。
策略检查导入、CUDA 可用性和权重分片完整性，不加载完整模型。
缺项显示 `status: failed` 并 exit 1。启动服务后，再执行[回合指南](episodes.zh-CN.md)中的健康检查。

换卡时，将两个 GPU 变量都设为同一空闲物理卡编号；策略进程内部设备仍为 `cuda:0`，
EGL 变量选择物理渲染卡。换端口时，服务 `--port` 与客户端 `XIAOMI_REMOTE_PORTS` 保持一致。
同机部署将服务绑定 `127.0.0.1`。
