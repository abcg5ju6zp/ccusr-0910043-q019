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

import threading
import time
import unittest

import rule_engine.engine as engine
import rule_engine.errors as errors
import rule_engine.types as types

__all__ = ('IncrementalEvaluationTests',)


def _function_resolver(mapping):
    """根解析器：先从映射中取函数值，否则按 MAPPING 字段解析。"""
    def resolve(thing, name):
        if name in mapping:
            return mapping[name]
        return thing[name]
    return resolve


class IncrementalEvaluationTests(unittest.TestCase):
    def test_compiled_dependencies_list_fields(self):
        rule = engine.Rule('price > 10 and tier == "gold"')
        self.assertEqual(rule.dependencies.fields, frozenset({'price', 'tier'}))
        self.assertFalse(rule.dependencies.non_cacheable)

    def test_unchanged_field_reuses_subtree(self):
        rule = engine.Rule('price > 10 and tier == "gold"')
        session = rule.incremental_session()
        thing = {'price': 20, 'tier': 'gold'}
        self.assertTrue(session.evaluate(thing, None))
        recomputed_after_first = session.stats['recomputed']
        # tier 变化：price 子树与变更集无关，应直接晋升其根节点并跳过子树
        self.assertTrue(session.evaluate(thing, ('tier',)))
        self.assertGreaterEqual(session.stats['promoted'], 1)
        # 左子树（price 比较）的整棵子节点不应被重算
        self.assertEqual(session.evaluate(thing, ('tier',)), True)
        self.assertGreater(recomputed_after_first, 0)

    def test_related_change_recomputes(self):
        rule = engine.Rule('price * discount > 100')
        session = rule.incremental_session()
        thing = {'price': 50, 'discount': 3}
        self.assertTrue(session.evaluate(thing, None))
        thing['discount'] = 1
        self.assertFalse(session.evaluate(thing, ('discount',)))
        thing['price'] = 200
        self.assertTrue(session.evaluate(thing, ('price',)))

    def test_external_change_recomputes_everything(self):
        rule = engine.Rule('a + b')
        session = rule.incremental_session()
        thing = {'a': 1, 'b': 2}
        self.assertEqual(session.evaluate(thing, None), 3)
        promoted_before = session.stats['promoted']
        # 未声明变更集（空元组且无 external_change）时，无相交字段仍可晋升……
        self.assertEqual(session.evaluate(thing, ('unrelated',)), 3)
        self.assertGreater(session.stats['promoted'], promoted_before)
        # ……但 external_change 必须强制全量重算
        recomputed_before = session.stats['recomputed']
        self.assertEqual(session.evaluate(thing, None, external_change=True), 3)
        self.assertGreater(session.stats['recomputed'], recomputed_before)

    def test_short_circuit_flip_does_not_reuse_stale_branch(self):
        rule = engine.Rule('active and score >= 10')
        session = rule.incremental_session()
        thing = {'active': False, 'score': 5}
        # 首版：右分支被短路，从未求值
        self.assertFalse(session.evaluate(thing, None))
        # 条件翻转使右分支首次被求值（旧版本没有它的条目，必须补算而不是沿用任何东西）
        thing['active'] = True
        self.assertFalse(session.evaluate(thing, ('active',)))
        thing['score'] = 20
        self.assertTrue(session.evaluate(thing, ('score',)))
        # 再翻回 false：右分支不执行，即使 score 子树有旧缓存也不影响结果
        thing['active'] = False
        self.assertFalse(session.evaluate(thing, ('active',)))

    def test_or_short_circuit_flip(self):
        rule = engine.Rule('vip or expensive_check()')
        calls = {'n': 0}
        def expensive_check():
            calls['n'] += 1
            return False
        context = engine.Context(resolver=_function_resolver({'expensive_check': expensive_check}))
        # 未登记纯度的自定义函数按有副作用处理
        rule = engine.Rule('vip or expensive_check()', context=context)
        session = rule.incremental_session()
        thing = {'vip': True}
        self.assertTrue(session.evaluate(thing, None))
        self.assertEqual(calls['n'], 0)
        thing['vip'] = False
        self.assertFalse(session.evaluate(thing, ('vip',)))
        self.assertEqual(calls['n'], 1)
        # 有副作用的调用每次都执行
        self.assertFalse(session.evaluate(thing, ('vip',)))
        self.assertEqual(calls['n'], 2)

    def test_ternary_branch_flip(self):
        context = engine.Context(resolver=_function_resolver({'on_branch': lambda: 'on', 'off_branch': lambda: 'off'}))
        rule = engine.Rule('flag ? on_branch() : off_branch()', context=context)
        session = rule.incremental_session()
        thing = {'flag': True}
        self.assertEqual(session.evaluate(thing, None), 'on')
        thing['flag'] = False
        self.assertEqual(session.evaluate(thing, ('flag',)), 'off')
        thing['flag'] = True
        self.assertEqual(session.evaluate(thing, ('flag',)), 'on')

    def test_comprehension_body_is_not_shared_across_elements(self):
        calls = {'n': 0}
        def tax(amount):
            calls['n'] += 1
            return amount
        context = engine.Context(resolver=_function_resolver({'tax': tax}))
        context.declare_function_purity('tax', 'pure')
        rule = engine.Rule('[tax(m) for m in amounts if m > 1]', context=context)
        session = rule.incremental_session()
        thing = {'amounts': (1, 2, 3, 4)}
        result = session.evaluate(thing, None)
        self.assertEqual(result, (2, 3, 4))
        # 三个元素通过条件，纯函数必须按元素各执行一次（不能因 AST 节点相同而只算一次）
        self.assertEqual(calls['n'], 3)
        # 迭代输入未变：整个推导结果晋升，函数一次都不再执行
        session.evaluate(thing, ('unrelated',))
        self.assertEqual(calls['n'], 3)
        # 输入变化后重新推导
        thing['amounts'] = (5, 6)
        self.assertEqual(session.evaluate(thing, ('amounts',)), (5, 6))
        self.assertEqual(calls['n'], 5)

    def test_dynamic_attribute_always_recomputed(self):
        calls = {'n': 0}
        class Account(object):
            @property
            def score(self):
                calls['n'] += 1
                return 42
        account = Account()
        dynamic_type = types.DataType.OBJECT(
                'Account',
                attributes={'score': types.DataType.FLOAT},
                accessor=lambda obj, name: getattr(obj, name),
                accessor_dynamic=True
        )
        context = engine.Context(type_resolver={'account': dynamic_type, 'Account': dynamic_type})
        rule = engine.Rule('account.score > 0', context=context)
        session = rule.incremental_session()
        thing = {'account': account}
        self.assertTrue(session.evaluate(thing, None))
        self.assertEqual(calls['n'], 1)
        # 即使没有声明任何字段变化，动态属性也必须重新读取
        self.assertTrue(session.evaluate(thing, ()))
        self.assertEqual(calls['n'], 2)

    def test_static_attribute_is_reused(self):
        calls = {'n': 0}
        class Account(object):
            @property
            def score(self):
                calls['n'] += 1
                return 42
        account = Account()
        static_type = types.DataType.OBJECT(
                'Account',
                attributes={'score': types.DataType.FLOAT},
                accessor=lambda obj, name: getattr(obj, name)
        )
        context = engine.Context(type_resolver={'account': static_type, 'Account': static_type})
        rule = engine.Rule('account.score > 0', context=context)
        session = rule.incremental_session()
        thing = {'account': account}
        self.assertTrue(session.evaluate(thing, None))
        self.assertTrue(session.evaluate(thing, ('unrelated',)))
        # 静态声明的访问器在无关变更时不重新执行
        self.assertEqual(calls['n'], 1)

    def test_impure_custom_function_always_runs(self):
        calls = {'n': 0}
        def side_effect(value):
            calls['n'] += 1
            return value
        context = engine.Context(resolver=_function_resolver({'side_effect': side_effect}))
        rule = engine.Rule('side_effect(price) > 5', context=context)
        session = rule.incremental_session()
        thing = {'price': 10}
        self.assertTrue(session.evaluate(thing, None))
        self.assertEqual(calls['n'], 1)
        self.assertTrue(session.evaluate(thing, ('unrelated',)))
        self.assertEqual(calls['n'], 2)

    def test_pure_custom_function_is_reused_and_invalidated(self):
        calls = {'n': 0}
        def double(value):
            calls['n'] += 1
            return value * 2
        context = engine.Context(resolver=_function_resolver({'double': double}))
        context.declare_function_purity('double', engine.PURE)
        rule = engine.Rule('double(price) > 10', context=context)
        session = rule.incremental_session()
        thing = {'price': 20}
        self.assertTrue(session.evaluate(thing, None))
        self.assertEqual(calls['n'], 1)
        # 无关变化：整棵子树晋升
        self.assertTrue(session.evaluate(thing, ('unrelated',)))
        self.assertEqual(calls['n'], 1)
        # 函数热更新：通过 changed_functions 精确失效，即使字段未变也重新执行一次
        self.assertTrue(session.evaluate(thing, (), changed_functions=('root:double',)))
        self.assertEqual(calls['n'], 2)
        # 参数变化才会再次调用
        thing['price'] = 3
        self.assertFalse(session.evaluate(thing, ('price',)))
        self.assertEqual(calls['n'], 3)

    def test_invalid_purity_rejected(self):
        context = engine.Context()
        with self.assertRaises(ValueError):
            context.declare_function_purity('double', 'sometimes')

    def test_higher_order_builtin_propagates_argument_impurity(self):
        # 无类型规则下参数 f 的类型是 UNDEFINED；map/filter 仍必须把它的副作用纯度并入调用节点
        calls = {'n': 0}
        def bump(value):
            calls['n'] += 1
            return value
        context = engine.Context(resolver=_function_resolver({'bump': bump}))
        rule = engine.Rule('$map(bump, items) == items', context=context)
        session = rule.incremental_session()
        thing = {'items': (1, 2, 3)}
        self.assertTrue(session.evaluate(thing, None))
        self.assertEqual(calls['n'], 3)
        self.assertTrue(session.evaluate(thing, ('unrelated',)))
        # bump 未登记纯度：map 调用节点每次都必须重新执行
        self.assertEqual(calls['n'], 6)

    def test_higher_order_builtin_with_pure_argument_is_reused(self):
        calls = {'n': 0}
        def bump(value):
            calls['n'] += 1
            return value
        context = engine.Context(resolver=_function_resolver({'bump': bump}))
        context.declare_function_purity('bump', engine.PURE)
        rule = engine.Rule('$map(bump, items) == items', context=context)
        session = rule.incremental_session()
        thing = {'items': (1, 2, 3)}
        self.assertTrue(session.evaluate(thing, None))
        self.assertEqual(calls['n'], 3)
        self.assertTrue(session.evaluate(thing, ('unrelated',)))
        self.assertEqual(calls['n'], 3)
        thing['items'] = (4, 5)
        self.assertTrue(session.evaluate(thing, ('items',)))
        self.assertEqual(calls['n'], 5)

    def test_exception_result_is_cached_and_reraised(self):
        calls = {'n': 0}
        def reciprocal(value):
            calls['n'] += 1
            if value == 0:
                raise ZeroDivisionError('zero')
            return 1
        context = engine.Context(resolver=_function_resolver({'reciprocal': reciprocal}))
        context.declare_function_purity('reciprocal', engine.PURE)
        rule = engine.Rule('reciprocal(price) == 1', context=context)
        session = rule.incremental_session()
        thing = {'price': 0}
        with self.assertRaises(errors.FunctionCallError):
            session.evaluate(thing, None)
        self.assertEqual(calls['n'], 1)
        # 同一输入版本：异常结果也要缓存，函数不重复执行，但异常一致地重抛
        with self.assertRaises(errors.FunctionCallError):
            session.evaluate(thing, ('unrelated',))
        self.assertEqual(calls['n'], 1)
        # 字段修复后恢复正常
        thing['price'] = 1
        self.assertTrue(session.evaluate(thing, ('price',)))
        self.assertEqual(calls['n'], 2)

    def test_arithmetic_exception_invalidation(self):
        rule = engine.Rule('10 / divisor == 5')
        session = rule.incremental_session()
        thing = {'divisor': 0}
        with self.assertRaises(errors.ArithmeticError):
            session.evaluate(thing, None)
        thing['divisor'] = 2
        self.assertTrue(session.evaluate(thing, ('divisor',)))

    def test_volatile_builtins_are_not_reused(self):
        rule = engine.Rule('$random(1000000) >= 0')
        session = rule.incremental_session()
        thing = {}
        self.assertTrue(session.evaluate(thing, None))
        recomputed_before = session.stats['recomputed']
        bypassed_before = session.stats['bypassed']
        self.assertTrue(session.evaluate(thing, ()))
        # 波动节点每次都真实执行
        self.assertGreater(session.stats['bypassed'], bypassed_before)
        self.assertEqual(session.stats['recomputed'], recomputed_before)

    def test_regex_match_side_effect_replays(self):
        # =~ 会写入 $re_groups；即使左值未变，匹配节点也必须重放以更新线程本地状态
        rule = engine.Rule('words =~ "(\\w+) (\\w+)" and $re_groups[0] != ""')
        session = rule.incremental_session()
        thing = {'words': 'MainThread Test'}
        self.assertTrue(session.evaluate(thing, None))
        self.assertTrue(session.evaluate(thing, ('unrelated',)))

    def test_concurrent_evaluation_shares_same_version(self):
        calls = {'n': 0}
        started = threading.Event()
        proceed = threading.Event()
        def slow_double(value):
            calls['n'] += 1
            started.set()
            self.assertTrue(proceed.wait(5))
            return value * 2
        context = engine.Context(resolver=_function_resolver({'slow_double': slow_double}))
        context.declare_function_purity('slow_double', engine.PURE)
        rule = engine.Rule('slow_double(price) == 40', context=context)
        session = rule.incremental_session()
        thing = {'price': 20}
        revision = session.revise(None)
        results = []
        errors_seen = []
        def worker():
            try:
                with revision:
                    results.append(revision.evaluate(thing))
            except BaseException as error:
                errors_seen.append(error)
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        # 等先到的线程进入函数体，再留时间让第二个线程阻塞在节点事件上，最后放行
        self.assertTrue(started.wait(5))
        time.sleep(0.2)
        proceed.set()
        t1.join(5)
        t2.join(5)
        self.assertEqual(errors_seen, [])
        self.assertEqual(results, [True, True])
        # 同一版本并发：函数只执行一次，另一个线程共享只读结果
        self.assertEqual(calls['n'], 1)
        self.assertGreaterEqual(session.stats['reused'], 1)

    def test_concurrent_exception_is_shared(self):
        calls = {'n': 0}
        started = threading.Event()
        proceed = threading.Event()
        def boom(value):
            calls['n'] += 1
            started.set()
            self.assertTrue(proceed.wait(5))
            raise RuntimeError('boom')
        context = engine.Context(resolver=_function_resolver({'boom': boom}))
        context.declare_function_purity('boom', engine.PURE)
        rule = engine.Rule('boom(price) == 1', context=context)
        session = rule.incremental_session()
        thing = {'price': 1}
        revision = session.revise(None)
        seen = []
        def worker():
            with revision:
                try:
                    revision.evaluate(thing)
                except errors.FunctionCallError:
                    seen.append('error')
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        self.assertTrue(started.wait(5))
        time.sleep(0.2)
        proceed.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(seen, ['error', 'error'])
        self.assertEqual(calls['n'], 1)

    def test_sessions_do_not_share_across_rule_instances(self):
        rule_a = engine.Rule('price > 10')
        rule_b = engine.Rule('price > 10')
        session_a = rule_a.incremental_session()
        session_b = rule_b.incremental_session()
        thing = {'price': 20}
        self.assertTrue(session_a.evaluate(thing, None))
        self.assertTrue(session_b.evaluate(thing, None))
        # 两个会话各自独立计数、独立缓存
        self.assertEqual(session_a.stats['reused'], 0)
        self.assertEqual(session_b.stats['reused'], 0)
        thing['price'] = 5
        self.assertFalse(session_a.evaluate(thing, ('price',)))
        # 会话 B 的旧结果不允许被会话 A 的变更影响之外的途径看到：它仍需按自己的版本重算
        self.assertFalse(session_b.evaluate(thing, ('price',)))

    def test_sessions_do_not_share_across_contexts(self):
        context_a = engine.Context(type_resolver={'price': types.DataType.FLOAT})
        context_b = engine.Context(type_resolver={'price': types.DataType.FLOAT})
        rule_a = engine.Rule('price > 10', context=context_a)
        rule_b = engine.Rule('price > 10', context=context_b)
        session_a = rule_a.incremental_session()
        session_b = rule_b.incremental_session()
        thing = {'price': 20}
        self.assertTrue(session_a.evaluate(thing, None))
        self.assertTrue(session_b.evaluate(thing, None))
        self.assertIsNot(session_a._store, session_b._store)

    def test_returned_set_mutation_does_not_poison_cache(self):
        rule = engine.Rule('{a, b}')
        session = rule.incremental_session()
        thing = {'a': 1, 'b': 2}
        first = session.evaluate(thing, None)
        first.add(99)
        second = session.evaluate(thing, ('unrelated',))
        self.assertEqual(second, {1, 2})
        self.assertIsNot(first, second)

    def test_returned_mapping_mutation_does_not_poison_cache(self):
        rule = engine.Rule('{"sum": a + b}')
        session = rule.incremental_session()
        thing = {'a': 1, 'b': 2}
        first = session.evaluate(thing, None)
        first['sum'] = 999
        second = session.evaluate(thing, ('unrelated',))
        self.assertEqual(dict(second), {'sum': 3})

    def test_resolver_change_invalidates(self):
        calls = {'n': 0}
        def counting_accessor(obj, name):
            if name == 'score':
                calls['n'] += 1
                return 42
            return getattr(obj, name)
        account_type = types.DataType.OBJECT(
                'Account',
                attributes={'score': types.DataType.FLOAT},
                accessor=counting_accessor
        )
        context = engine.Context(type_resolver={'account': account_type, 'Account': account_type})
        rule = engine.Rule('account.score', context=context)
        session = rule.incremental_session()
        thing = {'account': object()}
        self.assertEqual(session.evaluate(thing, None), 42)
        self.assertEqual(session.evaluate(thing, ('unrelated',)), 42)
        self.assertEqual(calls['n'], 1)
        # 访问器行为热更新时通过 changed_resolvers 精确失效，即使声明没有字段变化
        self.assertEqual(session.evaluate(thing, (), changed_resolvers=('object:Account',)), 42)
        self.assertEqual(calls['n'], 2)

    def test_revision_requires_activation(self):
        rule = engine.Rule('a + b')
        session = rule.incremental_session()
        revision = session.revise(None)
        with self.assertRaises(errors.EngineError):
            revision.evaluate({'a': 1, 'b': 2})

    def test_one_revision_rejects_different_input_objects(self):
        rule = engine.Rule('price > 10')
        session = rule.incremental_session()
        revision = session.revise(None)
        with revision:
            self.assertTrue(revision.evaluate({'price': 20}))
            with self.assertRaises(errors.EngineError):
                revision.evaluate({'price': 5})

    def test_different_input_objects_do_not_share_entries(self):
        rule = engine.Rule('price > 10')
        session = rule.incremental_session()
        first = {'price': 20}
        second = {'price': 5}
        self.assertTrue(session.evaluate(first, None))
        # 新输入对象（不同身份）：旧条目全部作废，必须按新对象的数据重新计算
        self.assertFalse(session.evaluate(second, ('price',)))
        self.assertEqual(session.stats['reused'], 0)

    def test_plain_evaluation_path_unchanged(self):
        # 不经过会话时行为与原引擎完全一致（路由退化为直接求值）
        rule = engine.Rule('a + b')
        self.assertEqual(rule.evaluate({'a': 1, 'b': 2}), 3)
        self.assertEqual(rule.matches({'a': 1, 'b': 2}), True)


if __name__ == '__main__':
    unittest.main()
