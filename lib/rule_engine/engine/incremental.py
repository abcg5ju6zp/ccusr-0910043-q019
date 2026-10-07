#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/engine/incremental.py
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are
#  met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of the project nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY FOR A
#  PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

"""编译期依赖分析与按输入版本复用中间结果的增量求值支持。

工作模型：

* 规则编译完成后，:py:func:`analyze_statement` 自底向上遍历 AST，为每个表达式节点挂载
  :py:class:`NodeDependencies`，记录它真实读取的根输入 *字段*、经过的 *解析器* 与调用的
  *函数*，以及保守的 ``non_cacheable`` 标记（波动型内置、未登记纯度的自定义函数、动态
  属性访问、正则捕获组副作用等）。
* 调用方通过 :py:class:`IncrementalSession` 提交一个"修订版"（变更集），会话只重算依赖与
  变更集相交（或带 ``non_cacheable`` 标记）的节点；其余节点沿用上一版本的只读结果。短路
  算子与三元表达式在运行时按需求值——从未执行过的分支没有缓存条目，分支翻转时会自然补算，
  而不会沿用上一版的旧分支结果。
* 缓存条目按输入版本编号。同一版本内的并发评估在节点上会合（事件 + 双检），先到的线程
  计算，其余线程共享同一个只读结果或重抛同一个异常快照。
* 缓存存放在会话对象中：不同 :py:class:`~rule_engine.Rule` 实例、不同
  :py:class:`~rule_engine.Context` 之间天然不共享；线程仅在 TLS 中保存当前修订版的指针。
"""

from __future__ import annotations

import collections
import contextlib
import copy
import threading
from typing import TYPE_CHECKING, Any, Iterable, Iterator, NamedTuple

from .. import ast
from .. import builtins as _builtins
from .. import errors
from ..types import DataType

if TYPE_CHECKING:
    from .context import Context
    from .rule import Rule

# 函数纯度档位
PURE = 'pure'
"""给定相同参数时结果确定且无副作用：可以按字段依赖安全缓存。"""
VOLATILE = 'volatile'
"""结果不随输入版本固定（时间、随机数等）：节点及其祖先每次评估都重新计算。"""
IMPURE = 'impure'
"""自定义函数可能产生副作用或读取外部状态：节点及其祖先每次评估都重新计算。"""

_PURITY_LEVELS = frozenset({PURE, VOLATILE, IMPURE})

_BUILTIN_SCOPE = _builtins.Builtins.scope_name
_ATTRIBUTE_SCOPE = 'attribute'
_ROOT_SCOPE = 'root'

# 内置符号的默认纯度（未列出的内置名称一律按波动处理，保守地不做复用）
_DEFAULT_PURE_BUILTINS = frozenset({
        'abs', 'all', 'any', 'sum', 'map', 'max', 'min', 'filter',
        'parse_datetime', 'parse_float', 'parse_timedelta', 'range', 'split',
        'e', 'pi',
})
# 会在引擎内部间接调用其参数的内置函数：参数位 -> 参数位置（这些位置上的函数纯度必须并入）
_HIGHER_ORDER_BUILTINS = {'map': (0,), 'filter': (0,)}


class NodeDependencies(object):
    """单个 AST 节点的编译期依赖摘要。"""
    __slots__ = ('fields', 'resolvers', 'functions', 'non_cacheable')
    fields: frozenset[str]
    """节点（含全部后代）读取的根输入字段名。"""
    resolvers: frozenset[str]
    """节点经过的具名解析器（``object:<类型名>``、``attr:<属性名>``、``mapping-attribute:<属性名>``）。"""
    functions: frozenset[str]
    """节点调用的函数（``built-in:<名称>``、``root:<名称>``、``attribute:<名称>``）。"""
    non_cacheable: bool
    """为真时该节点及其祖先不能跨评估复用（波动/副作用/动态来源）。"""

    def __init__(
            self,
            fields: Iterable[str] = (),
            *,
            resolvers: Iterable[str] = (),
            functions: Iterable[str] = (),
            non_cacheable: bool = False
    ) -> None:
        self.fields = frozenset(fields)
        self.resolvers = frozenset(resolvers)
        self.functions = frozenset(functions)
        self.non_cacheable = non_cacheable

    def __repr__(self) -> str:
        return "<{} fields={} resolvers={} functions={} non_cacheable={} >".format(
                self.__class__.__name__,
                sorted(self.fields),
                sorted(self.resolvers),
                sorted(self.functions),
                self.non_cacheable
        )


