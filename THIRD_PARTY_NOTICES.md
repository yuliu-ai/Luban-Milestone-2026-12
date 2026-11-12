[English](THIRD_PARTY_NOTICES.md) · [简体中文](THIRD_PARTY_NOTICES.zh-CN.md)

# Third-party notices

Checked for this preparation on 2026-10-07. The existing project LICENSE is
Apache-2.0; this is not a relicensing of external code, models or data.

## What is distributed

The framework and its owned atomic-skill/Chemistry source retain the original
Apache-2.0 license and file-level notices. Generated protobuf files derive from
the project's included proto schema and require separately installed protobuf/grpc.
The parent source reference and snapshot identity are in NOTICE and docs/provenance.md.

No copied SpatialLM, EmbodiedBench, RoboCasa, robot/navigation vendor trees, CAD,
model weights or simulator datasets are distributed. The original archive's
SpatialLM Llama/CC-BY-NC files and unlicensed CAD are therefore outside this repo.

## Historical simulation fixtures

`catalogs/v2.2/evidence/` contains small pre-existing simulator screenshots,
segmentation masks and JSON/JSONL probe records referenced by catalog integrity
tests. They are historical test/evidence fixtures, not results of this preparation.
Their simulator imagery derives from RoboCasa scenes/assets. Attribution:
**RoboCasa Team**. The pinned [RoboCasa release declaration](https://github.com/robocasa/robocasa/blob/456174f62b89b8fca99eaaf33949c29fec9cfc2a/README.md)
licenses its assets and datasets under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Changes represented in these fixtures include rendered camera viewpoints, image
sampling and segmentation. Preserve source attribution with redistributed images;
the root Apache-2.0 code license does not replace the asset terms. Individual
third-party object/material notices also remain applicable.

Historical segmentation masks are outputs of an external perception model.
The [SAM License](https://github.com/facebookresearch/sam3/blob/main/LICENSE)
and original asset terms are separate from the framework's code license. The model
and its weights are not included in this repository.

## Runtime libraries and optional SDKs

| Component | Relationship | License/status |
| --- | --- | --- |
| httpx, AnyIO, PyYAML, msgpack, Gradio | Installed dependencies | BSD-3-Clause / MIT / Apache-2.0 according to each installed distribution; exact versions and package metadata inventory in validation evidence |
| h5py | Optional full-test dependency | BSD-3-Clause; bundled HDF5/binary notices remain in its distribution |
| Torch CPU | Optional full-test dependency | Installed License-Expression combines Apache-2.0, Apache-2.0 WITH LLVM-exception, BSD-2-Clause, BSD-3-Clause, BSL-1.0 and MIT; retain its binary/component notices |
| SymPy / NetworkX / Tree | Installed test/OpenPI dependencies | BSD / BSD-3-Clause / MIT according to installed metadata |
| NumPy | Installed dependency | BSD-3-Clause; packaged binary includes separate bundled notices |
| Pillow | Installed dependency | HPND / bundled component notices; retain its distribution licenses |
| pyzmq | Optional dependency | BSD-3-Clause and bundled libzmq LGPL terms; inspect the binary distribution's licenses |
| grpcio / protobuf | Optional Chemistry clients | Apache-2.0 / BSD-3-Clause; see distribution metadata |
| OpenPI client (wheel metadata lacks a license field; pinned upstream LICENSE reviewed) | Optional fixed upstream archive `5a6beda9ff99da30b4e1b59320f6a32971d7c397` | Apache-2.0 upstream code; models/base components have separate terms |
| RoboCasa source `456174f62b89b8fca99eaaf33949c29fec9cfc2a` | External simulator | MIT, copyright RoboCasa Team, with MuJoCo attribution in upstream LICENSE |
| robosuite 1.5.2 | External simulator dependency | MIT upstream; retain its LICENSE and bundled model notices |
| Xiaomi Robotics-1 code | External policy server | Apache-2.0, copyright Xiaomi Corporation; source reference in SOURCES.toml |
| Xiaomi-Robotics-1-RoboCasa365 | External model | Official model card and local weight LICENSE declare Apache-2.0; base-model and tokenizer notices still apply |
| EmbodiedBench | External legacy adapter | GitHub license endpoint returned 404; no redistribution permission established; not bundled and obsolete automatic extra removed |
| LIBERO / RoboTwin / ClawVLA / LeRobot | External integrations | Check exact chosen upstream versions and asset/model terms before installation or redistribution; not live-tested or bundled here |
| SpatialLM | External optional backend | Upstream code Llama 3.2 terms / some SceneScript-derived CC-BY-NC-4.0; weights separately restricted; not installed or bundled here |
| SAM3 | External optional perception SDK/model | Custom [SAM License](https://github.com/facebookresearch/sam3/blob/main/LICENSE); the [official model](https://huggingface.co/facebook/sam3) uses gated access. Not bundled/downloaded or live-run here; users must obtain access and review the terms themselves |
| Molmo2-4B | External optional perception model | [Official card](https://huggingface.co/allenai/Molmo2-4B) declares Apache-2.0 and additionally describes intended research/education use and training-data conditions; review suitability. Not bundled/downloaded or live-run here |
| Isaac Sim | External Chemistry worker | NVIDIA EULA; not an Apache-2.0 component |
| FFmpeg | External video encoder | Binary build options determine LGPL/GPL obligations; not bundled |

Primary license references: [RoboCasa pinned LICENSE](https://github.com/robocasa/robocasa/blob/456174f62b89b8fca99eaaf33949c29fec9cfc2a/LICENSE),
[Xiaomi code LICENSE](https://github.com/XiaomiRobotics/Xiaomi-Robotics-1/blob/0dd7aef8dc87296246aae812a1f59ccb708e5546/LICENSE),
[checkpoint model card](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa365).

RoboCasa assets and benchmark task data are separate downloads and may contain
third-party object/material licenses. No blanket right to redistribute those
assets is claimed. Downloads, weights, API accounts and inference outputs remain
subject to the chosen provider's terms.
