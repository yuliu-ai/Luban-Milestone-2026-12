[English](SECURITY.md) · [简体中文](SECURITY.zh-CN.md)

# Security

Keep service credentials in environment variables, never YAML or Git. Treat
`class_path` YAML as trusted executable configuration. Policy RPC and dashboards
default to loopback; remote deployment requires your own network/access controls.

If you find a vulnerability, contact the repository owner through an existing
private channel. Do not post real credentials or private run evidence in an issue.
Dependency/security checks are version- and database-specific, not a guarantee of
absence of vulnerabilities. Scope and exclusions are in the validation record.
