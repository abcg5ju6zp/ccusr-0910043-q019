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

### 增量评估（复用中间结果）

规则编译后会记录每个表达式节点真实依赖的**输入字段、属性解析器与函数**。对同一输入对象做小
范围修改时，可以通过增量会话只重算受变更集影响的节点：

```python
import rule_engine

rule = rule_engine.Rule('price * discount > 100 and tier == "gold"')
session = rule.incremental_session()

thing = {'price': 50, 'discount': 3, 'tier': 'gold'}
session.evaluate(thing, None)                    # 首版：全量计算

thing['tier'] = 'silver'
session.evaluate(thing, ('tier',))              # 只重算 tier 子树，price * discount 直接复用
```

失效规则：

- `changed_fields` 声明变更的根字段；只有依赖与变更集相交的节点才重算。短路/三元分支按运行
  时实际执行路径工作——分支翻转时从未执行过的分支会自然补算，不会沿用旧分支结果。
- 无法用字段描述的外部变化传 `changed_fields=None` 或 `external_change=True` 强制全量重算；
  属性访问器、自定义函数热更新可用 `changed_resolvers` / `changed_functions` 精确失效。
- 自定义函数默认按**有副作用**处理（每次执行）；确认无副作用且结果确定时，用
  `Context.declare_function_purity('f', rule_engine.PURE)` 声明为纯函数；`'volatile'` 用于
  `$now`、`$random` 一类结果随时间变化的函数。
- 读取外部状态的动态属性可在 OBJECT 类型上声明 `accessor_dynamic=True`，相关节点不做复用。
- 异常结果与正常结果一样按版本缓存并一致重抛；集合/映射结果在缓存与调用方之间复制隔离。
- 同一输入版本可被多个线程并发进入并共享只读结果；缓存保存在会话对象内，不同规则实例、不同
  上下文、不同输入对象之间绝不串用。

