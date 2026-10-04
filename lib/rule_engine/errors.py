#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/errors.py
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

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types.definitions import _DataTypeDef


class _UNDEFINED(object):
    def __bool__(self) -> bool:
        return False
    __name__ = 'UNDEFINED'
    __nonzero__ = __bool__
    def __repr__(self) -> str:
        return self.__name__
UNDEFINED = _UNDEFINED()
"""
A sentinel value to specify that something is undefined. When evaluated, the value is falsy.

.. versionadded:: 2.0.0
"""

class EngineError(Exception):
    """项目内部接口说明。"""
    def __init__(self, message: str = '') -> None:
        """项目内部接口说明。"""
        self.message = message
        """A text description of what error occurred."""

    def __repr__(self) -> str:
        return "<{} message={!r} >".format(self.__class__.__name__, self.message)

class MappingAttributeLookupDeprecation(DeprecationWarning):
    """项目内部接口说明。"""

class EvaluationError(EngineError):
    """项目内部接口说明。"""

class SyntaxError(EngineError):
    """项目内部接口说明。"""

class BytesSyntaxError(SyntaxError):
    """项目内部接口说明。"""
    def __init__(self, message: str, value: str) -> None:
        """项目内部接口说明。"""
        super(BytesSyntaxError, self).__init__(message)
        self.value = value
        """The bytes value which contains the syntax error which caused this exception to be raised."""

class StringSyntaxError(SyntaxError):
    """项目内部接口说明。"""
    def __init__(self, message: str, value: str) -> None:
        """项目内部接口说明。"""
        super(StringSyntaxError, self).__init__(message)
        self.value = value
        """The string value which contains the syntax error which caused this exception to be raised."""

class DatetimeSyntaxError(SyntaxError):
    """项目内部接口说明。"""
    def __init__(self, message: str, value: str) -> None:
        """项目内部接口说明。"""
        super(DatetimeSyntaxError, self).__init__(message)
        self.value = value
        """The datetime value which contains the syntax error which caused this exception to be raised."""

class FloatSyntaxError(SyntaxError):
    """项目内部接口说明。"""
    def __init__(self, message: str, value: str) -> None:
        """项目内部接口说明。"""
        super(FloatSyntaxError, self).__init__(message)
        self.value = value
        """The float value which contains the syntax error which caused this exception to be raised."""

class TimedeltaSyntaxError(SyntaxError):
    """项目内部接口说明。"""
    def __init__(self, message: str, value: str) -> None:
        """项目内部接口说明。"""
        super(TimedeltaSyntaxError, self).__init__(message)
        self.value = value
        """The timedelta value which contains the syntax error which caused this exception to be raised."""

class RegexSyntaxError(SyntaxError):
    """项目内部接口说明。"""
    def __init__(self, message: str, error: re.error, value: str) -> None:
        """项目内部接口说明。"""
        super(RegexSyntaxError, self).__init__(message)
        self.error = error
        """The :py:exc:`re.error` exception from which this error was triggered."""
        self.value = value
        """The regular expression value which contains the syntax error which caused this exception to be raised."""

class RuleSyntaxError(SyntaxError):
    """项目内部接口说明。"""
    def __init__(self, message: str, token: Any = None) -> None:
        """项目内部接口说明。"""
        if token is None:
            position = 'EOF'
        else:
            position = "line {0}:{1}".format(token.lineno, token.lexpos)
        message = message + ' at: ' + position
        super(RuleSyntaxError, self).__init__(message)
        self.token = token
        """The PLY token (if available) which is related to the syntax error."""

class AttributeResolutionError(EvaluationError):
    """项目内部接口说明。"""
    def __init__(self, attribute_name: str, object_: Any, thing: Any = UNDEFINED, suggestion: str | None = None) -> None:
        """项目内部接口说明。"""
        self.attribute_name = attribute_name
        """The name of the symbol that can not be resolved."""
        self.object = object_
        """The value that *attribute_name* was used as an attribute for."""
        self.thing = thing
        """The root-object that was used to resolve *object*."""
        self.suggestion = suggestion
        """An optional suggestion for a valid attribute name."""
        super(AttributeResolutionError, self).__init__("unknown attribute: {0!r}".format(attribute_name))

    def __repr__(self) -> str:
        return "<{} message={!r} suggestion={!r} >".format(self.__class__.__name__, self.message, self.suggestion)

