[English](CONTRIBUTING.md) · [简体中文](CONTRIBUTING.zh-CN.md)

# Contributing

Keep contributions focused on the current package scope.

Install Python 3.11 and run `uv sync --frozen --group dev`, then
`bash scripts/ci.sh`. See [development](docs/development.md) for commands, optional
tests, packaging and evidence requirements. Fixes should include meaningful
regression coverage and documentation when public behavior changes.

Do not commit credentials, private endpoints, raw run evidence, weights or datasets.
Preserve copyright notices and document any introduced third-party source/license.
Contributions to the existing Apache-2.0 project follow the repository LICENSE;
do not assert rights to someone else's code or assets. No CLA is required.
