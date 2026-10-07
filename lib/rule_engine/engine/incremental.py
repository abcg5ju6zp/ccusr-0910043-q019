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
#    copyright notice, this list of conditions and the following disclaimer
#    in the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the project nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

"""
增量求值支持。

定价服务等场景每次只变更一个输入字段，全量重算会重复执行整棵规则树与所有
昂贵的解析函数。本模块在编译期记录字段、解析器（解析函数）与自定义函数之间
的真实依赖；调用方提交变更集（changeset）后，求值仅重新计算受影响的节点，
未受影响的中间结果在同一输入版本内被并发安全地共享。

缓存严格挂在单个 :py:class:`IncrementalRule` 实例上：不同规则实例、不同
上下文之间绝不串用；同一输入版本的并发求值共享只读缓存条目。
"""

import decimal
import threading
from typing import Any, Iterable, Iterator

from .. import ast
from .. import errors
from ..types import DataType
from ..types import _DataTypeDef, _FunctionDataTypeDef

from .context import Context
from .rule import Rule

__all__ = ('IncrementalRule',)

# 结果不确定的内置函数，调用点每次求值都必须重新执行
_VOLATILE_BUILTIN_FUNCTIONS = frozenset(('random',))
# 由 BuiltinValueGenerator 生成、但生成结果确定（可安全缓存）的内置符号
_DETERMINISTIC_VALUE_GENERATORS = frozenset(('parse_datetime',))
# 读取线程局部正则匹配结果的内置符号
_REGEX_GROUPS_SYMBOL = 're_groups'
# 内置作用域名称（与 parser 中 '$' 前缀对应）
_BUILTIN_SCOPE = 'built-in'
# 方法调用可视为纯函数的不可变标量类型
_PURE_METHOD_TYPES = (
        DataType.STRING, DataType.BYTES, DataType.FLOAT,
        DataType.BOOLEAN, DataType.DATETIME, DataType.TIMEDELTA,
)
# 不包含子表达式的标量字面量节点（编译时无需包装）
_SCALAR_LITERAL_TYPES = (
        ast.BooleanExpression, ast.BytesExpression, ast.DatetimeExpression,
        ast.FloatExpression, ast.FunctionExpression, ast.NullExpression,
        ast.StringExpression, ast.TimedeltaExpression,
)

def _iter_children(node: ast.ExpressionBase) -> Iterator[ast.ExpressionBase]:
    """遍历表达式节点的全部直接子表达式。"""
    if isinstance(node, (ast.BinaryExpressionBase, ast.CoalesceExpression)):
        yield node.left
        yield node.right
    elif isinstance(node, ast.UnaryExpression):
        yield node.right
    elif isinstance(node, ast.TernaryExpression):
        yield node.condition
        yield node.case_true
        yield node.case_false
    elif isinstance(node, ast.ComprehensionExpression):
        yield node.result
        yield node.iterable
        if node.condition is not None:
            yield node.condition
    elif isinstance(node, ast.ContainsExpression):
        yield node.container
        yield node.member
    elif isinstance(node, ast.GetAttributeExpression):
        yield node.object
    elif isinstance(node, ast.GetItemExpression):
        yield node.container
        yield node.item
    elif isinstance(node, ast.GetSliceExpression):
        yield node.container
        yield node.start
        yield node.stop
    elif isinstance(node, ast.FunctionCallExpression):
        yield node.function
        yield from node.arguments
    elif isinstance(node, ast.MappingExpression):
        for key, value in node.value:
            yield key
            yield value
    elif isinstance(node, ast._CollectionMixin):
        yield from node.value

def _scan_regex_groups(node: ast.ExpressionBase) -> bool:
    """检查规则是否引用了线程局部的 $re_groups 符号。"""
    if isinstance(node, ast.SymbolExpression):
        return node.scope == _BUILTIN_SCOPE and node.name == _REGEX_GROUPS_SYMBOL
    return any(_scan_regex_groups(child) for child in _iter_children(node))

def _label(node: ast.ExpressionBase) -> str:
    """生成节点的可读标签，用于依赖报告。"""
    parts = [node.__class__.__name__]
    for attribute in ('name', 'type', 'variable'):
        value = getattr(node, attribute, None)
        if isinstance(value, str):
            parts.append(repr(value))
    return ' '.join(parts)

