"""Static proof that no code path in app/graph writes a numeric confidence
value. Pure source inspection — no database, always runs (never skipped).
"""

import ast
import inspect

import app.graph.service as graph_service


def _confidence_kwarg_values() -> list[ast.expr]:
    """Every `confidence=<value>` keyword argument anywhere in the module's
    source, as AST expression nodes."""
    tree = ast.parse(inspect.getsource(graph_service))
    values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "confidence":
                    values.append(keyword.value)
    return values


def test_no_code_path_writes_a_numeric_confidence() -> None:
    confidence_values = _confidence_kwarg_values()
    assert len(confidence_values) >= 1  # sanity: the check actually found the write site

    for value in confidence_values:
        # The only honest v1 value is the literal `None` — never an int or
        # float constant (0.0, 0.5, 0.9, 0.95, 0.99, 1.0, ...).
        assert isinstance(value, ast.Constant) and value.value is None, (
            f"found a non-None confidence= argument: {ast.dump(value)}"
        )
