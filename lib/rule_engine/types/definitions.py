#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/types/definitions.py
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

import collections
import collections.abc
import warnings
from typing import ClassVar, cast

from .. import errors

_PYTHON_FUNCTION_TYPE = type(lambda: None)
NoneType = type(None)

class _DataTypeDef(object):
    __slots__ = ('name', 'python_type', 'is_scalar', 'iterable_type')
    name: str
    python_type: type
    is_scalar: bool
    is_object: ClassVar[bool] = False
    def __init__(self, name: str, python_type: type) -> None:
        self.name = name
        self.python_type = python_type
        self.is_scalar = True
        if '__call__' in dir(self) and self.__call__.__doc__:  # type: ignore[operator]
            # patch the call docs into the top-level class for Sphinx
            self.__class__.__doc__ = self.__call__.__doc__  # type: ignore[operator]

    @property
    def is_iterable(self) -> bool:
        return getattr(self, 'iterable_type', None) is not None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, self.__class__):
            return False
        return self.name == other.name

    def __hash__(self) -> int:
        return hash((self.python_type, self.is_scalar))

    def __repr__(self) -> str:
        return "<{} name={} python_type={} >".format(self.__class__.__name__, self.name,  self.python_type.__name__)

    @property
    def is_compound(self) -> bool:
        return not self.is_scalar

class _UndefinedDataTypeDef(_DataTypeDef):
    def __repr__(self) -> str:
        return 'UNDEFINED'

_DATA_TYPE_UNDEFINED = _UndefinedDataTypeDef('UNDEFINED', cast(type, errors.UNDEFINED))

class _NullableDataTypeDef(_DataTypeDef):
    """项目内部接口说明。"""
    __slots__ = ('inner_type',)
    inner_type: _DataTypeDef
    def __init__(self, name: str, python_type: type, inner_type: _DataTypeDef = _DATA_TYPE_UNDEFINED) -> None:
        if isinstance(inner_type, _NullableDataTypeDef):
            inner_type = inner_type.inner_type
        if inner_type.python_type is NoneType:
            raise errors.EngineError('NULLABLE may not wrap NULL')
        super(_NullableDataTypeDef, self).__init__(name, python_type)
        self.is_scalar = False
        self.inner_type = inner_type

    def __call__(self, inner_type: _DataTypeDef) -> '_NullableDataTypeDef':
        """项目内部接口说明。"""
        if isinstance(inner_type, _UndefinedDataTypeDef):
            raise errors.EngineError('NULLABLE may not wrap UNDEFINED')
        return self.__class__(self.name, self.python_type, inner_type=inner_type)

    def __repr__(self) -> str:
        return "<{} name={} inner_type={} >".format(self.__class__.__name__, self.name, self.inner_type.name)

    def __eq__(self, other: object) -> bool:
        if not super().__eq__(other):
            return False
        assert isinstance(other, _NullableDataTypeDef)
        return self.inner_type == other.inner_type

    def __hash__(self) -> int:
        return hash((self.python_type, self.is_scalar, hash(self.inner_type)))

    @staticmethod
    def unwrap(dt: '_DataTypeDef') -> '_DataTypeDef':
        """项目内部接口说明。"""
        if isinstance(dt, _NullableDataTypeDef):
            return dt.inner_type
        return dt

    @staticmethod
    def wrap(dt: '_DataTypeDef') -> '_DataTypeDef':
        """项目内部接口说明。"""
        if isinstance(dt, (_NullableDataTypeDef, _UndefinedDataTypeDef)):
            return dt
        if dt.python_type is NoneType:
            return dt
        return _NullableDataTypeDef('NULLABLE', object, inner_type=dt)