def _subsume_prefixes(frame: set[tuple], path: tuple) -> None:
    """从读取集合中移除被 *path* 覆盖的真前缀读取。

    纯净的符号/属性链上，父节点记录了更深层路径后，链上的浅层读取已被该
    路径的前缀版本戳覆盖，继续保留只会让无关的兄弟字段变化误伤本节点。
    """
    plen = len(path)
    for other in tuple(frame):
        if len(other) < plen and path[:len(other)] == other:
            frame.discard(other)

# _literal_scalar 无法确定时的哨兵
_NO_LITERAL = object()

def _literal_scalar(node: ast.ExpressionBase) -> Any:
    """提取标量字面量的值（用于静态下标路径），否则返回哨兵。"""
    if isinstance(node, (ast.StringExpression, ast.BytesExpression, ast.FloatExpression, ast.BooleanExpression)):
        return node.value
    return _NO_LITERAL

class _NodeInfo(object):
    """单个 AST 节点的编译期依赖记录。"""
    __slots__ = (
            'node_id', 'kind', 'label', 'volatile', 'is_comprehension',
            'read_path', 'read_subsume', 'item_base', 'item_composite',
            'static_path', 'static_clean', 'static_exact',
            'fields', 'functions', 'impure_functions',
    )
    def __init__(self, kind: str, label: str) -> None:
        self.node_id = -1
        self.kind = kind
        self.label = label
        # 易变节点（自身或后代含副作用 / 不确定求值）永不缓存
        self.volatile = False
        self.is_comprehension = False
        # 求值前记录的读取路径（符号 / 属性 / 切片）
        self.read_path: tuple | None = None
        # 记录 read_path 后是否吸收掉链上的真前缀读取（仅限纯净属性链）
        self.read_subsume = False
        # GetItem 的容器静态路径与是否记录运行时精确下标
        self.item_base: tuple | None = None
        self.item_composite = False
        # 表达式读取的输入静态路径（无法静态确定时为 None）与是否纯净符号/属性链
        self.static_path: tuple | None = None
        self.static_clean = False
        # 静态路径是否恰好指向本表达式的值（切片等情形下路径只是保守依赖）
        self.static_exact = False
        # 依赖报告：传递闭包内的字段路径、调用函数与不确定函数
        self.fields: frozenset[tuple] = frozenset()
        self.functions: frozenset[str] = frozenset()
        self.impure_functions: frozenset[str] = frozenset()

class _CacheEntry(object):
    """一次求值的缓存结果（正常值或引擎异常）及其读取集合的版本戳。"""
    __slots__ = ('outcome', 'is_error', 'paths', 'exact', 'subtree', 'epoch')
    def __init__(
                    self,
                    outcome: Any,
                    is_error: bool,
                    paths: frozenset,
                    exact: dict[tuple, int],
                    subtree: dict[tuple, int],
                    epoch: int
    ) -> None:
        self.outcome = outcome
        self.is_error = is_error
        self.paths = paths
        # 每条读取路径的全部前缀的精确版本：变更集落在任一前缀上都会失效
        self.exact = exact
        # 每条读取路径自身的子树版本：变更集落在路径之下（更深层）也会失效
        self.subtree = subtree
        self.epoch = epoch

    def reveal(self) -> Any:
        if self.is_error:
            raise self.outcome
        return self.outcome

class _EvaluationState(object):
    """单次求值的线程局部状态：版本快照与读取帧栈。"""
    __slots__ = ('exact', 'subtree', 'epoch', 'frames', 'suspend')
    def __init__(self, exact: dict[tuple, int], subtree: dict[tuple, int], epoch: int) -> None:
        self.exact = exact
        self.subtree = subtree
        self.epoch = epoch
        # 帧栈底是基础帧，吸收无缓存祖先的读取
        self.frames: list[set[tuple]] = [set()]
        # 位于集合推导式体内时暂停节点级缓存（循环变量每次迭代都变）
        self.suspend = 0

