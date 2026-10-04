#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/ast/base.py
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

import datetime
from typing import TYPE_CHECKING, Any, Iterable

from .. import errors
from ..types import *
from ..types import _CollectionDataTypeDef, _DataTypeDef, _ReferenceDataTypeDef

if TYPE_CHECKING:
    from ..engine.context import Context

def _assert_is_bytes(*values: Any) -> None:
    if not all(map(isinstance, values, [bytes])):
        raise errors.EvaluationError('data type mismatch (not a bytes value)')

def _assert_is_integer_number(*values: Any) -> None:
    if not all(map(is_integer_number, values)):
        raise errors.EvaluationError('data type mismatch (not an integer number)')

def _assert_is_natural_number(*values: Any) -> None:
    if not all(map(is_natural_number, values)):
        raise errors.EvaluationError('data type mismatch (not a natural number)')

def _assert_is_numeric(*values: Any) -> None:
    if not all(map(is_numeric, values)):
        raise errors.EvaluationError('data type mismatch (not a numeric value)')

def _assert_is_string(*values: Any) -> None:
    if not all(map(isinstance, values, [str])):
        raise errors.EvaluationError('data type mismatch (not a string value)')

def _is_reduced(*values: Any) -> bool:
    """项目内部接口说明。"""
    return all((isinstance(value, LiteralExpressionBase) and value.is_reduced) for value in values)

def _iterable_member_value_type(value: Iterable[Any]) -> _DataTypeDef:
    value = (
            member.result_type if isinstance(member, ExpressionBase) else member for member in value
    )
    return iterable_member_value_type(value)

def _propagate_nullable(result_type: _DataTypeDef, *operand_types: _DataTypeDef) -> _DataTypeDef:
    """项目内部接口说明。"""
    if any(DataType.is_type(t, DataType.NULLABLE) for t in operand_types):
        return DataType.NULLABLE.wrap(result_type)
    return result_type

def _assert_not_nullable(dt: _DataTypeDef, *, role: str) -> None:
    """项目内部接口说明。"""
    if DataType.is_type(dt, DataType.NULLABLE):
        raise errors.EvaluationError(
                "data type mismatch ({0} is nullable; discharge with '??' or use '&.' / '&[' for safe navigation)".format(role)
        )

def _resolve_type(definition: _DataTypeDef, context: 'Context') -> _DataTypeDef:
    """项目内部接口说明。"""
    if isinstance(definition, _ReferenceDataTypeDef):
        try:
            resolved = context.resolve_type(definition.name)
        except errors.SymbolResolutionError as error:
            raise errors.EvaluationError(
                    "unresolved object reference: {0!r} - add it to the Context type_resolver".format(definition.name)
            ) from error
        if not DataType.is_type(resolved, DataType.OBJECT):
            raise errors.EvaluationError(
                    "reference {0!r} does not resolve to an OBJECT type".format(definition.name)
            )
        if resolved.name != definition.name:
            raise errors.EvaluationError(
                    "reference {0!r} resolves to an OBJECT with mismatched name {1!r}".format(definition.name, resolved.name)
            )
        return resolved
    if DataType.is_type(definition, DataType.OBJECT):
        return definition
    if DataType.is_type(definition, DataType.NULLABLE):
        new_inner = _resolve_type(definition.inner_type, context)
        if new_inner is definition.inner_type:
            return definition
        return definition.__class__(
                definition.name,
                definition.python_type,
                inner_type=new_inner
        )
    if isinstance(definition, _CollectionDataTypeDef):
        new_value_type = _resolve_type(definition.value_type, context)
        if new_value_type is definition.value_type:
            return definition
        return definition.__class__(
                definition.name,
                definition.python_type,
                value_type=new_value_type
        )
    if DataType.is_type(definition, DataType.MAPPING):
        new_key_type = _resolve_type(definition.key_type, context)
        new_value_type = _resolve_type(definition.value_type, context)
        if new_key_type is definition.key_type and new_value_type is definition.value_type:
            return definition
        return definition.__class__(
                definition.name,
                definition.python_type,
                key_type=new_key_type,
                value_type=new_value_type
        )
    if DataType.is_type(definition, DataType.FUNCTION):
        new_return_type = _resolve_type(definition.return_type, context)
        new_argument_types: tuple[_DataTypeDef, ...] | _DataTypeDef
        if definition.argument_types is DataType.UNDEFINED:
            new_argument_types = definition.argument_types
        else:
            assert isinstance(definition.argument_types, tuple)
            new_argument_types = tuple(_resolve_type(arg, context) for arg in definition.argument_types)
        if new_return_type is definition.return_type and new_argument_types is definition.argument_types:
            return definition
        return definition.__class__(
                definition.name,
                definition.python_type,
                value_name=definition.value_name,
                return_type=new_return_type,
                argument_types=new_argument_types,
                minimum_arguments=definition.minimum_arguments
        )
    return definition

