---
name: python-style
description: 本项目的 Python 代码规范：格式化、类型标注、测试布局。写 Python 前读。
---

# Python 规范

## 格式化与检查

- 格式化：`ruff format .`
- 检查：`ruff check . --fix`
- 行宽 110
- 提交前两条都要跑过

## 类型标注

- 所有公开函数必须有参数和返回值标注
- 用 `X | None`，不用 `Optional[X]`
- 用内置泛型 `list[str]` / `dict[str, int]`，不用 `typing.List`
- 私有辅助函数可以省略标注

## 结构

- 模块级常量全大写
- 数据容器优先 `@dataclass`，需要校验时才上 pydantic
- 不写只有一个实现的抽象基类

## 测试

- 测试放在 `tests/`，文件名 `test_<模块名>.py`
- 一个测试只断言一件事
- 用 `pytest.raises` 断言异常，不要 `try/except` + `assert False`
- 不 mock 被测对象本身，只 mock 它的外部依赖

## 禁止

- 裸 `except:`（至少写 `except Exception`）
- 可变对象作默认参数（`def f(x=[])`）
- `from module import *`