class _DependencyTracker(object):
    """:py:class:`IncrementalRule` 私有的版本与缓存状态，实例之间绝不共享。"""
    def __init__(self) -> None:
        self.slots: list[_CacheEntry | None] = []
        self.lock = threading.Lock()
        self.exact_versions: dict[tuple, int] = {}
        self.subtree_versions: dict[tuple, int] = {}
        self.epoch = 0
        self.thing: Any = None
        self._tls = threading.local()

    @property
    def current_state(self) -> _EvaluationState | None:
        states = getattr(self._tls, 'states', None)
        return states[-1] if states else None

    def begin(self, thing: Any) -> _EvaluationState:
        """开始一次求值：必要时按输入对象身份做全量失效，并拍版本快照。"""
        with self.lock:
            if thing is not self.thing:
                # 输入对象被整体替换，旧版本记录对新输入没有意义
                self.thing = thing
                self._reset_locked()
            state = _EvaluationState(dict(self.exact_versions), dict(self.subtree_versions), self.epoch)
        states = getattr(self._tls, 'states', None)
        if states is None:
            states = self._tls.states = []
        states.append(state)
        return state

    def end(self, state: _EvaluationState) -> None:
        self._tls.states.pop()

    def submit(self, paths: Iterable[tuple]) -> int:
        """提交变更路径：提升路径自身（精确）及其全部祖先（子树）的版本。"""
        with self.lock:
            for path in paths:
                self.exact_versions[path] = self.exact_versions.get(path, 0) + 1
                for index in range(1, len(path) + 1):
                    prefix = path[:index]
                    self.subtree_versions[prefix] = self.subtree_versions.get(prefix, 0) + 1
            self.epoch += 1
            return self.epoch

    def reset(self) -> None:
        with self.lock:
            self.thing = None
            self._reset_locked()

    def _reset_locked(self) -> None:
        for index in range(len(self.slots)):
            self.slots[index] = None
        self.exact_versions.clear()
        self.subtree_versions.clear()
        self.epoch += 1

    def store(self, node_id: int, entry: _CacheEntry) -> None:
        with self.lock:
            self.slots[node_id] = entry

    @staticmethod
    def entry_valid(entry: _CacheEntry, state: _EvaluationState) -> bool:
        # 条目写入后没有提交过任何变更集，直接有效
        if entry.epoch == state.epoch:
            return True
        exact_get = state.exact.get
        for path, version in entry.exact.items():
            if exact_get(path, 0) != version:
                return False
        subtree_get = state.subtree.get
        for path, version in entry.subtree.items():
            if subtree_get(path, 0) != version:
                return False
        return True

def _make_entry(state: _EvaluationState, frame: set[tuple], outcome: Any, is_error: bool) -> _CacheEntry:
    """把读取帧固化为带版本戳的缓存条目。"""
    exact: dict[tuple, int] = {}
    subtree: dict[tuple, int] = {}
    exact_get = state.exact.get
    subtree_get = state.subtree.get
    for path in frame:
        for index in range(1, len(path) + 1):
            prefix = path[:index]
            if prefix not in exact:
                exact[prefix] = exact_get(prefix, 0)
        subtree[path] = subtree_get(path, 0)
    return _CacheEntry(outcome, is_error, frozenset(frame), exact, subtree, state.epoch)