def _merge_dependencies(*dependencies: NodeDependencies | None, non_cacheable: bool = False) -> NodeDependencies:
    fields: set[str] = set()
    resolvers: set[str] = set()
    functions: set[str] = set()
    tainted = non_cacheable
    for dependency in dependencies:
        if dependency is None:
            continue
        fields.update(dependency.fields)
        resolvers.update(dependency.resolvers)
        functions.update(dependency.functions)
        tainted = tainted or dependency.non_cacheable
    return NodeDependencies(fields, resolvers=resolvers, functions=functions, non_cacheable=tainted)


def _callee_scope(expression: ast.ExpressionBase) -> str | None:
    """返回被调用表达式的作用域标签；无法静态命名时返回 None。"""
    if isinstance(expression, ast.SymbolExpression):
        return expression.scope if expression.scope is not None else _ROOT_SCOPE
    if isinstance(expression, ast.GetAttributeExpression):
        return _ATTRIBUTE_SCOPE
    return None


def _function_label(name: str, scope: str | None) -> str:
    return '{0}:{1}'.format(scope or _ROOT_SCOPE, name)


class _DependencyAnalyzer(object):
    """编译期依赖分析器：遍历已归约的语句并就地挂载 ``_deps``。"""
    def __init__(self, context: 'Context') -> None:
        self.context = context

    def analyze(self, statement: ast.Statement) -> NodeDependencies:
        return self._visit(statement.expression, frozenset())

    def _visit(self, node: Any, bound_variables: frozenset[str]) -> NodeDependencies:
        if isinstance(node, ast.SymbolExpression):
            deps = self._symbol_dependencies(node, bound_variables)
        elif isinstance(node, ast.FunctionCallExpression):
            deps = self._function_call_dependencies(node, bound_variables)
        elif isinstance(node, ast.GetAttributeExpression):
            deps = self._get_attribute_dependencies(node, bound_variables)
        elif isinstance(node, ast.FuzzyComparisonExpression):
            # 正则匹配会把捕获组写入 Context 的线程本地状态，节点必须每次执行以重放副作用
            deps = _merge_dependencies(
                    *(self._visit(child, bound_variables) for child in self._expression_children(node)),
                    non_cacheable=True
            )
        elif isinstance(node, ast.ComprehensionExpression):
            deps = self._comprehension_dependencies(node, bound_variables)
        else:
            children = self._expression_children(node)
            deps = (
                    _merge_dependencies(*(self._visit(child, bound_variables) for child in children))
                    if children else NodeDependencies()
            )
        node._deps = deps
        return deps

    @staticmethod
    def _expression_children(node: Any) -> list[ast.ExpressionBase]:
        """收集节点直接持有的全部表达式子节点（含 tuple/set 中的嵌套成员）。

        节点属性可能声明在各级 ``__slots__`` 中，也可能落在实例 ``__dict__`` 里（部分基类
        自身没有 ``__slots__``，例如二元表达式基类）。
        """
        children: list[ast.ExpressionBase] = []
        candidate_names: set[str] = set()
        for klass in type(node).__mro__:
            candidate_names.update(getattr(klass, '__slots__', ()))
        instance_dict = getattr(node, '__dict__', None)
        if instance_dict:
            candidate_names.update(instance_dict)
        candidate_names.discard('context')
        candidate_names.discard('_deps')
        for attr_name in candidate_names:
            try:
                value = getattr(node, attr_name)
            except AttributeError:
                continue
            _DependencyAnalyzer._scan(value, children)
        return children

    @staticmethod
    def _scan(value: Any, children: list[ast.ExpressionBase]) -> None:
        if isinstance(value, ast.ExpressionBase):
            children.append(value)
        elif isinstance(value, (tuple, list, set, frozenset)):
            for member in value:
                _DependencyAnalyzer._scan(member, children)

    def _purity(self, name: str, scope: str | None) -> str:
        registered = self.context.resolve_function_purity(name, scope=scope)
        if registered is not None:
            return registered
        if scope == _BUILTIN_SCOPE:
            return PURE if name in _DEFAULT_PURE_BUILTINS else VOLATILE
        if scope == _ATTRIBUTE_SCOPE:
            # 属性解析器产出的可调用值（.ends_with 等）受模式约束，视为纯；动态 accessor 已另行标记
            return PURE
        # 未经登记的根作用域自定义函数：默认有副作用，调用方必须显式声明纯度才可复用
        return IMPURE

    def _symbol_dependencies(self, node: ast.SymbolExpression, bound_variables: frozenset[str]) -> NodeDependencies:
        if node.scope == _BUILTIN_SCOPE:
            purity = self._purity(node.name, _BUILTIN_SCOPE)
            return NodeDependencies(
                    functions=(_function_label(node.name, _BUILTIN_SCOPE),),
                    non_cacheable=(purity != PURE)
            )
        # 推导变量遮蔽同名根字段：迭代元素不是根输入
        if node.name in bound_variables:
            return NodeDependencies()
        # 根字段（也包括由字段承载、稍后被调用的函数值）
        return NodeDependencies((node.name,))

    def _get_attribute_dependencies(
            self, node: ast.GetAttributeExpression, bound_variables: frozenset[str]
    ) -> NodeDependencies:
        deps = self._visit(node.object, bound_variables)
        resolver_label: str
        dynamic = False
        if node._object_type is not None:
            resolver_label = 'object:{0}'.format(node._object_type.name)
            # 声明为动态的属性访问（例如读取外部状态的 property）没有可声明的字段依赖
            dynamic = bool(getattr(node._object_type, 'accessor_dynamic', False))
        elif DataType.is_type(node.object.result_type, DataType.MAPPING):
            # MAPPING 上的点号回退：值可能来自任意键
            resolver_label = 'mapping-attribute:{0}'.format(node.name)
        else:
            resolver_label = 'attr:{0}'.format(node.name)
        return _merge_dependencies(deps, NodeDependencies(resolvers=(resolver_label,), non_cacheable=dynamic))

    def _function_call_dependencies(
            self, node: ast.FunctionCallExpression, bound_variables: frozenset[str]
    ) -> NodeDependencies:
        deps = [self._visit(node.function, bound_variables)]
        labels: set[str] = set()
        impure = False
        higher_order_positions: tuple[int, ...] = ()

        callee_scope = _callee_scope(node.function)
        if callee_scope is not None:
            callee_name = node.function.name  # type: ignore[attr-defined]
            labels.add(_function_label(callee_name, callee_scope))
            symbol_scope = node.function.scope if isinstance(node.function, ast.SymbolExpression) else callee_scope  # type: ignore[attr-defined]
            if self._purity(callee_name, symbol_scope) != PURE:
                impure = True
            if callee_scope == _BUILTIN_SCOPE:
                higher_order_positions = _HIGHER_ORDER_BUILTINS.get(callee_name, ())
        else:
            # 无法静态命名的被调用值（例如其它函数的返回值再调用）：保守视为有副作用
            impure = True

        for position, argument in enumerate(node.arguments):
            deps.append(self._visit(argument, bound_variables))
            # 该参数会被间接调用：内置高阶函数（map/filter）的固定参数位，或静态类型为 FUNCTION
            invoked_indirectly = (
                    position in higher_order_positions
                    or DataType.is_type(argument.result_type, DataType.FUNCTION)
            )
            if not invoked_indirectly:
                continue
            argument_scope = _callee_scope(argument)
            if argument_scope is not None:
                labels.add(_function_label(argument.name, argument_scope))  # type: ignore[attr-defined]
                arg_symbol_scope = argument.scope if isinstance(argument, ast.SymbolExpression) else argument_scope  # type: ignore[attr-defined]
                if self._purity(argument.name, arg_symbol_scope) != PURE:  # type: ignore[attr-defined]
                    impure = True
            else:
                # 被间接调用的函数来自无法静态命名的表达式（其它调用的返回值等）：保守视为有副作用
                impure = True

        return _merge_dependencies(*deps, NodeDependencies(functions=labels, non_cacheable=impure))

    def _comprehension_dependencies(
            self, node: ast.ComprehensionExpression, bound_variables: frozenset[str]
    ) -> NodeDependencies:
        deps = [self._visit(node.iterable, bound_variables)]
        inner_bound = bound_variables | {node.variable}
        if node.condition is not None:
            deps.append(self._visit(node.condition, inner_bound))
        deps.append(self._visit(node.result, inner_bound))
        return _merge_dependencies(*deps)


