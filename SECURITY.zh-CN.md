[English](SECURITY.md) · [简体中文](SECURITY.zh-CN.md)

# 安全说明

服务凭据放在环境变量中，不写入 YAML 或 Git。`class_path` YAML 能加载 Python 类，
按可信可执行配置处理。策略 RPC 和 Dashboard 默认 loopback；远程部署自行配置网络与访问控制。

发现漏洞时通过已有私密渠道联系仓库所有者，不在 issue 贴真实凭据或私人运行证据。
依赖／安全检查取决于版本和漏洞数据库，不保证不存在漏洞；范围见[验证记录](docs/validation.zh-CN.md)。