class _TrackedExpression(ast.ExpressionBase):
    """包装一个编译后的子表达式：求值时记录读取集合并按需缓存结果。"""
    __slots__ = ('_child', '_info', '_tracker', 'result_type')
    result_type: _DataTypeDef
    def __init__(self, child: ast.ExpressionBase, info: _NodeInfo, tracker: _DependencyTracker) -> None:
        self.context = child.context
        self._child = child
        self._info = info
        self._tracker = tracker
        self.result_type = child.result_type

    def __repr__(self) -> str:
        return repr(self._child)

    def evaluate(self, thing: Any) -> Any:
        tracker = self._tracker
        state = tracker.current_state
        if state is None:
            # 未处于增量求值中（例如直接调用 statement.evaluate），透传
            return self._child.evaluate(thing)
        info = self._info
        if info.volatile or state.suspend:
            return self._compute(thing, state, memo=False)
        entry = tracker.slots[info.node_id]
        if entry is not None and tracker.entry_valid(entry, state):
            state.frames[-1].update(entry.paths)
            return entry.reveal()
        return self._compute(thing, state, memo=True)

    def _compute(self, thing: Any, state: _EvaluationState, memo: bool) -> Any:
        info = self._info
        tracker = self._tracker
        if memo:
            frame: set[tuple] = set()
            state.frames.append(frame)
        else:
            # 易变节点与被暂停的节点直接把读取记录到当前帧
            frame = state.frames[-1]
        if info.is_comprehension:
            state.suspend += 1
        try:
            if info.read_path is not None:
                frame.add(info.read_path)
                if info.read_subsume:
                    _subsume_prefixes(frame, info.read_path)
            if info.item_base is not None:
                frame.add(info.item_base)
            try:
                value = self._child.evaluate(thing)
            except errors.EngineError as error:
                # 异常也是求值结果：按同样的读取集合缓存，变更后正确重试
                if memo:
                    tracker.store(info.node_id, _make_entry(state, frame, error, True))
                raise
            if info.item_composite and not state.suspend:
                self._record_item_read(thing, frame)
        finally:
            if info.is_comprehension:
                state.suspend -= 1
            if memo:
                state.frames.pop()
                state.frames[-1].update(frame)
        if memo:
            tracker.store(info.node_id, _make_entry(state, frame, value, False))
        return value

    def _record_item_read(self, thing: Any, frame: set[tuple]) -> None:
        """为 GetItem 记录运行时精确下标路径（容器与下标均为纯子表达式）。"""
        info = self._info
        tracker = self._tracker
        assert info.item_base is not None
        child = self._child
        assert isinstance(child, ast.GetItemExpression)
        container = child.container.evaluate(thing)
        if container is None and child.safe:
            # 原求值在安全导航下短路，并未读取下标表达式
            return
        item = child.item.evaluate(thing)
        # 与 GetItemExpression.evaluate 保持一致的整数下标归一化
        if isinstance(container, (bytes, str, tuple)):
            item = int(item)
        try:
            hash(item)
        except TypeError:
            # 下标不可哈希时退化为整容器依赖（item_base 已记录）
            return
        frame.add(info.item_base + (item,))
        # 精确下标路径的前缀版本戳已覆盖容器被整体替换的情形；容器链上的
        # 整对象读取可以移除，除非下标子表达式自身也读取了同一路径（例如
        # user[f(user)]），避免兄弟字段变化误伤本节点
        item_child = child.item
        item_paths: frozenset = frozenset()
        if isinstance(item_child, _TrackedExpression):
            item_entry = tracker.slots[item_child._info.node_id]
            if item_entry is not None:
                item_paths = item_entry.paths
        if info.item_base not in item_paths:
            frame.discard(info.item_base)

    def to_graphviz(self, digraph: Any, *args: Any, **kwargs: Any) -> None:
        digraph.node(str(id(self)), 'Tracked')
        self._child.to_graphviz(digraph, *args, **kwargs)
        digraph.edge(str(id(self)), str(id(self._child)))

