from app.parsing.parser import parse_source_file
from app.parsing.result import ParsedSymbol, ParseResult


def _by_qname(result: ParseResult, qualified_name: str) -> ParsedSymbol:
    matches = [s for s in result.symbols if s.qualified_name == qualified_name]
    assert len(matches) == 1, (
        f"expected exactly one symbol {qualified_name!r}, found {len(matches)}"
    )
    return matches[0]


def _parse(source: str, path: str = "mod.py") -> ParseResult:
    return parse_source_file(source, path, "python")


# 1. empty Python file
def test_empty_file_has_only_a_module_symbol() -> None:
    result = _parse("")
    assert result.status == "parsed"
    assert [s.kind for s in result.symbols] == ["module"]


# 2. module-level function
def test_module_level_function() -> None:
    result = _parse("def greet():\n    pass\n")
    fn = _by_qname(result, "mod.greet")
    assert fn.kind == "function"
    assert fn.parent_qualified_name == "mod"


# 3. async function
def test_async_function() -> None:
    result = _parse("async def fetch():\n    pass\n")
    fn = _by_qname(result, "mod.fetch")
    assert fn.kind == "function"  # no separate kind — async-ness is in signature
    assert fn.signature is not None
    assert fn.signature.startswith("async def fetch(")


# 4. class
def test_class() -> None:
    result = _parse("class Widget:\n    pass\n")
    cls = _by_qname(result, "mod.Widget")
    assert cls.kind == "class"
    assert cls.parent_qualified_name == "mod"


# 5. class method
def test_class_method() -> None:
    source = "class Widget:\n    def render(self):\n        pass\n"
    result = _parse(source)
    method = _by_qname(result, "mod.Widget.render")
    assert method.kind == "method"
    assert method.parent_qualified_name == "mod.Widget"


# 6. nested function
def test_nested_function() -> None:
    source = "def outer():\n    def inner():\n        pass\n    return inner\n"
    result = _parse(source)
    inner = _by_qname(result, "mod.outer.inner")
    assert inner.kind == "function"  # nested, not a method
    assert inner.parent_qualified_name == "mod.outer"


# 7. nested class
def test_nested_class() -> None:
    source = "def make():\n    class Inner:\n        pass\n    return Inner\n"
    result = _parse(source)
    inner = _by_qname(result, "mod.make.Inner")
    assert inner.kind == "class"
    assert inner.parent_qualified_name == "mod.make"


# 8. decorators
def test_decorators_are_captured_in_signature() -> None:
    source = "@staticmethod\n@cached\ndef compute():\n    pass\n"
    result = _parse(source)
    fn = _by_qname(result, "mod.compute")
    assert fn.signature is not None
    assert "@staticmethod" in fn.signature
    assert "@cached" in fn.signature


# 9. inheritance syntax
def test_class_inheritance_is_captured_in_signature() -> None:
    result = _parse("class Admin(User, Auditable):\n    pass\n")
    cls = _by_qname(result, "mod.Admin")
    assert cls.signature == "class Admin(User, Auditable)"


# 10. function arguments
def test_function_arguments_are_captured_in_signature() -> None:
    result = _parse("def charge(self, amount: int = 0, *args, **kwargs):\n    pass\n")
    fn = _by_qname(result, "mod.charge")
    assert fn.signature == "def charge(self, amount: int=0, *args, **kwargs)"


# 11. return annotations
def test_return_annotation_is_captured_in_signature() -> None:
    result = _parse("def is_valid() -> bool:\n    pass\n")
    fn = _by_qname(result, "mod.is_valid")
    assert fn.signature == "def is_valid() -> bool"


# 12. imports
def test_plain_and_dotted_imports() -> None:
    result = _parse("import os\nimport os.path\n")
    assert result.imports[0].module == "os"
    assert result.imports[0].level == 0
    assert result.imports[1].module == "os.path"


def test_from_import() -> None:
    result = _parse("from collections import OrderedDict\n")
    imp = result.imports[0]
    assert imp.module == "collections"
    assert imp.imported_name == "OrderedDict"
    assert imp.level == 0


# 13. relative imports
def test_relative_imports() -> None:
    source = "from . import sibling\nfrom .pkg import thing\nfrom ..pkg2 import other\n"
    result = _parse(source)
    assert result.imports[0].level == 1
    assert result.imports[0].module is None
    assert result.imports[0].imported_name == "sibling"
    assert result.imports[1].level == 1
    assert result.imports[1].module == "pkg"
    assert result.imports[2].level == 2
    assert result.imports[2].module == "pkg2"