class _CollectionDataTypeDef(_DataTypeDef):
    __slots__ = ('value_type',)
    value_type: _DataTypeDef
    def __init__(
            self,
            name: str,
            python_type: type,
            value_type: _DataTypeDef = _DATA_TYPE_UNDEFINED,
            value_type_nullable: bool | None = None
    ) -> None:
        # check these three classes individually instead of using Collection which isn't available before Python v3.6
        if not issubclass(python_type, collections.abc.Container):
            raise TypeError('the specified python_type is not a container')
        if not issubclass(python_type, collections.abc.Iterable):
            raise TypeError('the specified python_type is not an iterable')
        if not issubclass(python_type, collections.abc.Sized):
            raise TypeError('the specified python_type is not a sized')
        super(_CollectionDataTypeDef, self).__init__(name, python_type)
        self.is_scalar = False
        if value_type_nullable is not None:
            warnings.warn(
                    "The 'value_type_nullable' kwarg is deprecated and will be removed in v6.0.0; "
                    "wrap nullable element types with DataType.NULLABLE(T) instead.",
                    DeprecationWarning,
                    stacklevel=2
            )
            if value_type_nullable:
                value_type = _NullableDataTypeDef.wrap(value_type)
        self.value_type = value_type

    @property
    def iterable_type(self) -> _DataTypeDef:  # type: ignore[override]
        return self.value_type

    @property
    def value_type_nullable(self) -> bool:
        return isinstance(self.value_type, _NullableDataTypeDef)

    def __call__(self, value_type: _DataTypeDef, value_type_nullable: bool | None = None) -> _CollectionDataTypeDef:
        """项目内部接口说明。"""
        if value_type_nullable is not None:
            warnings.warn(
                    "The 'value_type_nullable' kwarg is deprecated and will be removed in v6.0.0; "
                    "wrap nullable element types with DataType.NULLABLE(T) instead.",
                    DeprecationWarning,
                    stacklevel=2
            )
            if value_type_nullable:
                value_type = _NullableDataTypeDef.wrap(value_type)
        return self.__class__(
                self.name,
                self.python_type,
                value_type=value_type
        )

    def __repr__(self) -> str:
        return "<{} name={} python_type={} value_type={} >".format(
                self.__class__.__name__,
                self.name,
                self.python_type.__name__,
                self.value_type.name
        )

    def __eq__(self, other: object) -> bool:
        if not super().__eq__(other):
            return False
        assert isinstance(other, _CollectionDataTypeDef)
        return self.value_type == other.value_type

    def __hash__(self) -> int:
        return hash((self.python_type, self.is_scalar, hash(self.value_type)))

class _ArrayDataTypeDef(_CollectionDataTypeDef):
    pass

class _SetDataTypeDef(_CollectionDataTypeDef):
    def __init__(
            self,
            name: str,
            python_type: type,
            value_type: _DataTypeDef = _DATA_TYPE_UNDEFINED,
            value_type_nullable: bool | None = None
    ) -> None:
        inner = value_type.inner_type if isinstance(value_type, _NullableDataTypeDef) else value_type
        if inner.is_object:
            raise errors.EngineError('the OBJECT data type may not be used as a SET member')
        super(_SetDataTypeDef, self).__init__(name, python_type, value_type=value_type, value_type_nullable=value_type_nullable)

class _MappingDataTypeDef(_DataTypeDef):
    __slots__ = ('key_type', 'value_type')
    key_type: _DataTypeDef
    value_type: _DataTypeDef
    def __init__(
            self,
            name: str,
            python_type: type,
            key_type: _DataTypeDef = _DATA_TYPE_UNDEFINED,
            value_type: _DataTypeDef = _DATA_TYPE_UNDEFINED,
            value_type_nullable: bool | None = None
    ) -> None:
        if not issubclass(python_type, collections.abc.Mapping):
            raise TypeError('the specified python_type is not a mapping')
        super(_MappingDataTypeDef, self).__init__(name, python_type)
        self.is_scalar = False
        # ARRAY is the only compound data type that can be used as a mapping key, this is because ARRAY's are backed by
        # Python tuple's while SET and MAPPING objects are set and dict instances, respectively which are not hashable.
        if key_type.is_compound and not isinstance(key_type, _ArrayDataTypeDef):
            raise errors.EngineError("the {} data type may not be used for mapping keys".format(key_type.name))
        if value_type_nullable is not None:
            warnings.warn(
                    "The 'value_type_nullable' kwarg is deprecated and will be removed in v6.0.0; "
                    "wrap nullable value types with DataType.NULLABLE(T) instead.",
                    DeprecationWarning,
                    stacklevel=2
            )
            if value_type_nullable:
                value_type = _NullableDataTypeDef.wrap(value_type)
        self.key_type = key_type
        self.value_type = value_type

    @property
    def iterable_type(self) -> _DataTypeDef:  # type: ignore[override]
        return self.key_type

    @property
    def value_type_nullable(self) -> bool:
        return isinstance(self.value_type, _NullableDataTypeDef)

    def __call__(
            self,
            key_type: _DataTypeDef,
            value_type: _DataTypeDef = _DATA_TYPE_UNDEFINED,
            value_type_nullable: bool | None = None
    ) -> _MappingDataTypeDef:
        """项目内部接口说明。"""
        if value_type_nullable is not None:
            warnings.warn(
                    "The 'value_type_nullable' kwarg is deprecated and will be removed in v6.0.0; "
                    "wrap nullable value types with DataType.NULLABLE(T) instead.",
                    DeprecationWarning,
                    stacklevel=2
            )
            if value_type_nullable:
                value_type = _NullableDataTypeDef.wrap(value_type)
        return self.__class__(
                self.name,
                self.python_type,
                key_type=key_type,
                value_type=value_type
        )

    def __repr__(self) -> str:
        return "<{} name={} python_type={} key_type={} value_type={} >".format(
                self.__class__.__name__,
                self.name,
                self.python_type.__name__,
                self.key_type.name,
                self.value_type.name
        )

    def __eq__(self, other: object) -> bool:
        if not super().__eq__(other):
            return False
        assert isinstance(other, _MappingDataTypeDef)
        return all((
                self.key_type == other.key_type,
                self.value_type == other.value_type
        ))

    def __hash__(self) -> int:
        return hash((self.python_type, self.is_scalar, hash((self.key_type, self.value_type))))

