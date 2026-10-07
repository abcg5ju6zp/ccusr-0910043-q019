#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  tests/incremental.py
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

import collections
import dataclasses
import pickle
import random
import threading
import unittest

import rule_engine
import rule_engine.engine as engine
import rule_engine.errors as errors

__all__ = ('IncrementalRuleTests',)

class CountingResolver(object):
    """记录每个符号被解析次数的 resolver，用于验证只重算受影响节点。"""
    def __init__(self):
        self.counts = collections.Counter()
        self.lock = threading.Lock()

    def __call__(self, thing, name):
        with self.lock:
            self.counts[name] += 1
        return engine.resolve_item(thing, name)

    def reset(self):
        with self.lock:
            self.counts.clear()

    @property
    def total(self):
        return sum(self.counts.values())

@dataclasses.dataclass
class _User:
    age: int
    name: str

@dataclasses.dataclass
class _Order:
    user: _User
    qty: int = 0

@dataclasses.dataclass
class _MaybeOrder:
    user: _User | None
    qty: int = 0

def _order_context(cls=_Order):
    return engine.Context(
            resolver=engine.resolve_attribute,
            type_resolver=engine.type_resolver_from_dataclass(cls),
    )

class IncrementalRuleTests(unittest.TestCase):
    def assert_rule_parity(self, text, thing, steps, context=None, pure_functions=()):
        """逐步变更输入，验证 IncrementalRule 与全量求值的 Rule 结果一致。"""
        incremental = engine.IncrementalRule(text, context=context, pure_functions=pure_functions)
        reference = engine.Rule(text, context=context)

        def check():
            try:
                expected = reference.evaluate(thing)
            except Exception as error:
                expected = error
            try:
                actual = incremental.evaluate(thing)
            except Exception as error:
                actual = error
            if isinstance(expected, Exception):
                self.assertIs(type(actual), type(expected), 'rule: ' + text)
            else:
                self.assertEqual(actual, expected, 'rule: ' + text)

        check()
        for mutate, changes in steps:
            mutate(thing)
            if changes is not None:
                incremental.submit_changes(changes)
            check()

    def test_basic_incremental_recompute(self):
        resolver = CountingResolver()
        context = engine.Context(resolver=resolver)
        rule = engine.IncrementalRule('price * qty - discount', context=context)
        thing = {'price': 2, 'qty': 3, 'discount': 1}
        self.assertEqual(rule.evaluate(thing), 5)
        self.assertEqual(resolver.total, 3)

        # 无变更：任何字段都不重新解析
        resolver.reset()
        self.assertEqual(rule.evaluate(thing), 5)
        self.assertEqual(resolver.total, 0)

        # 变更一个字段：只有受影响的节点重算
        thing['discount'] = 2
        rule.submit_changes({'discount'})
        resolver.reset()
        self.assertEqual(rule.evaluate(thing), 4)
        self.assertEqual(resolver.counts['discount'], 1)
        self.assertEqual(resolver.counts['price'], 0)
        self.assertEqual(resolver.counts['qty'], 0)

        thing['price'] = 4
        rule.submit_changes({'price'})
        resolver.reset()
        self.assertEqual(rule.evaluate(thing), 10)
        self.assertEqual(resolver.counts['price'], 1)
        self.assertEqual(resolver.counts['discount'], 0)

    def test_expensive_parser_reuse(self):
        parse_calls = []
        def parse_price(text):
            parse_calls.append(text)
            return float(text)
        resolver = CountingResolver()
        context = engine.Context(resolver=resolver)
        thing = {'price': '1.5', 'qty': 3, 'parse_price': parse_price}
        rule = engine.IncrementalRule('parse_price(price) * qty', context=context, pure_functions={'parse_price'})
        self.assertEqual(rule.evaluate(thing), 4.5)
        self.assertEqual(parse_calls, ['1.5'])

        # 无关字段变化：解析器不重跑
        thing['qty'] = 4
        rule.submit_changes({'qty'})
        self.assertEqual(rule.evaluate(thing), 6.0)
        self.assertEqual(parse_calls, ['1.5'])

        # 解析器输入变化：重跑一次
        thing['price'] = '2.0'
        rule.submit_changes({'price'})
        self.assertEqual(rule.evaluate(thing), 8.0)
        self.assertEqual(parse_calls, ['1.5', '2.0'])

    def test_custom_function_side_effects(self):
        calls = []
        def record(value):
            calls.append(value)
            return value
        thing = {'f': record, 'v': 1}
        # 未声明纯函数：每次求值都重新执行（副作用必须生效）
        rule = engine.IncrementalRule('f(v)')
        self.assertEqual(rule.evaluate(thing), 1)
        self.assertEqual(rule.evaluate(thing), 1)
        self.assertEqual(calls, [1, 1])
        # 声明为纯函数：参与缓存
        calls.clear()
        rule = engine.IncrementalRule('f(v)', pure_functions={'f'})
        self.assertEqual(rule.evaluate(thing), 1)
        self.assertEqual(rule.evaluate(thing), 1)
        self.assertEqual(calls, [1])
        # 函数本身被替换也属于变更
        calls.clear()
        def record2(value):
            calls.append('new')
            return value + 1
        thing['f'] = record2
        rule.submit_changes({'f'})
        self.assertEqual(rule.evaluate(thing), 2)
        self.assertEqual(calls, ['new'])

    def test_higher_order_functions(self):
        calls = []
        def plus_one(value):
            calls.append(value)
            return value + 1
        thing = {'f': plus_one, 'arr': (1, 2)}
        rule = engine.IncrementalRule('$map(f, arr)')
        self.assertEqual(rule.evaluate(thing), (2, 3))
        self.assertEqual(rule.evaluate(thing), (2, 3))
        self.assertEqual(calls, [1, 2, 1, 2])
        calls.clear()
        rule = engine.IncrementalRule('$map(f, arr)', pure_functions={'f'})
        self.assertEqual(rule.evaluate(thing), (2, 3))
        self.assertEqual(rule.evaluate(thing), (2, 3))
        self.assertEqual(calls, [1, 2])

    def test_short_circuit_and_or(self):
        rule = engine.IncrementalRule('a and b')
        thing = {'a': False, 'b': True}
        self.assertIs(rule.evaluate(thing), False)
        # b 的变化不影响结果（a 短路），但缓存必须正确失效
        thing['b'] = False
        rule.submit_changes({'b'})
        self.assertIs(rule.evaluate(thing), False)
        thing['a'] = True
        rule.submit_changes({'a'})
        self.assertIs(rule.evaluate(thing), False)
        thing['b'] = True
        rule.submit_changes({'b'})
        self.assertIs(rule.evaluate(thing), True)

        rule = engine.IncrementalRule('a or b')
        thing = {'a': True, 'b': False}
        self.assertIs(rule.evaluate(thing), True)
        thing['a'] = False
        rule.submit_changes({'a'})
        self.assertIs(rule.evaluate(thing), False)

    def test_ternary_branch_switching(self):
        calls = []
        def branch_a():
            calls.append('a')
            return 'A'
        def branch_b():
            calls.append('b')
            return 'B'
        thing = {'flag': True, 'fa': branch_a, 'fb': branch_b}
        rule = engine.IncrementalRule('flag ? fa() : fb()', pure_functions={'fa', 'fb'})
        self.assertEqual(rule.evaluate(thing), 'A')
        self.assertEqual(calls, ['a'])
        # 条件翻转：切到另一分支，旧分支结果不得沿用
        thing['flag'] = False
        rule.submit_changes({'flag'})
        self.assertEqual(rule.evaluate(thing), 'B')
        self.assertEqual(calls, ['a', 'b'])
        # 切回：fa 的输入未变，命中缓存而不是重算
        thing['flag'] = True
        rule.submit_changes({'flag'})
        self.assertEqual(rule.evaluate(thing), 'A')
        self.assertEqual(calls, ['a', 'b'])

    def test_coalesce(self):
        rule = engine.IncrementalRule('a ?? b ?? 3')
        thing = {'a': None, 'b': None}
        self.assertEqual(rule.evaluate(thing), 3)
        thing['b'] = 2
        rule.submit_changes({'b'})
        self.assertEqual(rule.evaluate(thing), 2)
        thing['a'] = 1
        rule.submit_changes({'a'})
        self.assertEqual(rule.evaluate(thing), 1)
        thing['b'] = 99
        rule.submit_changes({'b'})
        self.assertEqual(rule.evaluate(thing), 1)

    def test_comprehension_elements(self):
        # 循环变量每次迭代都变：节点级缓存绝不允许跨迭代串值
        rule = engine.IncrementalRule('[x * 2 for x in items]')
        thing = {'items': (1, 2, 3)}
        self.assertEqual(rule.evaluate(thing), (2, 4, 6))
        self.assertEqual(rule.evaluate(thing), (2, 4, 6))
        thing['items'] = (1, 2, 3, 4)
        rule.submit_changes({'items'})
        self.assertEqual(rule.evaluate(thing), (2, 4, 6, 8))

    def test_comprehension_condition_and_outer_field(self):
        resolver = CountingResolver()
        context = engine.Context(resolver=resolver)
        rule = engine.IncrementalRule('[x for x in items if x > threshold]', context=context)
        thing = {'items': (1, 5, 10), 'threshold': 4}
        self.assertEqual(rule.evaluate(thing), (5, 10))
        # 无关变化：整个推导式命中缓存
        resolver.reset()
        self.assertEqual(rule.evaluate(thing), (5, 10))
        self.assertEqual(resolver.total, 0)
        # 阈值变化：推导式整体重算
        thing['threshold'] = 6
        rule.submit_changes({'threshold'})
        self.assertEqual(rule.evaluate(thing), (10,))

    def test_nested_comprehension(self):
        rule = engine.IncrementalRule('[[y * 2 for y in row] for row in matrix]')
        thing = {'matrix': ((1, 2), (3, 4))}
        self.assertEqual(rule.evaluate(thing), ((2, 4), (6, 8)))
        self.assertEqual(rule.evaluate(thing), ((2, 4), (6, 8)))
        thing['matrix'] = ((1, 2), (3, 4, 5))
        rule.submit_changes({'matrix'})
        self.assertEqual(rule.evaluate(thing), ((2, 4), (6, 8, 10)))

    def test_comprehension_over_function_results(self):
        rule = engine.IncrementalRule('[x + "!" for x in $split(text, ",")]')
        thing = {'text': '1,2,3'}
        self.assertEqual(rule.evaluate(thing), ('1!', '2!', '3!'))
        thing['text'] = '1,2'
        rule.submit_changes({'text'})
        self.assertEqual(rule.evaluate(thing), ('1!', '2!'))

    def test_dynamic_item_access(self):
        rule = engine.IncrementalRule('items[i]')
        thing = {'items': ('a', 'b', 'c'), 'i': 0}
        self.assertEqual(rule.evaluate(thing), 'a')
        thing['i'] = 2
        rule.submit_changes({'i'})
        self.assertEqual(rule.evaluate(thing), 'c')
        # 精确下标变更集：只影响读取该下标的节点
        thing['items'] = ('a', 'b', 'z')
        rule.submit_changes({('items', 2)})
        self.assertEqual(rule.evaluate(thing), 'z')
        thing['items'] = ('a', 'y', 'z')
        rule.submit_changes({('items', 1)})
        self.assertEqual(rule.evaluate(thing), 'z')

    def test_nested_path_changeset_precision(self):
        resolver = CountingResolver()
        context = engine.Context(resolver=resolver)
        rule = engine.IncrementalRule("user['age'] >= 18 and user['name'] != ''", context=context)
        thing = {'user': {'age': 20, 'name': 'x'}}
        self.assertIs(rule.evaluate(thing), True)
        thing['user'] = {'age': 17, 'name': 'x'}
        rule.submit_changes({('user', 'age')})
        resolver.reset()
        self.assertIs(rule.evaluate(thing), False)
        # 兄弟字段 name 所在分支未被重算
        self.assertEqual(resolver.counts['name'], 0)

    def test_attribute_path_on_object(self):
        context = _order_context()
        rule = engine.IncrementalRule('user.age >= 18 and user.name != ""', context=context)
        thing = _Order(user=_User(age=20, name='x'))
        self.assertIs(rule.evaluate(thing), True)
        thing.user.age = 17
        rule.submit_changes({('user', 'age')})
        self.assertIs(rule.evaluate(thing), False)
        thing.user.age = 19
        thing.user.name = ''
        rule.submit_changes({('user', 'name')})
        self.assertIs(rule.evaluate(thing), False)

    def test_whole_object_read_invalidation(self):
        rule = engine.IncrementalRule('user == other')
        thing = {'user': {'age': 20}, 'other': {'age': 20}}
        self.assertIs(rule.evaluate(thing), True)
        # 深层变化同样使整体读取失效
        thing['user'] = {'age': 21}
        rule.submit_changes({('user', 'age')})
        self.assertIs(rule.evaluate(thing), False)

    def test_exception_results(self):
        resolver = CountingResolver()
        context = engine.Context(resolver=resolver)
        rule = engine.IncrementalRule('missing + 1', context=context)
        thing = {}
        with self.assertRaises(errors.SymbolResolutionError):
            rule.evaluate(thing)
        # 异常结果被缓存：无关变更前不再重新解析
        resolver.reset()
        with self.assertRaises(errors.SymbolResolutionError):
            rule.evaluate(thing)
        self.assertEqual(resolver.total, 0)
        # 字段出现后正确恢复
        thing['missing'] = 41
        rule.submit_changes({'missing'})
        self.assertEqual(rule.evaluate(thing), 42)

    def test_exception_getitem_lookup(self):
        rule = engine.IncrementalRule('items[5]')
        thing = {'items': (1,)}
        with self.assertRaises(errors.LookupError):
            rule.evaluate(thing)
        with self.assertRaises(errors.LookupError):
            rule.evaluate(thing)
        thing['items'] = (1, 2, 3, 4, 5, 6)
        rule.submit_changes({'items'})
        self.assertEqual(rule.evaluate(thing), 6)

    def test_exception_arithmetic(self):
        rule = engine.IncrementalRule('a / b')
        thing = {'a': 1, 'b': 0}
        with self.assertRaises(errors.ArithmeticError):
            rule.evaluate(thing)
        thing['b'] = 2
        rule.submit_changes({'b'})
        self.assertEqual(rule.evaluate(thing), 0.5)

    def test_volatile_builtins_not_cached(self):
        rule = engine.IncrementalRule('$random() >= 0')
        self.assertIs(rule.evaluate({}), True)
        # 易变子树不写入任何缓存条目
        self.assertEqual(rule.cache_info()['entries'], 0)
        rule = engine.IncrementalRule('$now')
        first = rule.evaluate({})
        second = rule.evaluate({})
        self.assertLessEqual(first, second)

    def test_pure_builtin_cached(self):
        rule = engine.IncrementalRule('$abs(delta) > 1')
        thing = {'delta': -2}
        self.assertIs(rule.evaluate(thing), True)
        self.assertGreater(rule.cache_info()['entries'], 0)
        thing['delta'] = 0
        rule.submit_changes({'delta'})
        self.assertIs(rule.evaluate(thing), False)

    def test_regex_groups(self):
        rule = engine.IncrementalRule('name =~ "a(.*)" and $re_groups[0] == "bc"')
        thing = {'name': 'abc', 'unrelated': 1}
        self.assertIs(rule.evaluate(thing), True)
        # 无关字段变化：正则匹配重放以设置线程局部分组，结果保持正确
        thing['unrelated'] = 2
        rule.submit_changes({'unrelated'})
        self.assertIs(rule.evaluate(thing), True)
        thing['name'] = 'ax'
        rule.submit_changes({'name'})
        self.assertIs(rule.evaluate(thing), False)

    def test_regex_without_groups_cached(self):
        rule = engine.IncrementalRule('name =~ "a.*"')
        thing = {'name': 'abc'}
        self.assertIs(rule.evaluate(thing), True)
        self.assertGreater(rule.cache_info()['entries'], 0)
        thing['name'] = 'xbc'
        rule.submit_changes({'name'})
        self.assertIs(rule.evaluate(thing), False)

    def test_safe_navigation(self):
        rule = engine.IncrementalRule("a&['key']")
        thing = {'a': None}
        self.assertIsNone(rule.evaluate(thing))
        self.assertIsNone(rule.evaluate(thing))
        thing['a'] = {'key': 5}
        rule.submit_changes({'a'})
        self.assertEqual(rule.evaluate(thing), 5)

    def test_safe_navigation_attribute(self):
        context = _order_context(_MaybeOrder)
        rule = engine.IncrementalRule('user&.age', context=context)
        thing = _MaybeOrder(user=None)
        self.assertIsNone(rule.evaluate(thing))
        thing.user = _User(age=3, name='x')
        rule.submit_changes({'user'})
        self.assertEqual(rule.evaluate(thing), 3)

    def test_slice(self):
        rule = engine.IncrementalRule('items[1:3]')
        thing = {'items': (1, 2, 3, 4)}
        self.assertEqual(rule.evaluate(thing), (2, 3))
        thing['items'] = (1, 2, 3, 99)
        rule.submit_changes({('items', 3)})
        self.assertEqual(rule.evaluate(thing), (2, 3))
        thing['items'] = (1, 8, 3, 99)
        rule.submit_changes({('items', 1)})
        self.assertEqual(rule.evaluate(thing), (8, 3))

    def test_contains(self):
        rule = engine.IncrementalRule('x in items')
        thing = {'x': 2, 'items': (1, 2, 3)}
        self.assertIs(rule.evaluate(thing), True)
        thing['items'] = (1, 3)
        rule.submit_changes({'items'})
        self.assertIs(rule.evaluate(thing), False)

    def test_collection_literals(self):
        rule = engine.IncrementalRule("{'k': a, 'j': b}['k'] + [a, b][1]")
        thing = {'a': 1, 'b': 2}
        self.assertEqual(rule.evaluate(thing), 3)
        thing['a'] = 10
        rule.submit_changes({'a'})
        self.assertEqual(rule.evaluate(thing), 12)

    def test_scalar_method_purity(self):
        rule = engine.IncrementalRule('name.as_upper == expected')
        thing = {'name': 'abc', 'expected': 'ABC'}
        self.assertIs(rule.evaluate(thing), True)
        self.assertGreater(rule.cache_info()['entries'], 0)
        thing['expected'] = 'ABD'
        rule.submit_changes({'expected'})
        self.assertIs(rule.evaluate(thing), False)

    def test_default_value_missing_field(self):
        context = engine.Context(default_value=None)
        rule = engine.IncrementalRule('maybe ?? 5', context=context)
        thing = {}
        self.assertEqual(rule.evaluate(thing), 5)
        thing['maybe'] = 7
        rule.submit_changes({'maybe'})
        self.assertEqual(rule.evaluate(thing), 7)

    def test_thing_switch_full_invalidation(self):
        rule = engine.IncrementalRule('a + 1')
        self.assertEqual(rule.evaluate({'a': 1}), 2)
        # 输入对象整体替换（未提交变更集）：按全量失效处理
        self.assertEqual(rule.evaluate({'a': 100}), 101)
        self.assertEqual(rule.evaluate({'a': 1}), 2)

    def test_matches_and_filter(self):
        rule = engine.IncrementalRule('age >= 18')
        things = [{'age': 20}, {'age': 10}, {'age': 30}]
        self.assertEqual(list(rule.filter(things)), [things[0], things[2]])
        self.assertIs(rule.matches({'age': 1}), False)

    def test_concurrent_same_version(self):
        resolver = CountingResolver()
        context = engine.Context(resolver=resolver)
        rule = engine.IncrementalRule('price * qty + $abs(discount)', context=context)
        thing = {'price': 2, 'qty': 3, 'discount': -1}
        n_threads = 8
        barrier = threading.Barrier(n_threads)
        results = []
        failures = []
        def worker():
            try:
                barrier.wait(timeout=10)
                for _ in range(50):
                    results.append(rule.evaluate(thing))
            except Exception as error:  # noqa: BLE001
                failures.append(error)
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(results), n_threads * 50)
        self.assertTrue(all(value == 7 for value in results))
        # 缓存被共享：总解析次数远小于求值次数
        self.assertLess(resolver.total, len(results))

    def test_no_cross_talk_between_instances(self):
        thing = {'a': 1}
        rule1 = engine.IncrementalRule('a + 1')
        rule2 = engine.IncrementalRule('a + 1')
        self.assertEqual(rule1.evaluate(thing), 2)
        self.assertGreater(rule1.cache_info()['entries'], 0)
        self.assertEqual(rule2.cache_info()['entries'], 0)
        self.assertEqual(rule2.evaluate(thing), 2)

    def test_no_cross_talk_between_contexts(self):
        context1 = engine.Context()
        context2 = engine.Context()
        rule1 = engine.IncrementalRule('x', context=context1)
        rule2 = engine.IncrementalRule('x', context=context2)
        self.assertEqual(rule1.evaluate({'x': 1}), 1)
        self.assertEqual(rule2.cache_info()['entries'], 0)
        self.assertEqual(rule2.evaluate({'x': 2}), 2)

    def test_version_and_reset(self):
        rule = engine.IncrementalRule('a + 1')
        thing = {'a': 1}
        self.assertEqual(rule.evaluate(thing), 2)
        version = rule.version
        self.assertEqual(rule.submit_changes({'a'}), version + 1)
        self.assertEqual(rule.version, version + 1)
        rule.reset()
        self.assertEqual(rule.cache_info()['entries'], 0)
        self.assertEqual(rule.evaluate(thing), 2)

    def test_submit_changes_forms(self):
        rule = engine.IncrementalRule('a + 1')
        thing = {'a': 1}
        rule.evaluate(thing)
        # 单个字段名
        rule.submit_changes('a')
        # 单条路径
        rule.submit_changes(('a',))
        # 混合可迭代对象
        rule.submit_changes(['a', ('b', 'c')])
        with self.assertRaises(ValueError):
            rule.submit_changes([()])

    def test_pickle_roundtrip(self):
        rule = engine.IncrementalRule('f(v) * 2', pure_functions={'f'})
        thing = {'f': abs, 'v': -3}
        self.assertEqual(rule.evaluate(thing), 6)
        restored = pickle.loads(pickle.dumps(rule))
        self.assertEqual(restored.evaluate(thing), 6)
        thing['v'] = -4
        restored.submit_changes({'v'})
        self.assertEqual(restored.evaluate(thing), 8)

    def test_dependency_report(self):
        rule = engine.IncrementalRule(
                "parse_price(price) * qty > limit and user['age'] >= 18",
                pure_functions={'parse_price'},
        )
        report = rule.dependency_report()
        self.assertEqual(report['fields'], ['limit', 'parse_price', 'price', 'qty', 'user'])
        self.assertIn(('user', 'age'), report['field_paths'])
        self.assertIn('parse_price', report['functions'])
        self.assertNotIn('parse_price', report['impure_functions'])
        self.assertFalse(report['uses_regex_groups'])
        self.assertTrue(any(node['volatile'] for node in report['nodes']) is False)

    def test_dependency_report_impure(self):
        rule = engine.IncrementalRule('f(v) + $random()')
        report = rule.dependency_report()
        self.assertIn('f', report['impure_functions'])
        self.assertIn('random', report['volatile_builtins'])
        self.assertTrue(any(node['volatile'] for node in report['nodes']))

    def test_result_parity(self):
        cases = [
                (
                        'price * qty - discount',
                        {'price': 2, 'qty': 3, 'discount': 1},
                        [
                                (lambda t: t.update(discount=2), {'discount'}),
                                (lambda t: t.update(price=5), {'price'}),
                                (lambda t: t.update(qty=10), {'qty'}),
                        ],
                ),
                (
                        'flag ? price : qty',
                        {'flag': True, 'price': 1, 'qty': 2},
                        [
                                (lambda t: t.update(flag=False), {'flag'}),
                                (lambda t: t.update(price=99), {'price'}),
                                (lambda t: t.update(flag=True), {'flag'}),
                        ],
                ),
                (
                        "user['age'] >= 18 and user['name'] != ''",
                        {'user': {'age': 20, 'name': 'x'}},
                        [
                                (lambda t: t.update(user={'age': 17, 'name': 'x'}), {('user', 'age')}),
                                (lambda t: t.update(user={'age': 19, 'name': ''}), {('user', 'name')}),
                                (lambda t: t.update(user={'age': 19, 'name': 'y'}), {'user'}),
                        ],
                ),
                (
                        'items[i] + items[0]',
                        {'items': (1, 2, 3), 'i': 1},
                        [
                                (lambda t: t.update(i=2), {'i'}),
                                (lambda t: t.update(items=(9, 2, 3)), {('items', 0)}),
                                (lambda t: t.update(items=(9, 2, 8)), {('items', 2)}),
                        ],
                ),
                (
                        '[x * factor for x in items if x > 0]',
                        {'items': (1, -2, 3), 'factor': 2},
                        [
                                (lambda t: t.update(factor=3), {'factor'}),
                                (lambda t: t.update(items=(4, -5, 6)), {'items'}),
                        ],
                ),
                (
                        'a / b',
                        {'a': 1, 'b': 0},
                        [
                                (lambda t: t.update(b=2), {'b'}),
                                (lambda t: t.update(b=0), {'b'}),
                                (lambda t: t.update(a=4, b=4), {'a', 'b'}),
                        ],
                ),
                (
                        'items[1:3][0] + tail',
                        {'items': (1, 2, 3, 4), 'tail': 0},
                        [
                                (lambda t: t.update(items=(9, 2, 3, 4)), {('items', 0)}),
                                (lambda t: t.update(items=(9, 8, 3, 4)), {('items', 1)}),
                                (lambda t: t.update(items=(9, 8, 7, 4)), {('items', 2)}),
                                (lambda t: t.update(items=(9, 8, 7, 6)), {('items', 3)}),
                                (lambda t: t.update(tail=5), {'tail'}),
                        ],
                ),
                (
                        'a[0][1] + a[1][0]',
                        {'a': ((1, 2), (3, 4))},
                        [
                                (lambda t: t.update(a=((1, 9), (3, 4))), {('a', 0, 1)}),
                                (lambda t: t.update(a=((1, 9), (8, 4))), {('a', 1, 0)}),
                                (lambda t: t.update(a=((1, 9), (8, 7))), {('a', 1, 1)}),
                        ],
                ),
                (
                        'a[i][1]',
                        {'a': ((1, 2), (3, 4)), 'i': 0},
                        [
                                (lambda t: t.update(i=1), {'i'}),
                                (lambda t: t.update(a=((1, 2), (3, 9))), {('a', 1, 1)}),
                                (lambda t: t.update(a=((1, 7), (3, 9))), {('a', 0, 1)}),
                        ],
                ),
                (
                        'a ?? b ?? 3',
                        {'a': None, 'b': None},
                        [
                                (lambda t: t.update(b=2), {'b'}),
                                (lambda t: t.update(a=1), {'a'}),
                        ],
                ),
                (
                        '$abs(delta) + base',
                        {'delta': -2, 'base': 10},
                        [
                                (lambda t: t.update(delta=5), {'delta'}),
                                (lambda t: t.update(base=1), {'base'}),
                        ],
                ),
                (
                        'x in items and flag',
                        {'x': 2, 'items': (1, 2), 'flag': True},
                        [
                                (lambda t: t.update(items=(1,)), {'items'}),
                                (lambda t: t.update(flag=False), {'flag'}),
                        ],
                ),
                (
                        'items[1:3][0] + tail',
                        {'items': (1, 2, 3, 4), 'tail': 10},
                        [
                                (lambda t: t.update(items=(1, 9, 3, 4)), {('items', 1)}),
                                (lambda t: t.update(tail=20), {'tail'}),
                        ],
                ),
        ]
        for text, thing, steps in cases:
            with self.subTest(rule=text):
                self.assert_rule_parity(text, thing, steps)

    def test_fuzzed_changeset_parity(self):
        """固定种子的随机变更模糊测试：增量结果必须与全量求值一致。"""
        rules = (
                'price * qty - discount',
                'price > 0 and qty > 0 or flag',
                'flag ? price * 2 : qty',
                "user['age'] >= 18 and user['name'] != ''",
                'items[i] + items[0]',
                '[x * 2 for x in items]',
                '[x for x in items if x > threshold]',
                'x in items',
                'a / b',
                'maybe ?? 5',
        )
        rng = random.Random(20261006)

        def random_thing():
            return {
                    'price': rng.randint(0, 10), 'qty': rng.randint(0, 5), 'discount': rng.randint(0, 3),
                    'flag': rng.choice((True, False)),
                    'a': rng.randint(0, 5), 'b': rng.randint(0, 3),
                    'user': {'age': rng.randint(10, 30), 'name': rng.choice(('x', 'y', ''))},
                    'items': tuple(rng.randint(0, 5) for _ in range(3)),
                    'i': rng.randint(0, 2), 'x': rng.randint(0, 5),
                    'threshold': rng.randint(0, 5),
                    'maybe': rng.choice((None, 1, 2)),
            }

        for text in rules:
            incremental = engine.IncrementalRule(text)
            reference = engine.Rule(text)
            thing = random_thing()
            for _step in range(30):
                try:
                    expected = reference.evaluate(thing)
                except Exception as error:
                    expected = type(error)
                try:
                    actual = incremental.evaluate(thing)
                except Exception as error:
                    actual = type(error)
                if isinstance(expected, type):
                    self.assertIs(actual, expected, 'rule: ' + text)
                else:
                    self.assertEqual(actual, expected, 'rule: ' + text)
                    self.assertIs(type(actual), type(expected), 'rule: ' + text)
                field = rng.choice(('price', 'qty', 'flag', 'b', 'user', 'items', 'i', 'maybe'))
                if field == 'user':
                    thing['user'] = dict(thing['user'], age=rng.randint(10, 30))
                    incremental.submit_changes({('user', 'age')})
                elif field == 'items':
                    items = list(thing['items'])
                    index = rng.randint(0, 2)
                    items[index] = rng.randint(0, 5)
                    thing['items'] = tuple(items)
                    incremental.submit_changes({('items', index)})
                elif field == 'flag':
                    thing['flag'] = not thing['flag']
                    incremental.submit_changes({'flag'})
                elif field == 'maybe':
                    thing['maybe'] = rng.choice((None, 1, 2, 3))
                    incremental.submit_changes({'maybe'})
                else:
                    thing[field] = rng.randint(0, 10)
                    incremental.submit_changes({field})

    def test_result_parity_with_exceptions(self):
        cases = [
                (
                        'missing',
                        {},
                        [(lambda t: t.update(missing=5), {'missing'})],
                ),
                (
                        'items[3]',
                        {'items': (1,)},
                        [(lambda t: t.update(items=(1, 2, 3, 4)), {'items'})],
                ),
                (
                        "user['age'] + 1",
                        {'user': {}},
                        [(lambda t: t.update(user={'age': 1}), {('user', 'age')})],
                ),
        ]
        for text, thing, steps in cases:
            with self.subTest(rule=text):
                self.assert_rule_parity(text, thing, steps)
