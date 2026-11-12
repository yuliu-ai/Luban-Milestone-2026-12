## 改动说明

(做了什么、为什么;一个 PR 只做一件事)

## 自查清单

- [ ] `pytest` 全绿
- [ ] `ruff check src tests scripts` 与 `ruff format --check src tests` 通过
- [ ] `mypy`(strict)0 错误
- [ ] 改了配置/组件接口时:`python scripts/validate_configs.py` 无 FAIL,并同步更新 `docs/`
- [ ] 新增/修改行为有对应单元测试(修 bug 先写回归测试)
- [ ] 不包含内部信息(服务器地址、账号、密钥)与模型权重

## 验证输出

```
(粘贴 pytest / mypy / ruff / validate_configs 的关键输出)
```
