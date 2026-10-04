#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/types/_object.py
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

import threading
from collections.abc import Mapping
from typing import Any, Callable, ClassVar

from .definitions import (
        _CollectionDataTypeDef,
        _DATA_TYPE_UNDEFINED,
        _DataTypeDef,
        _FunctionDataTypeDef,
        _MappingDataTypeDef,
        _NullableDataTypeDef,
        _ReferenceDataTypeDef,
        _SelfReferenceDataTypeDef,
)

_object_compare_tls = threading.local()

def _substitute_self_references(definition: _DataTypeDef, target: _ObjectDataTypeDef) -> _DataTypeDef:
    """项目内部接口说明。"""
    if isinstance(definition, _ReferenceDataTypeDef):
        if isinstance(definition, _SelfReferenceDataTypeDef) or definition.name == target.name:
            return target
        return definition
    if isinstance(definition, _ObjectDataTypeDef):
        return definition
    if isinstance(definition, _NullableDataTypeDef):
        new_inner = _substitute_self_references(definition.inner_type, target)
        if new_inner is definition.inner_type:
            return definition
        return definition.__class__(definition.name, definition.python_type, inner_type=new_inner)
    if isinstance(definition, _CollectionDataTypeDef):
        new_value_type = _substitute_self_references(definition.value_type, target)
        if new_value_type is definition.value_type:
            return definition
        return definition.__class__(
                definition.name,
                definition.python_type,
                value_type=new_value_type
        )
    if isinstance(definition, _MappingDataTypeDef):
        new_key_type = _substitute_self_references(definition.key_type, target)
        new_value_type = _substitute_self_references(definition.value_type, target)
        if new_key_type is definition.key_type and new_value_type is definition.value_type:
            return definition
        return definition.__class__(
                definition.name,
                definition.python_type,
                key_type=new_key_type,
                value_type=new_value_type
        )
    if isinstance(definition, _FunctionDataTypeDef):
        new_return_type = _substitute_self_references(definition.return_type, target)
        if definition.argument_types is _DATA_TYPE_UNDEFINED:
            new_argument_types: tuple[_DataTypeDef, ...] | _DataTypeDef = definition.argument_types
        else:
            assert isinstance(definition.argument_types, tuple)
            new_argument_types = tuple(_substitute_self_references(arg_type, target) for arg_type in definition.argument_types)
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

class _ObjectDataTypeDef(_DataTypeDef):
    """项目内部接口说明。"""
    __slots__ = ('attributes', 'accessor')
    attributes: dict[str, _DataTypeDef]
    accessor: Callable[[Any, str], Any]
    is_object: ClassVar[bool] = True
    # class attribute (not in __slots__) — set after class definition below; a sentinel used inside attribute
    # schemas to self-reference the enclosing OBJECT without repeating its name
    self: ClassVar[_SelfReferenceDataTypeDef]
    def __init__(
            self,
            name: str,
            python_type: type = object,
            attributes: Mapping[str, _DataTypeDef] | None = None,
            accessor: Callable[[Any, str], Any] | None = None
    ) -> None:
        super(_ObjectDataTypeDef, self).__init__(name, python_type)
        self.is_scalar = False
        self.attributes = dict(attributes) if attributes else {}
        self.accessor = accessor if accessor is not None else getattr
        # resolve self-references in the attribute schema now that self exists; cross-name references are left intact
        # and will be resolved lazily at rule parse time via Context.resolve_type
        for attr_name, attr_type in self.attributes.items():
            if not isinstance(attr_type, _DataTypeDef):
                raise TypeError("object {0} attribute {1!r} has an invalid type: {2!r}".format(self.name, attr_name, attr_type))
            self.attributes[attr_name] = _substitute_self_references(attr_type, self)

    def __call__(
            self,
            name: str,
            attributes: Mapping[str, _DataTypeDef] | None = None,
            accessor: Callable[[Any, str], Any] | None = None
    ) -> _ObjectDataTypeDef:
        """项目内部接口说明。"""
        return self.__class__(
                name,
                self.python_type,
                attributes=attributes,
                accessor=accessor
        )

    @staticmethod
    def reference(name: str) -> _ReferenceDataTypeDef:
        """项目内部接口说明。"""
        return _ReferenceDataTypeDef(name)

    @staticmethod
    def from_dataclass(
            name: str,
            cls: type,
            *,
            accessor: Callable[[Any, str], Any] | None = None,
            strict: bool = True
    ) -> _ObjectDataTypeDef:
        """项目内部接口说明。"""
        import dataclasses
        from .dataclass import _build_object_from_dataclass
        if not dataclasses.is_dataclass(cls):
            raise TypeError('from_dataclass argument 2 must be a dataclass, not ' + type(cls).__name__)
        return _build_object_from_dataclass(cls, name, accessor=accessor, _seen={}, strict=strict)

    @staticmethod
    def from_sqlalchemy(
            name: str,
            cls: type,
            *,
            accessor: Callable[[Any, str], Any] | None = None,
            strict: bool = True
    ) -> _ObjectDataTypeDef:
        """项目内部接口说明。"""
        from .sqlalchemy import _build_object_from_sqlalchemy
        if not hasattr(cls, '__mapper__'):
            raise TypeError('from_sqlalchemy argument 2 must be a SQLAlchemy mapped class, not ' + type(cls).__name__)
        return _build_object_from_sqlalchemy(cls, name, accessor=accessor, _seen={}, strict=strict)

    def __repr__(self) -> str:
        return "<{} name={} attributes=[{}] >".format(
                self.__class__.__name__,
                self.name,
                ', '.join(self.attributes.keys())
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _ObjectDataTypeDef):
            return False
        if self.name != other.name:
            return False
        if self.attributes.keys() != other.attributes.keys():
            return False
        stack = getattr(_object_compare_tls, 'stack', None)
        if stack is None:
            stack = set()
            _object_compare_tls.stack = stack
        key = (id(self), id(other))
        if key in stack:
            # break recursive comparisons by assuming equality (standard cycle-breaking fixpoint)
            return True
        stack.add(key)
        try:
            for attr_name in self.attributes:
                if self.attributes[attr_name] != other.attributes[attr_name]:
                    return False
            return True
        finally:
            stack.discard(key)

    def __hash__(self) -> int:
        # nominal hashing only: hashing the attribute schema would infinite-loop on self-references and provides no
        # benefit over name-based hashing since equality requires a full structural match anyway
        return hash(('OBJECT', self.name))

# Sentinel used inside OBJECT attribute schemas to denote "the enclosing OBJECT schema". Attached as a class attribute
# so it is available via ``DataType.OBJECT.self`` without depending on any particular OBJECT instance.
_ObjectDataTypeDef.self = _SelfReferenceDataTypeDef()
