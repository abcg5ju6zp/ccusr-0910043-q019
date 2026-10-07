# 规则表达式引擎

本项目提供可嵌入服务端的规则解析、类型检查、属性解析和表达式评估能力。生产源码位于 `lib/rule_engine/`，核心回归测试位于 `tests/`。

## 安装

`python3 -m pip install --break-system-packages --no-build-isolation -e .`

## 测试

`python3 -m pytest -q`

## 构建

`python3 -m compileall -q lib/rule_engine`

`python3 -m build --wheel --no-isolation`

## 使用

调用方创建规则并传入普通 Python 对象即可完成本地评估，不需要外部服务。

## 增量求值

定价等场景每次只变更一个输入字段，全量重算会重复执行整棵规则树与所有昂贵的解析函数。`rule_engine.IncrementalRule` 在编译期记录字段、解析器（解析函数）与自定义函数之间的真实依赖；调用方提交变更集后，求值仅重新计算受影响的节点，未受影响的中间结果在同一输入版本内被并发安全地共享。

```python
import rule_engine

rule = rule_engine.IncrementalRule('parse_price(price) * qty - discount', pure_functions={'parse_price'})
thing = {'price': '1.5', 'qty': 3, 'discount': 1, 'parse_price': parse_price}
rule.evaluate(thing)          # 完整求值并记录依赖

thing['discount'] = 2
rule.submit_changes({'discount'})   # 提交变更集：字符串为根字段，元组为嵌套路径
rule.evaluate(thing)          # 仅重算受 discount 影响的节点，parse_price 不会重跑
```

* `submit_changes` 接受单个字段名、单条路径（如 `('user', 'age')`）或它们的可迭代组合；变更会使该路径的祖先与后代读取同时失效。
* 短路（`and` / `or`）、三元（`?:`）与空值合并（`??`）只记录实际走过的分支读取，条件翻转后不会沿用旧分支结果。
* 动态下标（`items[i]`）按运行时真实下标记录依赖；集合推导式作为整体缓存，循环变量不会跨迭代串值。
* 自定义函数默认视为有副作用、每次重算；用 `pure_functions={'name', ...}` 声明纯函数后参与缓存。`$random`、`$now`、`$re_groups` 等不确定内置符号永不缓存。
* 引擎异常（如符号缺失、下标越界、除零）同样作为缓存结果参与失效：依赖未变时直接重放，相关字段变更后自动重试。
* 缓存严格挂在单个规则实例上，不同上下文或规则实例之间绝不串用；同一输入版本的并发 `evaluate` 共享只读结果。输入对象被整体替换时自动全量失效；`reset()` 可手动清空。
* 自定义 resolver 必须对同一输入版本返回确定结果。`dependency_report()` 可查看编译期记录的字段路径、函数调用与易变节点，`cache_info()` 可查看缓存概况。
