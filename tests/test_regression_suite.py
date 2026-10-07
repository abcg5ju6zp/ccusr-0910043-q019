"""运行项目 README 声明的核心回归测试。"""

from importlib import import_module


engine = import_module("tests.engine")
incremental = import_module("tests.incremental")
parser = import_module("tests.parser")
thread_safety = import_module("tests.thread_safety")


EngineTests = engine.EngineTests
EngineRuleTests = engine.EngineRuleTests
EngineDatetimeRuleTests = engine.EngineDatetimeRuleTests
ContextTests = engine.ContextTests
ObjectTypeTests = engine.ObjectTypeTests
ContextSerializationTests = engine.ContextSerializationTests
IncrementalEvaluationTests = incremental.IncrementalEvaluationTests
ParserTests = parser.ParserTests
ParserLeftOperatorRightTests = parser.ParserLeftOperatorRightTests
ParserLiteralTests = parser.ParserLiteralTests
ThreadSafetyTests = thread_safety.ThreadSafetyTests
