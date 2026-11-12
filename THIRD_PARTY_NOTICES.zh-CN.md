[English](THIRD_PARTY_NOTICES.md) · [简体中文](THIRD_PARTY_NOTICES.zh-CN.md)

# 第三方声明

原检查日期 2026-10-07。项目 LICENSE 为 Apache-2.0，不改变外部代码、模型或数据的许可。
本说明译文用于阅读，对应原许可文本与组件声明继续保留。

## 分发内容

框架、自有原子技能／Chemistry 源码保留 Apache-2.0 与文件级声明。
生成的 protobuf 来自随包 proto schema，protobuf／grpc 需另外安装。
父项目与快照身份见 NOTICE 和[来源说明](docs/provenance.zh-CN.md)。
不分发复制的 SpatialLM、EmbodiedBench、RoboCasa、机器人／导航厂商源码树、CAD、
权重或仿真数据集；原归档的 Llama／CC-BY-NC 文件和未获许可 CAD 不在本仓库中。

## 历史仿真夹具

`catalogs/v2.2/evidence/` 中的小型仿真截图、分割 mask、JSON／JSONL 探针记录用于目录完整性测试，
不是本次准备的新实验。图像来自 RoboCasa 场景／资产，归属为 **RoboCasa Team**。
[固定版本声明](https://github.com/robocasa/robocasa/blob/456174f62b89b8fca99eaaf33949c29fec9cfc2a/README.md)
将资产和数据声明为 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)。
夹具处理包括渲染视角、采样和分割；再分发时保留来源，根代码许可不覆盖资产条款，
单独对象／材质声明仍适用。

历史分割 mask 为外部感知模型输出，[SAM License](https://github.com/facebookresearch/sam3/blob/main/LICENSE)
与原资产条款独立适用。模型与权重不随仓库提供。

## 运行库与可选 SDK

| 组件 | 关系 | 许可／状态 |
| --- | --- | --- |
| httpx、AnyIO、PyYAML、msgpack、Gradio | 安装依赖 | 按各分发包为 BSD-3-Clause／MIT／Apache-2.0，版本与元数据见验证证据 |
| h5py | full-test | BSD-3-Clause；保留 HDF5／binary notices |
| Torch CPU | full-test | 元数据组合 Apache-2.0、LLVM exception、BSD-2/3-Clause、BSL-1.0、MIT；保留组件声明 |
| SymPy／NetworkX／Tree | 测试／OpenPI | 按元数据 BSD／BSD-3-Clause／MIT |
| NumPy | 核心依赖 | BSD-3-Clause；binary 的捆绑声明独立保留 |
| Pillow | 核心依赖 | HPND 与捆绑组件声明 |
| pyzmq | 可选客户端 | BSD-3-Clause，捆绑 libzmq 的 LGPL 条款另查 binary notices |
| grpcio／protobuf | Chemistry 客户端 | Apache-2.0／BSD-3-Clause |
| OpenPI client | 固定归档 `5a6beda9ff99da30b4e1b59320f6a32971d7c397` | wheel 缺许可字段，上游 LICENSE 已审阅为 Apache-2.0；模型另有条款 |
| RoboCasa `456174f62b89b8fca99eaaf33949c29fec9cfc2a` | 外部仿真 | MIT，RoboCasa Team；上游另含 MuJoCo 归属 |
| robosuite 1.5.2 | 外部仿真依赖 | MIT；保留 LICENSE 与模型声明 |
| Xiaomi Robotics-1 code | 外部策略服务 | Apache-2.0，Xiaomi Corporation；来源见 SOURCES.toml |
| Xiaomi-Robotics-1-RoboCasa365 | 外部模型 | 官方 card 与权重 LICENSE 为 Apache-2.0，底层模型／tokenizer 声明仍适用 |
| EmbodiedBench | 历史适配器 | 上游许可接口 404，未建立再分发许可，不捆绑，已移除旧自动 extra |
| LIBERO／RoboTwin／ClawVLA／LeRobot | 外部集成 | 核对实际版本及模型／资产条款，未捆绑或新增实测 |
| SpatialLM | 外部可选后端 | 上游 Llama 3.2／部分 CC-BY-NC-4.0，权重另受限制；未捆绑 |
| SAM3 | 外部感知 SDK／模型 | [自定义许可](https://github.com/facebookresearch/sam3/blob/main/LICENSE)，[模型](https://huggingface.co/facebook/sam3)需 gated access；自行获取与审阅 |
| Molmo2-4B | 外部感知模型 | [官方 card](https://huggingface.co/allenai/Molmo2-4B)为 Apache-2.0，并说明研究／教育用途和训练数据条件；未捆绑或新增实测 |
| Isaac Sim | 外部 Chemistry worker | NVIDIA EULA |
| FFmpeg | 外部编码器 | build 选项决定 LGPL／GPL 条件，不捆绑 |

一手许可：[RoboCasa](https://github.com/robocasa/robocasa/blob/456174f62b89b8fca99eaaf33949c29fec9cfc2a/LICENSE)、
[Xiaomi code](https://github.com/XiaomiRobotics/Xiaomi-Robotics-1/blob/0dd7aef8dc87296246aae812a1f59ccb708e5546/LICENSE)、
[checkpoint card](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa365)。

RoboCasa 资产／任务数据另行下载，可能包含对象／材质的第三方许可，不声明具有统一再分发权。
下载、权重、API 账号和推理输出仍遵循相应提供方条件。
