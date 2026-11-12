[English](robosynchallenge.md) · [简体中文](robosynchallenge.zh-CN.md)

# RoboSynChallenge

This integration connects RoboSynChallenge to Luban's runtime, execution and verification flow. It supports task-specific ACT/DP policies, explicit seeds, individual tasks and resumable evaluation matrices. RoboSynChallenge is a laboratory benchmark. The titration video uses a separate Isaac Sim scene.

## Environment and resources

Use a dedicated Python 3.11 simulation environment. Follow [RoboSynChallenge](https://github.com/EDEM-AI/RoboSynChallenge) and [EmbodiChain](https://github.com/DexForce/EmbodiChain) to prepare their SDKs, DexSim, LeRobot, NVIDIA drivers and assets, then install Luban. Reference revisions are RoboSynChallenge `8818c82df5fa2522809b8105e7018950dcc9a9a8` and EmbodiChain `89c14da8d0221dc4ee2a4a3e0ba1078b16d447bd`. Select the public ACT or DP checkpoint for the chosen task.

## Run one task

Run from the repository root and replace the three resource paths with your installations.

```bash
python scripts/eval_robosynchallenge.py \
  --robosyn-root /path/to/RoboSynChallenge \
  --embodichain-root /path/to/EmbodiChain \
  --task click_bell \
  --checkpoint /path/to/ACT_sim_click_bell \
  --setting random --seeds 0 \
  --device cuda:0 --gpu 0 \
  --output runs/robosyn-click-bell
```

Without an agent config, the entry point uses the task instruction and environment feedback. Add `--agent-config configs/agents/robosynchallenge_act_visual.yaml` to enable visual planning and verification. Set `LUBAN_LLM_BASE_URL` and `LUBAN_LLM_MODEL` for a service supporting images and structured output. The command-line checkpoint overrides the example config path. ACT/DP produces task-specific actions and does not consume the planner's language instruction.

## Outputs and resuming

The output directory contains `metadata.json`, `summary.json`, `episodes.jsonl` and per-attempt records. Native environment feedback determines success, and execution errors are counted separately. With the same settings, `--resume` skips valid outcomes and retries interrupted or exceptional episodes. Use a new process and output directory for another task or setting.

Use `--matrix-manifest` for a manifest containing `tasks`, `seeds`, `setting` and `policy`. Each task specifies `name` and `checkpoint_path`. `--matrix-gpus` selects GPU queues and `--matrix-scheduling dynamic` assigns work to available queues. CPU regression checks cover interfaces, native-result precedence, seed counts, persisted exceptions and resuming. They do not start simulation. See the [results guide](reported-results.md) for the historical 412/1000 result.
