[English](README.md) · [简体中文](README.zh-CN.md)

# Configurations

Start with `agents/offline.yaml` + `runs/offline.yaml` for CPU mock flow,
`agents/xr1_simple.yaml` + `runs/robocasa_simple.yaml` for the bounded real example,
or `agents/llm_counter.yaml` + `runs/llm_counter.yaml` for optional live-model/mock-env
validation. Complete commands and variables are in docs/.

Other agent/run files are retained research templates with existing CPU regression
coverage, not newly validated live integrations. They may require matching policy
instruction contracts, model IDs, heavy SDKs, assets and paid APIs. Review all
budgets before use. `experiments/recovery_memory*.yaml` are study-preparation
templates, not measured experimental results. `knowledge/` contains small supplied
example knowledge records, not a newly verified research benchmark or dataset.
