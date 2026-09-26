# a4i writes its awaited entry points out separately rather than sharing them, and
# claims the two differ only in the awaiting. This is what makes the claim worth
# something: because it holds, a behaviour verified against the synchronous class is
# verified against the awaited one, and the awaited tests need only cover what this
# cannot see. The comparison runs on the syntax tree rather than on the text, so a line
# broken differently is not a difference.

from __future__ import annotations

import ast
import copy
import inspect

import pytest

from a4i import _client as client
from a4i import _session as session
from a4i import _transport as transport

# The pairs, named outright rather than found by their prefix: DaemonTransport
# has no awaited twin on purpose (see a4i._transport), and a rule that went
# looking for one would report it missing every time.
PAIRS = [
    (client, "Client", "AsyncClient"),
    (session, "Session", "AsyncSession"),
    (transport, "DirectTransport", "AsyncDirectTransport"),
]
IDS = [async_name for _, _, async_name in PAIRS]

# Methods this comparison leaves alone. An exemption is a place where this test
# says nothing, so each one carries its reason.
EXEMPT: dict[tuple[str, str], set[str]] = {
    # Builds httpx2's synchronous client on one side and its awaited one on the
    # other: the one line that cannot be the same line.
    ("Client", "AsyncClient"): {"__init__"},
    ("Session", "AsyncSession"): {"__init__"},
    ("DirectTransport", "AsyncDirectTransport"): set(),
}

# The awaited spelling of a method the language gives no keyword for.
ALIASES = {"__enter__": "__aenter__", "__exit__": "__aexit__"}

# Names that differ only in spelling: httpx2 calls its awaited close "aclose", and
# a4i prefixes an awaited class with "Async". Both are spelled back before
# anything is compared.
SPELLINGS = {"aclose": "close", **{async_name: sync for _, sync, async_name in PAIRS}}
SPELLINGS["AsyncTransport"] = "Transport"


class _Normalize(ast.NodeTransformer):
    def visit_Await(self, node: ast.Await) -> ast.AST:
        return self.visit(node.value)

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        node.attr = SPELLINGS.get(node.attr, node.attr)
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = SPELLINGS.get(node.id, node.id)
        return node


def _normalized(node: ast.AST) -> str:
    return ast.unparse(_Normalize().visit(copy.deepcopy(node)))


def _methods(module, class_name: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.parse(inspect.getsource(module)).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name: item
                for item in node.body
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            }
    raise AssertionError(f"{class_name} is not in {module.__name__}")


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    parts = [_normalized(decorator) for decorator in fn.decorator_list]
    parts.append(_normalized(fn.args))
    parts.append(_normalized(fn.returns) if fn.returns is not None else "")
    return " | ".join(parts)


# The docstring is left out: the awaited one says "see the other one".
def _body(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    statements = fn.body
    first = statements[0]
    if isinstance(first, ast.Expr) and isinstance(getattr(first.value, "value", None), str):
        statements = statements[1:]
    return "\n".join(_normalized(statement) for statement in statements)


# The half a comparison of bodies cannot do: a method written on one side alone has
# nothing to be compared against, so it would pass unnoticed.
@pytest.mark.parametrize(("module", "sync_name", "async_name"), PAIRS, ids=IDS)
def test_the_awaited_class_carries_the_same_methods(module, sync_name, async_name) -> None:
    expected = {ALIASES.get(name, name) for name in _methods(module, sync_name)}
    assert expected == set(_methods(module, async_name)), (
        f"{sync_name} and {async_name} no longer carry the same methods; "
        f"a method belongs on both or on neither"
    )


@pytest.mark.parametrize(("module", "sync_name", "async_name"), PAIRS, ids=IDS)
def test_every_awaited_method_is_the_synchronous_one_awaited(module, sync_name, async_name) -> None:
    synchronous = _methods(module, sync_name)
    awaited = _methods(module, async_name)
    exempt = EXEMPT[sync_name, async_name]
    compared = 0
    for name, fn in synchronous.items():
        if name in exempt:
            continue
        other = awaited[ALIASES.get(name, name)]
        assert _signature(fn) == _signature(other), f"{async_name}.{name} takes something else"
        assert _body(fn) == _body(other), (
            f"{async_name}.{name} is no longer {sync_name}.{name} awaited"
        )
        compared += 1
    # A comparison that ran over nothing would pass just as quietly.
    assert compared == len(synchronous) - len(exempt)


# The next method to take a gone name would be waved through on a reason written for
# something else.
def test_nothing_is_exempt_without_a_pair_to_be_exempt_from() -> None:
    for module, sync_name, async_name in PAIRS:
        for name in EXEMPT[sync_name, async_name]:
            assert name in _methods(module, sync_name), f"{sync_name}.{name} is gone"
            assert name in _methods(module, async_name), f"{async_name}.{name} is gone"
