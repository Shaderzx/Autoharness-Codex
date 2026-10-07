from codex_autoharness.lib import lifecycle


def _m(name, calls, anchor=0):
    return {"name": name, "calls": calls, "anchor": anchor}


def test_denominator_grows_with_requests():
    # same symbol: probation at low request count, joins the mature pool once denom passes maturity
    m = [_m("x", 0, anchor=0), _m("y", 8, anchor=0)]
    assert lifecycle.evaluate(m, request_count=9, maturity=10, capacity=1) == []       # denom 9 < 10: both probation
    assert lifecycle.evaluate(m, request_count=10, maturity=10, capacity=1) == ["x"]   # mature pool of 2 over cap 1, lowest rate out


def test_capacity_tiebreak_by_name_deterministic():
    # equal rates, capacity forces one out → lexicographically-first name archived
    members = [_m("b", 10), _m("a", 10), _m("c", 10)]  # all rate .1
    out = lifecycle.evaluate(members, request_count=100, maturity=10, capacity=2)
    assert out == ["a"]


def _m3(name, use, view=0, anchor=0):
    return {"name": name, "use": use, "view": view, "anchor": anchor}


def test_graduation_review_needs_use_and_view_both_zero():
    # viewed-but-never-invoked: content had recall value (Read is the measured dominant path) -> spared
    out = lifecycle.evaluate([_m3("viewed", 0, view=3)], request_count=20, maturity=10, capacity=5)
    assert out == []
    out = lifecycle.evaluate([_m3("dead", 0, view=0)], request_count=20, maturity=10, capacity=5)
    assert out == ["dead"]


def test_graduation_review_suspend_gate_archives_nothing(monkeypatch):
    out = lifecycle.evaluate([_m3("dead", 0)], request_count=20, maturity=10, capacity=5,
                             review_suspended=True)
    assert out == []  # broken-surfacing period: zero-use must not be a death sentence


def test_view_never_feeds_the_rate():
    # capacity race: high-view/low-use must not outrank low-view/high-use
    members = [_m3("viewy", 1, view=99), _m3("usey", 50, view=0)]
    out = lifecycle.evaluate(members, request_count=100, maturity=10, capacity=1)
    assert out == ["viewy"]