class Assignment(object):
    """项目内部接口说明。"""
    __slots__ = ('name', 'value', 'value_type')
    name: str
    value: Any
    value_type: _DataTypeDef | None
    def __init__(self, name: str, *, value: Any = errors.UNDEFINED, value_type: _DataTypeDef | None = None) -> None:
        """项目内部接口说明。"""
        self.name = name
        self.value = value
        if value is not errors.UNDEFINED and value_type is not None:
            value_type = DataType.from_value(value)
        self.value_type = value_type

    def __repr__(self) -> str:
        return "<{} name={!r} value={!r} value_type={!r} >".format(self.__class__.__name__, self.name, self.value, self.value_type)

class ASTNodeBase(object):
    def to_graphviz(self, digraph: Any) -> None:
        digraph.node(str(id(self)), self.__class__.__name__)

    @classmethod
    def build(cls, *args: Any, **kwargs: Any) -> 'ASTNodeBase':
        return cls(*args, **kwargs).reduce()

    def evaluate(self, thing: Any) -> Any:
        """项目内部接口说明。"""
        raise NotImplementedError()

    def reduce(self) -> 'ASTNodeBase':
        """项目内部接口说明。"""
        return self

class Comment(ASTNodeBase):
    __slots__ = ('value',)
    value: Any
    def __init__(self, value: Any) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "<{0} {1!r}>".format(self.__class__.__name__, self.value)

    def to_graphviz(self, digraph: Any, *args: Any, **kwargs: Any) -> None:
        digraph.node(str(id(self)), "{}\n{!r}".format(self.__class__.__name__, self.value))

################################################################################
# Base Expression Classes
################################################################################
class ExpressionBase(ASTNodeBase):
    __slots__ = ('context',)
    context: 'Context'
    result_type: _DataTypeDef = DataType.UNDEFINED
    """成功求值结果的数据类型。"""
    def __repr__(self) -> str:
        return "<{0} >".format(self.__class__.__name__)

    def _new_value(self, *args: Any, **kwargs: Any) -> Any:
        # perform a context aware load of value
        value = coerce_value(*args, **kwargs)
        if isinstance(value, datetime.datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=self.context.default_timezone)
        return value

class LiteralExpressionBase(ExpressionBase):
    """项目内部接口说明。"""
    __slots__ = ('value',)
    value: Any
    is_reduced: bool = True
    def __init__(self, context: 'Context', value: Any) -> None:
        """项目内部接口说明。"""
        self.context = context
        if self.result_type.is_scalar and DataType.from_value(value) != self.result_type:
            raise TypeError("__init__ argument 2 must be {}, not {}".format(self.result_type.python_type.__name__, type(value).__name__))
        self.value = value

    def __repr__(self) -> str:
        return "<{0} value={1!r} >".format(self.__class__.__name__, self.value)

    @classmethod
    def from_value(cls, context: 'Context', value: Any) -> 'LiteralExpressionBase':
        """项目内部接口说明。"""
        datatype = DataType.from_value(value)
        for subclass in cls.__subclasses__():
            if DataType.is_compatible(subclass.result_type, datatype):
                break
        else:
            raise errors.EngineError("can not create literal expression from python value: {!r}".format(value))
        if datatype.is_compound:
            if DataType.is_type(datatype, DataType.ARRAY) or DataType.is_type(datatype, DataType.SET):
                value = datatype.python_type(cls.from_value(context, v) for v in value)
            elif DataType.is_type(datatype, DataType.MAPPING):
                value = tuple((cls.from_value(context, k), cls.from_value(context, v)) for k, v in value.items())
        else:
            value = coerce_value(value)
        return subclass(context, value)

    def evaluate(self, thing: Any) -> Any:
        return self.value

    def to_graphviz(self, digraph: Any, *args: Any, **kwargs: Any) -> None:
        if self.result_type.is_compound:
            digraph.node(str(id(self)), self.__class__.__name__)
        else:
            digraph.node(str(id(self)), "{}\nvalue={!r}".format(self.__class__.__name__, self.value))

class Statement(ASTNodeBase):
    """项目内部接口说明。"""
    __slots__ = ('context', 'expression', 'comment')
    context: 'Context'
    expression: ExpressionBase
    comment: Comment | None
    def __init__(self, context: 'Context', expression: ExpressionBase, comment: Comment | None = None) -> None:
        """项目内部接口说明。"""
        self.context = context
        self.expression = expression
        self.comment = comment

    @classmethod
    def build(cls, context: 'Context', expression: ExpressionBase, **kwargs: Any) -> 'Statement':  # type: ignore[override]
        built = expression.build()
        assert isinstance(built, ExpressionBase)
        reduced = cls(context, built, **kwargs).reduce()
        assert isinstance(reduced, Statement)
        return reduced

    def evaluate(self, thing: Any) -> Any:
        return self.expression.evaluate(thing)

    def to_graphviz(self, digraph: Any, *args: Any, **kwargs: Any) -> None:
        super(Statement, self).to_graphviz(digraph, *args, **kwargs)
        self.expression.to_graphviz(digraph, *args, **kwargs)
        digraph.edge(str(id(self)), str(id(self.expression)))
        if self.comment:
            self.comment.to_graphviz(digraph, *args, **kwargs)