class ObjectAttributeError(AttributeResolutionError):
    """项目内部接口说明。"""

class AttributeTypeError(EvaluationError):
    """项目内部接口说明。"""
    def __init__(
            self,
            attribute_name: str,
            object_type: _DataTypeDef,
            is_value: Any,
            is_type: _DataTypeDef,
            expected_type: _DataTypeDef
    ) -> None:
        """项目内部接口说明。"""
        self.attribute_name = attribute_name
        """The name of the attribute that is of an incompatible type."""
        self.object_type = object_type
        """The object on which the attribute was resolved."""
        self.is_value = is_value
        """The native Python value of the incompatible attribute."""
        self.is_type = is_type
        """The :py:class:`rule-engine type<rule_engine.types.DataType>` of the incompatible attribute."""
        self.expected_type = expected_type
        """The :py:class:`rule-engine type<rule_engine.types.DataType>` that was expected for this attribute."""
        message = "attribute {0!r} resolved to incorrect datatype (is: {1}, expected: {2})".format(
                attribute_name,
                is_type.name,
                expected_type.name
        )
        super(AttributeTypeError, self).__init__(message)

class LookupError(EvaluationError):
    """项目内部接口说明。"""
    def __init__(self, container: Any, item: Any) -> None:
        """项目内部接口说明。"""
        self.container = container
        """The container object that the lookup was performed on."""
        self.item = item
        """The item that was used as either the key or index of *container* for the lookup."""
        super(LookupError, self).__init__('lookup operation failed')

class SymbolResolutionError(EvaluationError):
    """项目内部接口说明。"""
    def __init__(
            self,
            symbol_name: str,
            symbol_scope: str | None = None,
            thing: Any = UNDEFINED,
            suggestion: str | None = None
    ) -> None:
        """项目内部接口说明。"""
        self.symbol_name = symbol_name
        """The name of the symbol that can not be resolved."""
        self.symbol_scope = symbol_scope
        """The scope of where the symbol should be valid for resolution."""
        self.thing = thing
        """The root-object that was used to resolve the symbol."""
        self.suggestion = suggestion
        """An optional suggestion for a valid symbol name."""
        super(SymbolResolutionError, self).__init__("unknown symbol: {0!r}".format(symbol_name))

    def __repr__(self) -> str:
        return "<{} message={!r} suggestion={!r} >".format(self.__class__.__name__, self.message, self.suggestion)

class SymbolTypeError(EvaluationError):
    """项目内部接口说明。"""
    def __init__(self, symbol_name: str, is_value: Any, is_type: _DataTypeDef, expected_type: _DataTypeDef) -> None:
        """项目内部接口说明。"""
        self.symbol_name = symbol_name
        """The name of the symbol that is of an incompatible type."""
        self.is_value = is_value
        """The native Python value of the incompatible symbol."""
        self.is_type = is_type
        """The :py:class:`rule-engine type<rule_engine.types.DataType>` of the incompatible symbol."""
        self.expected_type = expected_type
        """The :py:class:`rule-engine type<rule_engine.types.DataType>` that was expected for this symbol."""
        message = "symbol {0!r} resolved to incorrect datatype (is: {1}, expected: {2})".format(
                symbol_name,
                is_type.name,
                expected_type.name
        )
        super(SymbolTypeError, self).__init__(message)

class FunctionCallError(EvaluationError):
    """项目内部接口说明。"""
    def __init__(self, message: str, error: BaseException | None = None, function_name: str | None = None) -> None:
        super(FunctionCallError, self).__init__(message)
        self.error = error
        """The exception from which this error was triggered."""
        self.function_name = function_name

class ArithmeticError(EvaluationError):
    """项目内部接口说明。"""