def analyze_statement(statement: ast.Statement) -> NodeDependencies:
    """对已编译语句执行依赖分析，就地为每个表达式节点挂载 ``_deps`` 并返回根依赖。"""
    analyzer = _DependencyAnalyzer(statement.context)
    return analyzer.analyze(statement)


class _ChangeSet(NamedTuple):
    fields: frozenset[str] | None
    """本版变更的根字段；``None`` 表示全量失效（外部状态变化或调用方未声明变更集）。"""
    resolvers: frozenset[str]
    functions: frozenset[str]

    def affects(self, deps: NodeDependencies) -> bool:
        if self.fields is None:
            return True
        return bool(
                (deps.fields & self.fields)
                or (deps.resolvers & self.resolvers)
                or (deps.functions & self.functions)
        )


class _Entry(object):
    __slots__ = ('version', 'is_error', 'value', 'error')
    def __init__(self, version: int, *, value: Any = None, error: BaseException | None = None, is_error: bool = False) -> None:
        self.version = version
        self.is_error = is_error
        self.value = value
        self.error = error


def _clone_for_store(value: Any) -> Any:
    """为缓存制作一份与求值结果隔离的只读副本（仅复制可变集合，标量与 OBJECT 原样保留）。"""
    if isinstance(value, tuple):
        return tuple(_clone_for_store(member) for member in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_clone_for_store(member) for member in value)
    if isinstance(value, collections.abc.Mapping):
        return collections.OrderedDict(
                (key, _clone_for_store(member)) for key, member in value.items()
        )
    return value


