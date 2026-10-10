from app.graph.python_extractor import extract_graph_candidates


def test_bare_call_candidate() -> None:
    result = extract_graph_candidates("def checkout():\n    charge()\n", "mod")
    assert result.status == "extracted"
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.scope_qualified_name == "mod.checkout"
    assert call.name_chain == ("charge",)
    assert call.line == 2


def test_self_attribute_call_candidate() -> None:
    source = "class C:\n    def checkout(self):\n        self.charge()\n"
    result = extract_graph_candidates(source, "mod")
    call = result.calls[0]
    assert call.scope_qualified_name == "mod.C.checkout"
    assert call.name_chain == ("self", "charge")


def test_module_attribute_call_candidate() -> None:
    result = extract_graph_candidates("payments.charge()\n", "mod")
    assert result.calls[0].name_chain == ("payments", "charge")


def test_deep_attribute_chain_candidate() -> None:
    result = extract_graph_candidates("a.b.c.charge()\n", "mod")
    assert result.calls[0].name_chain == ("a", "b", "c", "charge")


def test_dynamic_receiver_produces_no_candidate_for_the_outer_call() -> None:
    """`get_service().charge()` has two calls: `get_service()` itself (a
    perfectly valid bare-name candidate) and `.charge()` on its *result*
    (a dynamic receiver — nothing provable, no candidate for that one)."""
    result = extract_graph_candidates("get_service().charge()\n", "mod")
    name_chains = {c.name_chain for c in result.calls}
    assert name_chains == {("get_service",)}


def test_subscript_receiver_produces_no_candidate() -> None:
    result = extract_graph_candidates("handlers['x']()\n", "mod")
    assert result.calls == []


def test_nested_call_inside_call_arguments_is_still_found() -> None:
    result = extract_graph_candidates("outer(inner())\n", "mod")
    names = {c.name_chain for c in result.calls}
    assert ("outer",) in names
    assert ("inner",) in names


def test_call_inside_if_condition_is_found() -> None:
    result = extract_graph_candidates("if charge():\n    pass\n", "mod")
    assert result.calls[0].name_chain == ("charge",)


def test_call_inside_comprehension_is_attributed_to_enclosing_scope() -> None:
    """Documented simplification: comprehensions don't get their own
    tracked scope — see graph-construction.md section 13."""
    source = "def f():\n    return [charge() for _ in range(3)]\n"
    result = extract_graph_candidates(source, "mod")
    assert result.calls[0].scope_qualified_name == "mod.f"


def test_inheritance_candidate_resolves_in_enclosing_scope() -> None:
    source = "class Payment:\n    pass\n\nclass Premium(Payment):\n    pass\n"
    result = extract_graph_candidates(source, "mod")
    assert len(result.inheritances) == 1
    inh = result.inheritances[0]
    assert inh.subclass_qualified_name == "mod.Premium"
    assert inh.scope_qualified_name == "mod"  # NOT "mod.Premium" — resolves in the enclosing scope
    assert inh.name_chain == ("Payment",)


def test_dotted_inheritance_candidate() -> None:
    result = extract_graph_candidates("class C(payments.Base):\n    pass\n", "mod")
    assert result.inheritances[0].name_chain == ("payments", "Base")


def test_dynamic_base_produces_no_inheritance_candidate() -> None:
    result = extract_graph_candidates("class C(make_base()):\n    pass\n", "mod")
    assert result.inheritances == []
    # but the call inside the dynamic base expression is still found
    assert result.calls[0].name_chain == ("make_base",)


def test_nested_class_inheritance_resolves_in_its_own_enclosing_scope() -> None:
    source = "def make():\n    class Inner(Base):\n        pass\n    return Inner\n"
    result = extract_graph_candidates(source, "mod")
    inh = result.inheritances[0]
    assert inh.subclass_qualified_name == "mod.make.Inner"
    assert inh.scope_qualified_name == "mod.make"


def test_assignment_target_is_tracked_as_bound_name() -> None:
    source = "def checkout():\n    charge = get_handler()\n    charge()\n"
    result = extract_graph_candidates(source, "mod")
    assert "charge" in result.locally_bound_names["mod.checkout"]


def test_for_loop_target_is_tracked_as_bound_name() -> None:
    source = "def f():\n    for charge in items:\n        pass\n"
    result = extract_graph_candidates(source, "mod")
    assert "charge" in result.locally_bound_names["mod.f"]


def test_with_as_target_is_tracked_as_bound_name() -> None:
    source = "def f():\n    with open('x') as charge:\n        pass\n"
    result = extract_graph_candidates(source, "mod")
    assert "charge" in result.locally_bound_names["mod.f"]


def test_tuple_unpacking_target_is_tracked() -> None:
    source = "def f():\n    charge, other = get_both()\n"
    result = extract_graph_candidates(source, "mod")
    assert "charge" in result.locally_bound_names["mod.f"]
    assert "other" in result.locally_bound_names["mod.f"]


def test_parameters_are_tracked_as_bound_names() -> None:
    source = "def f(charge, *args, extra=None, **kwargs):\n    pass\n"
    result = extract_graph_candidates(source, "mod")
    bound = result.locally_bound_names["mod.f"]
    assert {"charge", "args", "extra", "kwargs"} <= bound


def test_first_parameter_is_recorded_per_scope() -> None:
    source = "class C:\n    def m(self, x):\n        pass\n\ndef f(a, b):\n    pass\n"
    result = extract_graph_candidates(source, "mod")
    assert result.first_parameter_by_scope["mod.C.m"] == "self"
    assert result.first_parameter_by_scope["mod.f"] == "a"


def test_function_with_no_parameters_has_none_first_parameter() -> None:
    result = extract_graph_candidates("def f():\n    pass\n", "mod")
    assert result.first_parameter_by_scope["mod.f"] is None


def test_attribute_assignment_target_does_not_bind_a_plain_name() -> None:
    """`self.x = 1` binds an attribute, not a local name — must not be
    treated as shadowing a bare `x` reference."""
    source = "class C:\n    def m(self):\n        self.x = 1\n        x()\n"
    result = extract_graph_candidates(source, "mod")
    assert "x" not in result.locally_bound_names.get("mod.C.m", frozenset())


def test_syntax_error_returns_failed_status_not_an_exception() -> None:
    result = extract_graph_candidates("def broken(:\n", "mod")
    assert result.status == "failed"
    assert result.calls == []
    assert result.inheritances == []


def test_decorator_call_is_extracted_in_enclosing_scope() -> None:
    source = "@decorator()\ndef f():\n    pass\n"
    result = extract_graph_candidates(source, "mod")
    assert result.calls[0].name_chain == ("decorator",)
    assert result.calls[0].scope_qualified_name == "mod"