# 14. aliases
def test_import_aliases() -> None:
    result = _parse("import numpy as np\nfrom collections import OrderedDict as OD\n")
    assert result.imports[0].alias == "np"
    assert result.imports[1].alias == "OD"


# 15. wildcard import
def test_wildcard_import() -> None:
    result = _parse("from os.path import *\n")
    imp = result.imports[0]
    assert imp.is_wildcard is True
    assert imp.imported_name is None
    assert imp.module == "os.path"


# 16. multiline function
def test_multiline_function_spans_correct_lines() -> None:
    source = "def process(\n    a,\n    b,\n):\n    x = a + b\n    return x\n"
    result = _parse(source)
    fn = _by_qname(result, "mod.process")
    assert fn.start_line == 1
    assert fn.end_line == 6


# 17. one-line function
def test_one_line_function_has_matching_start_and_end_line() -> None:
    result = _parse("def f(): return 1\n")
    fn = _by_qname(result, "mod.f")
    assert fn.start_line == 1
    assert fn.end_line == 1


# 18. syntax error
def test_syntax_error_is_reported_not_raised() -> None:
    result = _parse("def broken(:\n    pass\n")
    assert result.status == "failed"
    assert result.syntax_error is not None
    assert result.syntax_error.line == 1
    assert result.symbols == []


def test_syntax_error_does_not_raise_an_exception() -> None:
    # The real assertion: calling this must never raise, for any malformed input.
    result = _parse("class (:::\n\tdef\n")
    assert result.status == "failed"


# 19. Unicode identifiers/comments
def test_unicode_identifiers_and_comments() -> None:
    source = "# café ☕\ndef café_price() -> int:\n    naïve = 1\n    return naïve\n"
    result = _parse(source)
    assert result.status == "parsed"
    fn = _by_qname(result, "mod.café_price")
    assert fn.name == "café_price"


# 20. deterministic repeated parsing
def test_repeated_parsing_is_deterministic() -> None:
    source = "class A:\n    def m(self):\n        pass\n\ndef f(x: int) -> int:\n    return x\n"
    first = _parse(source)
    second = _parse(source)
    assert first == second


# 21. stable qualified names
def test_qualified_names_follow_ast_nesting_not_names_alone() -> None:
    source = (
        "class Outer:\n"
        "    class Inner:\n"
        "        def method(self):\n"
        "            def helper():\n"
        "                pass\n"
    )
    result = _parse(source)
    qnames = {s.qualified_name for s in result.symbols}
    assert "mod.Outer" in qnames
    assert "mod.Outer.Inner" in qnames
    assert "mod.Outer.Inner.method" in qnames
    assert "mod.Outer.Inner.method.helper" in qnames


# 22. correct source locations
def test_decorator_does_not_shift_start_line_of_the_def_itself() -> None:
    source = "@decorator\ndef f():\n    pass\n"
    result = _parse(source)
    fn = _by_qname(result, "mod.f")
    # ast.FunctionDef.lineno is the `def` line itself in modern Python, not
    # the decorator line — documented explicitly in source-intelligence.md.
    assert fn.start_line == 2


def test_column_offsets_are_zero_indexed() -> None:
    result = _parse("class C:\n    def m(self):\n        pass\n")
    method = _by_qname(result, "mod.C.m")
    assert method.start_column == 4  # four spaces of indentation


def test_module_path_derivation_for_init_file() -> None:
    result = _parse("x = 1\n", path="pkg/subpkg/__init__.py")
    module_symbol = _by_qname(result, "pkg.subpkg")
    assert module_symbol.kind == "module"


def test_module_path_derivation_for_ordinary_file() -> None:
    result = _parse("x = 1\n", path="pkg/subpkg/thing.py")
    module_symbol = _by_qname(result, "pkg.subpkg.thing")
    assert module_symbol.kind == "module"


def test_def_nested_inside_if_block_is_still_found() -> None:
    """Symbols nested inside control flow (if/for/while/try/with), not just
    directly inside a function/class body, must still be discovered."""
    source = "if True:\n    def conditional():\n        pass\n"
    result = _parse(source)
    fn = _by_qname(result, "mod.conditional")
    assert fn.kind == "function"


def test_unsupported_language_is_skipped_not_failed() -> None:
    result = parse_source_file("<html></html>\n", "index.html", "html")
    assert result.status == "skipped"
    assert result.symbols == []


def test_unknown_language_is_skipped() -> None:
    result = parse_source_file("whatever\n", "data.xyz", None)
    assert result.status == "skipped"