def _clone_for_use(value: Any) -> Any:
    """从缓存取值时再复制一份，使一个消费者对集合的就地修改不会污染缓存或其他并发消费者。"""
    if isinstance(value, tuple):
        return tuple(_clone_for_use(member) for member in value)
    if isinstance(value, frozenset):
        return set(_clone_for_use(member) for member in value)
    if isinstance(value, set):
        return set(_clone_for_use(member) for member in value)
    if isinstance(value, collections.abc.Mapping):
        return collections.OrderedDict(
                (key, _clone_for_use(member)) for key, member in value.items()
        )
    return value


def _snapshot_exception(error: BaseException) -> BaseException:
    # 复制一份用于缓存并剥离异常栈上下文，避免跨线程/跨版本传播内部 traceback 与帧引用
    try:
        snapshot = copy.copy(error)
    except Exception:
        snapshot = error
    snapshot.__traceback__ = None
    snapshot.__cause__ = None
    snapshot.__context__ = None
    return snapshot


def _restore_exception(snapshot: BaseException) -> BaseException:
    try:
        restored = copy.copy(snapshot)
    except Exception:
        restored = snapshot
    restored.__traceback__ = None
    restored.__cause__ = None
    restored.__context__ = None
    return restored


class IncrementalRevision(object):
    """一个输入版本下的一次或多次（可并发、跨线程）评估。

    作为上下文管理器使用：进入时把本修订版绑定到当前线程的
    :py:class:`~rule_engine.Context` TLS，退出时恢复。同一个修订版对象可以被多个线程分别
    进入，以共享同一版本的只读结果；新版本（:py:meth:`IncrementalSession.revise`）在所有
    线程退出前不能开启。
    """
    def __init__(self, session: 'IncrementalSession', version: int, changes: _ChangeSet) -> None:
        self.session = session
        self.version = version
        self.changes = changes
        self._thing: Any = None
        self._thing_bound = threading.Event()

    @property
    def context(self) -> 'Context':
        return self.session.rule.context

    def __enter__(self) -> 'IncrementalRevision':
        storage = self.context._tls
        storage.incr_previous = storage.incremental
        storage.incremental = self
        self.session._revisions_open += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        storage = self.context._tls
        storage.incremental = getattr(storage, 'incr_previous', None)
        storage.incr_previous = None
        self.session._revisions_open -= 1

    def evaluate(self, thing: Any) -> Any:
        """在本修订版下评估规则（要求当前线程已进入该修订版）。

        同一个修订版只能绑定一个输入对象：并发线程应评估同一个 *thing*。跨对象评估必须开启
        新修订版；会话检测到输入对象身份变化时会丢弃全部旧条目，避免不同上下文的数据串用。
        """
        if self.context._tls.incremental is not self:
            raise errors.EngineError(
                    'an incremental revision must be activated (use it as a context manager) before evaluating'
            )
        session = self.session
        with session._lock:
            if not self._thing_bound.is_set():
                # 首个线程绑定输入对象；若与上一修订版不是同一对象，旧条目一律作废
                if session._last_thing is not None and session._last_thing is not thing:
                    session._store.clear()
                    session._events.clear()
                session._last_thing = thing
                self._thing = thing
                self._thing_bound.set()
            elif self._thing is not thing:
                raise errors.EngineError(
                        'all evaluations of one revision must use the same input object; '
                        'open a new revision to evaluate a different thing'
                )
        return session.rule.evaluate(thing)

    def matches(self, thing: Any) -> bool:
        return bool(self.evaluate(thing))