class _Compiler(object):
    """把解析后的 AST 编译为带依赖记录的跟踪表达式树。"""
    def __init__(self, context: Any, pure_functions: frozenset, tracker: _DependencyTracker) -> None:
        self.context = context
        self.pure_functions = pure_functions
        self.tracker = tracker
        self.nodes: list[_NodeInfo] = []
        builtins = context.builtins
        volatile = set(_VOLATILE_BUILTIN_FUNCTIONS)
        for name in builtins:
            if name not in _DETERMINISTIC_VALUE_GENERATORS and builtins.is_value_generator(name):
                volatile.add(name)
        # 结果不确定的内置符号（now、today、re_groups、random 等）
        self.volatile_builtins = frozenset(volatile)
        self.uses_regex_groups = False

    def compile(self, expression: ast.ExpressionBase) -> _TrackedExpression:
        self.uses_regex_groups = _scan_regex_groups(expression)
        info = self._compile(expression, frozenset())
        wrapped = self._wrap(expression, info, force=True)
        assert isinstance(wrapped, _TrackedExpression)
        return wrapped

    def _wrap(self, node: ast.ExpressionBase, info: _NodeInfo, force: bool = False) -> ast.ExpressionBase:
        if not force and isinstance(node, _SCALAR_LITERAL_TYPES):
            return node
        info.node_id = len(self.nodes)
        self.nodes.append(info)
        self.tracker.slots.append(None)
        return _TrackedExpression(node, info, self.tracker)

    @staticmethod
    def _merge(info: _NodeInfo, child_infos: Iterable[_NodeInfo]) -> None:
        fields = set(info.fields)
        functions = set(info.functions)
        impure = set(info.impure_functions)
        for child_info in child_infos:
            info.volatile = info.volatile or child_info.volatile
            fields.update(child_info.fields)
            functions.update(child_info.functions)
            impure.update(child_info.impure_functions)
        info.fields = frozenset(fields)
        info.functions = frozenset(functions)
        info.impure_functions = frozenset(impure)

    def _compile_attrs(self, node: ast.ExpressionBase, attrs: tuple[str, ...], bound: frozenset) -> dict[str, _NodeInfo]:
        return {attr: self._compile(getattr(node, attr), bound) for attr in attrs}

    def _wrap_attrs(self, node: ast.ExpressionBase, attrs: tuple[str, ...], infos: dict[str, _NodeInfo]) -> None:
        for attr in attrs:
            setattr(node, attr, self._wrap(getattr(node, attr), infos[attr]))

    def _compile(self, node: ast.ExpressionBase, bound: frozenset) -> _NodeInfo:
        info = _NodeInfo(node.__class__.__name__, _label(node))
        if isinstance(node, (ast.BinaryExpressionBase, ast.CoalesceExpression)):
            attrs: tuple[str, ...] = ('left', 'right')
            infos = self._compile_attrs(node, attrs, bound)
            self._merge(info, infos.values())
            # 正则匹配会写线程局部的 re_groups，被消费时必须每次重放
            if isinstance(node, ast.FuzzyComparisonExpression) and self.uses_regex_groups:
                info.volatile = True
            self._wrap_attrs(node, attrs, infos)
        elif isinstance(node, ast.UnaryExpression):
            attrs = ('right',)
            infos = self._compile_attrs(node, attrs, bound)
            self._merge(info, infos.values())
            self._wrap_attrs(node, attrs, infos)
        elif isinstance(node, ast.TernaryExpression):
            attrs = ('condition', 'case_true', 'case_false')
            infos = self._compile_attrs(node, attrs, bound)
            self._merge(info, infos.values())
            self._wrap_attrs(node, attrs, infos)
        elif isinstance(node, ast.ContainsExpression):
            attrs = ('container', 'member')
            infos = self._compile_attrs(node, attrs, bound)
            self._merge(info, infos.values())
            self._wrap_attrs(node, attrs, infos)
        elif isinstance(node, ast.ComprehensionExpression):
            # 推导式作为整体缓存；体内暂停节点级缓存，避免循环变量串值
            info.is_comprehension = True
            inner_bound = bound | {node.variable}
            iterable_info = self._compile(node.iterable, bound)
            result_info = self._compile(node.result, inner_bound)
            condition = node.condition
            condition_info = self._compile(condition, inner_bound) if condition is not None else None
            child_infos = [iterable_info, result_info] + ([condition_info] if condition_info is not None else [])
            self._merge(info, child_infos)
            node.iterable = self._wrap(node.iterable, iterable_info)
            node.result = self._wrap(node.result, result_info)
            if condition is not None and condition_info is not None:
                node.condition = self._wrap(condition, condition_info)
        elif isinstance(node, ast.GetAttributeExpression):
            attrs = ('object',)
            infos = self._compile_attrs(node, attrs, bound)
            self._merge(info, infos.values())
            base = infos['object'].static_path
            if base is not None:
                info.read_path = base + (node.name,)
                # 只有纯净的属性链才能吸收链上的前缀读取
                info.read_subsume = infos['object'].static_clean
                info.static_path = info.read_path
                info.static_clean = infos['object'].static_clean
                info.static_exact = infos['object'].static_exact
                info.fields = info.fields | {info.read_path}
            self._wrap_attrs(node, attrs, infos)
        elif isinstance(node, ast.GetItemExpression):
            attrs = ('container', 'item')
            infos = self._compile_attrs(node, attrs, bound)
            self._merge(info, infos.values())
            base = infos['container'].static_path
            if base is not None:
                info.item_base = base
                info.fields = info.fields | {base}
                literal = _literal_scalar(node.item)
                if literal is not _NO_LITERAL and infos['container'].static_exact:
                    # 容器的值恰好是静态路径指向的对象时，下标路径才精确有效
                    info.static_path = base + (literal,)
                    info.static_exact = True
                    info.fields = info.fields | {info.static_path}
                else:
                    info.static_path = base
                # 只有容器的值恰好是静态路径指向的对象时，运行时合成下标路径
                # 才真实有效（切片等会错位下标的情形不得合成）
                if infos['container'].static_exact and not infos['container'].volatile and not infos['item'].volatile:
                    info.item_composite = True
            self._wrap_attrs(node, attrs, infos)
        elif isinstance(node, ast.GetSliceExpression):
            attrs = ('container', 'start', 'stop')
            infos = self._compile_attrs(node, attrs, bound)
            self._merge(info, infos.values())
            base = infos['container'].static_path
            if base is not None:
                info.read_path = base
                info.static_path = base
                info.fields = info.fields | {base}
            self._wrap_attrs(node, attrs, infos)
        elif isinstance(node, ast.SymbolExpression):
            if node.scope == _BUILTIN_SCOPE:
                if node.name in self.volatile_builtins:
                    info.volatile = True
            elif node.scope is None and node.name not in bound:
                info.read_path = (node.name,)
                info.static_path = info.read_path
                info.static_clean = True
                info.static_exact = True
                info.fields = info.fields | {info.read_path}
        elif isinstance(node, ast.FunctionCallExpression):
            function_info = self._compile(node.function, bound)
            argument_infos = [self._compile(argument, bound) for argument in node.arguments]
            self._merge(info, [function_info] + argument_infos)
            name, pure = self._call_purity(node, bound)
            info.functions = info.functions | {name}
            if not pure:
                # 自定义函数可能有副作用：未声明为纯函数时永不缓存
                info.volatile = True
                info.impure_functions = info.impure_functions | {name}
            node.function = self._wrap(node.function, function_info)
            node.arguments = tuple(self._wrap(argument, argument_info) for argument, argument_info in zip(node.arguments, argument_infos))
        elif isinstance(node, ast.MappingExpression):
            pair_infos: list[_NodeInfo] = []
            new_value = []
            for key, value in node.value:
                key_info = self._compile(key, bound)
                value_info = self._compile(value, bound)
                pair_infos.extend((key_info, value_info))
                new_value.append((self._wrap(key, key_info), self._wrap(value, value_info)))
            self._merge(info, pair_infos)
            node.value = tuple(new_value)
        elif isinstance(node, ast._CollectionMixin):
            member_infos = [self._compile(member, bound) for member in node.value]
            self._merge(info, member_infos)
            node.value = type(node.value)(self._wrap(member, member_info) for member, member_info in zip(node.value, member_infos))
        # 其余节点（标量字面量）无子表达式、无读取
        return info

    def _call_purity(self, node: ast.FunctionCallExpression, bound: frozenset) -> tuple[str, bool]:
        """判断函数调用点是否可缓存，返回 (函数名, 是否纯函数)。"""
        function = node.function
        name = '<expression>'
        if isinstance(function, (ast.SymbolExpression, ast.GetAttributeExpression)):
            name = function.name
        pure = self._is_pure_function_expression(function, bound)
        if pure:
            function_type = function.result_type
            if isinstance(function_type, _FunctionDataTypeDef) and isinstance(function_type.argument_types, tuple):
                # 高阶函数：函数类型的实参也必须是纯函数表达式
                for argument, argument_type in zip(node.arguments, function_type.argument_types):
                    if DataType.is_type(argument_type, DataType.FUNCTION) and not self._is_pure_function_expression(argument, bound):
                        return name, False
        return name, pure

    def _is_pure_function_expression(self, expression: ast.ExpressionBase, bound: frozenset) -> bool:
        if isinstance(expression, ast.SymbolExpression):
            if expression.scope == _BUILTIN_SCOPE:
                return expression.name not in self.volatile_builtins
            if expression.scope is None:
                return expression.name in self.pure_functions and expression.name not in bound
            return False
        if isinstance(expression, ast.GetAttributeExpression):
            # 不可变标量类型的方法（如 str.upper）无副作用
            object_type = DataType.NULLABLE.unwrap(expression.object.result_type)
            return object_type in _PURE_METHOD_TYPES
        return False