class _FunctionDataTypeDef(_DataTypeDef):
    __slots__ = ('value_name', 'return_type', 'argument_types', 'minimum_arguments')
    value_name: str | None
    return_type: _DataTypeDef
    argument_types: tuple[_DataTypeDef, ...] | _DataTypeDef
    minimum_arguments: int | _DataTypeDef
    def __init__(
            self,
            name: str,
            python_type: type,
            value_name: str | None = None,
            return_type: _DataTypeDef = _DATA_TYPE_UNDEFINED,
            argument_types: tuple[_DataTypeDef, ...] | _DataTypeDef = _DATA_TYPE_UNDEFINED,
            minimum_arguments: int | _DataTypeDef | None = None
    ) -> None:
        super(_FunctionDataTypeDef, self).__init__(name, python_type)
        self.value_name = value_name
        self.return_type = return_type
        if argument_types is _DATA_TYPE_UNDEFINED:
            if minimum_arguments is None:
                minimum_arguments = _DATA_TYPE_UNDEFINED
        else:
            if not isinstance(argument_types, collections.abc.Sequence):
                raise TypeError('argument_types must be a sequence (list or tuple)')
            if minimum_arguments is None:
                # if arguments are specified, assume that they're all required by default
                minimum_arguments = len(argument_types)
            if len(argument_types) < minimum_arguments:  # type: ignore[operator]
                raise ValueError('minimum_arguments can not be greater than the length of argument_types')
        self.argument_types = argument_types
        self.minimum_arguments = minimum_arguments

    def __call__(
            self,
            name: str,
            return_type: _DataTypeDef = _DATA_TYPE_UNDEFINED,
            argument_types: tuple[_DataTypeDef, ...] | _DataTypeDef = _DATA_TYPE_UNDEFINED,
            minimum_arguments: int | _DataTypeDef | None = None
    ) -> _FunctionDataTypeDef:
        """项目内部接口说明。"""
        return self.__class__(
                self.name,
                self.python_type,
                value_name=name,
                return_type=return_type,
                argument_types=argument_types,
                minimum_arguments=minimum_arguments
        )
    def __repr__(self) -> str:
        return "<{} name={} python_type={} return_type={} >".format(
                self.__class__.__name__,
                self.name,
                self.python_type.__name__,
                self.return_type.name
        )

    def __eq__(self, other: object) -> bool:
        if not super().__eq__(other):
            return False
        assert isinstance(other, _FunctionDataTypeDef)
        return all((
                self.return_type == other.return_type,
                self.argument_types == other.argument_types,
                self.minimum_arguments == other.minimum_arguments
        ))

    def __hash__(self) -> int:
        return hash((self.python_type, self.is_scalar, hash((self.return_type, self.argument_types, self.minimum_arguments))))

class _ReferenceDataTypeDef(_DataTypeDef):
    """项目内部接口说明。"""
    __slots__ = ()
    def __init__(self, name: str) -> None:
        super(_ReferenceDataTypeDef, self).__init__(name, object)
        self.is_scalar = False

    def __repr__(self) -> str:
        return "<{} name={} (unresolved forward reference) >".format(self.__class__.__name__, self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _ReferenceDataTypeDef):
            return False
        return self.name == other.name

    def __hash__(self) -> int:
        return hash(('REFERENCE', self.name))

class _SelfReferenceDataTypeDef(_ReferenceDataTypeDef):
    """项目内部接口说明。"""
    __slots__ = ()
    def __init__(self) -> None:
        super(_SelfReferenceDataTypeDef, self).__init__('__self__')

    def __repr__(self) -> str:
        return "<{} (self reference placeholder) >".format(self.__class__.__name__)
