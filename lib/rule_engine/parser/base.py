#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/parser/base.py
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

import threading
from typing import TYPE_CHECKING, Any

from .._vendor.ply import lex, yacc

if TYPE_CHECKING:
    from ..engine.context import Context
    from ..ast import Statement

class ParserBase(object):
    """项目内部接口说明。"""
    precedence: tuple[tuple[str, ...], ...] = ()
    """The precedence for operators."""
    tokens: tuple[str, ...] = ()
    reserved_words: dict[str, str] = {}
    """
    A mapping of literal words which are reserved to their corresponding grammar
    names.
    """
    __mutex = threading.Lock()
    def __init__(self, debug: bool = False) -> None:
        """项目内部接口说明。"""
        self.debug = debug
        self.context: 'Context | None' = None
        # Build the lexer and parser
        self._lexer = lex.lex(module=self, debug=self.debug)
        self._parser = yacc.yacc(module=self, debug=self.debug, write_tables=self.debug)

    def parse(self, text: str, context: 'Context', **kwargs: Any) -> 'Statement':
        """项目内部接口说明。"""
        kwargs['lexer'] = kwargs.pop('lexer', self._lexer)
        with self.__mutex:
            self.context = context
            # phase 1: parse the string into a tree of deferred nodes
            result = self._parser.parse(text, **kwargs)
            self.context = None
        # phase 2: initialize each AST node recursively, providing them with an opportunity to define assignments
        return result.build()