class IncrementalRule(Rule):
    """支持变更集驱动增量求值的规则。

    编译结果记录字段、解析器（解析函数）与自定义函数之间的真实依赖；调用方
    在输入变化后通过 :py:meth:`submit_changes` 提交变更集，后续求值仅重算受
    影响的节点。同一输入版本的并发求值共享只读缓存；不同上下文或规则实例的
    缓存互不串用。动态属性、集合推导、自定义函数副作用与异常结果都会正确使
    缓存失效。

    自定义解析器（resolver）必须对同一输入版本返回确定结果；有副作用的自定
    义函数默认每次重算，可用 *pure_functions* 声明为纯函数以参与缓存。
    """
    def __init__(self, text: str, context: Context | None = None, *, pure_functions: Iterable[str] = ()) -> None:
        """项目内部接口说明。"""
        self._pure_functions = frozenset(pure_functions)
        super(IncrementalRule, self).__init__(text, context)
        self._compile_incremental()

    def _compile_incremental(self) -> None:
        tracker = _DependencyTracker()
        compiler = _Compiler(self.context, self._pure_functions, tracker)
        self.statement.expression = compiler.compile(self.statement.expression)
        self._tracker = tracker
        self._nodes = compiler.nodes
        self._volatile_builtins = compiler.volatile_builtins
        self._uses_regex_groups = compiler.uses_regex_groups

    def __getstate__(self) -> dict[str, Any]:
        state = super(IncrementalRule, self).__getstate__()
        state['pure_functions'] = self._pure_functions
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self._pure_functions = frozenset(state['pure_functions'])
        super(IncrementalRule, self).__setstate__(state)
        self._compile_incremental()

    @property
    def version(self) -> int:
        """当前输入版本号（每次提交变更集或重置后递增）。"""
        return self._tracker.epoch

    def reset(self) -> None:
        """清空全部缓存结果与版本记录，下次求值将完整重算。"""
        self._tracker.reset()

    def submit_changes(self, changed: Any) -> int:
        """提交变更集：使受影响的缓存节点失效，返回新的输入版本号。

        *changed* 可以是单个字段名、单条路径（元组），或由字段名 / 路径组成
        的可迭代对象。字符串表示根字段；嵌套变化用路径元组表示，例如
        ``('user', 'age')``。变更集会同时使该路径的祖先与后代读取失效。
        """
        if isinstance(changed, (str, tuple)):
            changed = (changed,)
        paths = []
        for item in changed:
            path = (item,) if isinstance(item, str) else tuple(item)
            if not path:
                raise ValueError('change paths may not be empty')
            paths.append(path)
        return self._tracker.submit(paths)

    def evaluate(self, thing: Any) -> Any:
        """增量求值：仅重算自上次求值以来受变更集影响的节点。"""
        tracker = self._tracker
        state = tracker.begin(thing)
        try:
            self.context._tls.reset()
            with decimal.localcontext(self.context.decimal_context):
                return self.statement.expression.evaluate(thing)
        finally:
            tracker.end(state)

    def cache_info(self) -> dict[str, int]:
        """返回缓存概况：依赖图节点数、已缓存条目数与当前输入版本。"""
        slots = self._tracker.slots
        return {
                'nodes': len(slots),
                'entries': sum(1 for slot in slots if slot is not None),
                'version': self._tracker.epoch,
        }

    def dependency_report(self) -> dict[str, Any]:
        """返回编译期记录的依赖信息：字段路径、调用函数与易变节点。"""
        fields: set[tuple] = set()
        functions: set[str] = set()
        impure_functions: set[str] = set()
        nodes = []
        for info in self._nodes:
            fields.update(info.fields)
            functions.update(info.functions)
            impure_functions.update(info.impure_functions)
            nodes.append({
                    'id': info.node_id,
                    'kind': info.kind,
                    'label': info.label,
                    'volatile': info.volatile,
                    'fields': sorted(info.fields, key=repr),
            })
        return {
                'text': self.text,
                'fields': sorted({path[0] for path in fields}),
                'field_paths': sorted(fields, key=repr),
                'functions': sorted(functions),
                'impure_functions': sorted(impure_functions),
                'volatile_builtins': sorted(self._volatile_builtins),
                'uses_regex_groups': self._uses_regex_groups,
                'nodes': nodes,
        }