class IncrementalSession(object):
    """跨多个输入版本保留节点中间结果的增量评估会话。

    一个会话绑定一个 :py:class:`~rule_engine.Rule`，因此不同规则实例、不同上下文之间绝不
    会串用缓存。典型用法::

        session = rule.incremental_session()
        result = session.evaluate(thing, changed_fields=None)          # 首版：全量
        thing['price'] = new_price
        result = session.evaluate(thing, changed_fields={'price'})    # 只重算受影响节点
    """
    def __init__(self, rule: 'Rule') -> None:
        self.rule = rule
        self._store: dict[ast.ExpressionBase, _Entry] = {}
        self._events: dict[ast.ExpressionBase, threading.Event] = {}
        self._lock = threading.Lock()
        self._revisions_open = 0
        self._next_version = 1
        self._last_thing: Any = None
        self.stats: collections.Counter[str] = collections.Counter()
        """计数：``recomputed`` 实际重算、``reused`` 同版本并发共享、``promoted`` 旧版本结果晋升、
        ``bypassed`` 波动/副作用或推导体节点按要求每次执行、``constant`` 编译期常量直接求值。"""

    def revise(
            self,
            changed_fields: Iterable[str] | None = (),
            *,
            changed_resolvers: Iterable[str] = (),
            changed_functions: Iterable[str] = (),
            external_change: bool = False
    ) -> IncrementalRevision:
        """开启一个新的输入版本。

        :param changed_fields: 相对上一版本发生变化的根输入字段；传 ``None`` 或设置
            *external_change* 表示无法用字段描述的外部变化，本版本所有节点都重新计算。
        :param changed_resolvers: 行为可能发生变化的解析器标签（见 NodeDependencies.resolvers）。
        :param changed_functions: 行为可能发生变化的函数标签（纯度注册表之外的热更新等）。
        """
        if external_change or changed_fields is None:
            fields: frozenset[str] | None = None
        else:
            fields = frozenset(changed_fields)
        changes = _ChangeSet(
                fields=fields,
                resolvers=frozenset(changed_resolvers),
                functions=frozenset(changed_functions)
        )
        with self._lock:
            if self._revisions_open:
                raise errors.EngineError('can not open a new revision while a previous one is still active')
            revision = IncrementalRevision(self, self._next_version, changes)
            self._next_version += 1
        return revision

    def evaluate(
            self,
            thing: Any,
            changed_fields: Iterable[str] | None = (),
            *,
            changed_resolvers: Iterable[str] = (),
            changed_functions: Iterable[str] = (),
            external_change: bool = False
    ) -> Any:
        """便捷方法：开启修订版、在当前线程评估一次并关闭。"""
        revision = self.revise(
                changed_fields,
                changed_resolvers=changed_resolvers,
                changed_functions=changed_functions,
                external_change=external_change
        )
        with revision:
            return revision.evaluate(thing)

    # -- 内部：由 ast.route_evaluate 调用 ------------------------------------------------------
    def _route(self, revision: IncrementalRevision, node: ast.ExpressionBase, thing: Any) -> Any:
        storage = self.rule.context._tls
        if storage.incr_uncacheable_depth:
            # 集合推导体内（按元素变化）不做节点共享
            self.stats['bypassed'] += 1
            return node.evaluate(thing)

        deps: NodeDependencies | None = getattr(node, '_deps', None)
        if deps is None or deps.non_cacheable:
            # 波动/副作用节点：每次都真实执行；其子节点仍会经过本路由各自复用
            self.stats['bypassed'] += 1
            return node.evaluate(thing)

        if not (deps.fields or deps.resolvers or deps.functions):
            # 编译期常量（归约后的字面量等）：与输入版本无关，直接求值即可，不必占用缓存
            self.stats['constant'] += 1
            return node.evaluate(thing)

        while True:
            entry = self._lookup_usable_entry(revision, node, deps)
            if entry is not None:
                return self._materialize_entry(entry)

            with self._lock:
                event = self._events.get(node)
                if event is None:
                    event = threading.Event()
                    self._events[node] = event
                    owner = True
                else:
                    owner = False

            if owner:
                return self._compute(revision, node, thing, event)
            event.wait()
            # 同伴线程已在当前版本落盘（值或异常），回到查找步骤复用或一致地重抛

    def _compute(
            self, revision: IncrementalRevision, node: ast.ExpressionBase, thing: Any, event: threading.Event
    ) -> Any:
        try:
            value = node.evaluate(thing)
        except BaseException as error:
            with self._lock:
                self._store[node] = _Entry(revision.version, error=_snapshot_exception(error), is_error=True)
                self._events.pop(node, None)
            event.set()
            raise
        # 缓存与返回值互相隔离：调用方就地修改结果不会污染缓存；复制万一失败，宁可保留原值
        # （退化为不隔离）也必须释放等待者，避免同伴线程永久阻塞
        try:
            stored = _clone_for_store(value)
        except Exception:
            stored = value
        with self._lock:
            self._store[node] = _Entry(revision.version, value=stored)
            self._events.pop(node, None)
        event.set()
        self.stats['recomputed'] += 1
        return value

    def _lookup_usable_entry(
            self, revision: IncrementalRevision, node: ast.ExpressionBase, deps: NodeDependencies
    ) -> _Entry | None:
        with self._lock:
            entry = self._store.get(node)
            if entry is None:
                return None
            if entry.version == revision.version:
                self.stats['reused'] += 1
                return entry
            # 仅允许从紧邻上一版本晋升：连续多版未受影响的节点会逐版晋升，一直被沿用；
            # 更老的条目说明中间跨过了未评估版本，不复用
            if entry.version == revision.version - 1 and not revision.changes.affects(deps):
                promoted = _Entry(
                        revision.version,
                        value=entry.value,
                        error=entry.error,
                        is_error=entry.is_error
                )
                self._store[node] = promoted
                self.stats['promoted'] += 1
                return promoted
            return None

    def _materialize_entry(self, entry: _Entry) -> Any:
        if entry.is_error:
            assert entry.error is not None
            raise _restore_exception(entry.error) from None
        return _clone_for_use(entry.value)
