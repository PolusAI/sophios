"""The safe JavaScript subset in Groovy: the Nextflow target's rendering and its reading.

The subset itself, its grammar, types and canonical form, is
``sophios.lang.subset``. Every admitted expression renders to Groovy that
means what the JavaScript means (design §6, Expressions), and the reader
takes back exactly what the renderer writes (design §4.2).
"""

import re
from typing import Mapping

from .lang.subset import CALLS, Expr, canonical, source_text

NF_FINITE_HELPER = "__sophios_finite_9f72e"
NF_ROUND_HELPER = "__sophios_round_9f72e"


CONTROL_ESCAPES = {code: f"\\u{code:04x}" for code in (*range(32), 127)}
_GROOVY_LITERAL_TABLE = {ord("\\"): "\\\\", ord("'"): "\\'", **CONTROL_ESCAPES}


def groovy_literal(value: str) -> str:
    """``value`` as a single-quoted Groovy string: the one escaper for that literal."""
    return f"'{value.translate(_GROOVY_LITERAL_TABLE)}'"


_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)", re.DOTALL)


def groovy_unescape(text: str) -> str:
    """The value Groovy reads from ``text``, the inside of a string, whose escapes are a backslash and a
    character, or ``\\u`` and four hex digits."""
    return _ESCAPE.sub(lambda found: chr(int(found[1][1:], 16)) if len(found[1]) == 5 else found[1], text)


#: A single-quoted Groovy string and a Groovy identifier: the text of the idiom table's holes, and what the readers
#: of the generated text match.
GROOVY_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'")
GROOVY_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GROOVY_CAST = re.compile(rf"\(\(({GROOVY_IDENTIFIER.pattern})\) as double\)")
_COMPARISONS = (" || ", " && ", " <= ", " >= ", " < ", " > ", " == ", " != ")


def read_groovy(text: str, variables: Mapping[str, str]) -> Expr:
    """The tree :func:`render_groovy` writes as ``text``: its inverse, on canonical trees (design §4.2).

    ``variables`` maps each Groovy variable the text reads to the input it
    holds. Text that is not such a rendering raises ValueError.
    """
    at = 0

    def take(prefix: str) -> bool:
        nonlocal at
        if text.startswith(prefix, at):
            at += len(prefix)
            return True
        return False

    def expect(prefix: str) -> None:
        if not take(prefix):
            raise ValueError(f"expected {prefix!r} at {text[at:at + 40]!r}")

    def literal() -> str:
        nonlocal at
        if (found := GROOVY_LITERAL.match(text, at)) is None:
            raise ValueError(f"expected a string at {text[at:at + 40]!r}")
        at = found.end()
        return groovy_unescape(found.group()[1:-1])

    def reference() -> Expr:
        nonlocal at
        if (found := GROOVY_IDENTIFIER.match(text, at)) is None or found.group() not in variables:
            raise ValueError(f"expected an input at {text[at:at + 40]!r}")
        at = found.end()
        return Expr("ref", value=variables[found.group()])

    def operand() -> Expr:
        nonlocal at
        if take("Double.valueOf("):
            number = literal()
            expect(")")
            return Expr("number", value=number)
        if text.startswith("'", at):
            return Expr("string", value=literal())
        for word, constant in (("true", Expr("boolean", value=True)), ("false", Expr("boolean", value=False)),
                               ("null", Expr("null"))):
            if take(word):
                return constant
        if take(f"{NF_FINITE_HELPER}("):
            # The check's where, part and inputs are the node's to render; they are not read.
            node = numeric()
            expect(", ")
            literal()
            expect(", ")
            literal()
            expect(", [")
            at = text.index("]", at) + 1
            expect(")")
            return node
        if (cast := _GROOVY_CAST.match(text, at)) is not None:
            at = cast.end()
            return read_groovy(cast[1], variables)
        if take("(!"):
            node = operand()
            expect(")")
            return Expr("u!", (node,))
        if take("("):
            left = operand()
            if take(" == null || "):
                operand()
                expect(" == [])")
                return Expr("==", (left, Expr("null")))
            return binary(left, _COMPARISONS)
        return reference()

    def binary(left: Expr, operators: tuple[str, ...]) -> Expr:
        for operator in operators:
            if take(operator):
                right = operand()
                expect(")")
                return Expr(operator.strip(), (left, right))
        raise ValueError(f"expected an operator at {text[at:at + 40]!r}")

    def folded(call: str) -> list[Expr]:
        # One call opens per argument after the first, so their count, not a recursion, pairs the closes.
        calls = 0
        while take(f"{call}("):
            calls += 1
        args = [operand()]
        for _ in range(calls):
            expect(", ")
            args.append(operand())
            expect(")")
        return args

    def numeric() -> Expr:
        if take("(-"):
            node = operand()
            expect(")")
            return Expr("u-", (node,))
        for call in ("Math.min", "Math.max"):
            if text.startswith(f"{call}(", at):
                return Expr(call, tuple(folded(call)))
        for spelling, call in ((f"{NF_ROUND_HELPER}(", "Math.round"), ("StrictMath.pow(", "Math.pow"),
                               *((f"{name}(", name) for name in ("Math.abs", "Math.sqrt", "Math.floor", "Math.ceil"))):
            if take(spelling):
                args = [operand()]
                while take(", "):
                    args.append(operand())
                expect(")")
                return Expr(call, tuple(args))
        expect("(")
        return binary(operand(), (" + ", " - ", " * ", " / ", " % "))

    tree = operand()
    if at != len(text):
        raise ValueError(f"unexpected text {text[at:at + 40]!r}")
    return canonical(tree)


def render_groovy(node: Expr, *, where: str, inputs: str, variables: Mapping[str, str] | None = None) -> str:
    """Render the fixed Groovy idiom for a typed tree.

    Every numeric node passes through the finite check, so NaN or ±Infinity
    anywhere fails with a diagnostic naming ``where``, the subexpression,
    and the ``inputs`` map expression. ``variables`` maps an input to the
    Groovy variable that holds it, when that is not the input's own name.
    """
    def numeric(expression: str, child: Expr) -> str:
        return (
            f"{NF_FINITE_HELPER}({expression}, {groovy_literal(where)}, "
            f"{groovy_literal(source_text(child))}, {inputs})"
        )

    def go(child: Expr) -> str:
        args = [go(arg) for arg in child.args]
        match child.op:
            case "number":
                return f"Double.valueOf({groovy_literal(str(child.value))})"
            case "string":
                return groovy_literal(child.value)
            case "boolean":
                return "true" if child.value else "false"
            case "null":
                return "null"
            case "ref":
                name = (variables or {}).get(child.value, child.value)
                return f"(({name}) as double)" if child in _numeric_refs else name
            case "u!":
                return f"(!{args[0]})"
            case "u-":
                return numeric(f"(-{args[0]})", child)
            case "u+":
                return args[0]
            case "&&" | "||" | "<" | "<=" | ">" | ">=":
                return f"({args[0]} {child.op} {args[1]})"
            case "==" | "===" | "!=" | "!==" if any(arg.op == "null" for arg in child.args):
                # An absent optional value arrives as the reserved [] sentinel
                # (design §6, Inputs and channels), so a null check tests for it.
                other = args[1] if child.args[0].op == "null" else args[0]
                absent = f"({other} == null || {other} == [])"
                return absent if child.op in {"==", "==="} else f"(!{absent})"
            case "==" | "===":
                return f"({args[0]} == {args[1]})"
            case "!=" | "!==":
                return f"({args[0]} != {args[1]})"
            case "+" | "-" | "*" | "/" | "%":
                return numeric(f"({args[0]} {child.op} {args[1]})", child)
            case "Math.round":
                return numeric(f"{NF_ROUND_HELPER}({args[0]})", child)
            case "Math.pow":
                return numeric(f"StrictMath.pow({', '.join(args)})", child)
            case "Math.min" | "Math.max":
                folded = args[0]
                for arg in args[1:]:
                    folded = f"{child.op}({folded}, {arg})"
                return numeric(folded, child)
            case call if call in CALLS:
                return numeric(f"{call}({', '.join(args)})", child)
            case _:
                raise ValueError(f"unsupported construct {child.op!r}")

    _numeric_refs = _numeric_ref_nodes(node)
    return go(node)


def _numeric_ref_nodes(node: Expr) -> set[Expr]:
    """References used as numbers: operands of arithmetic, comparison, or calls."""
    numeric_ops = {"u-", "u+", "<", "<=", ">", ">=", "+", "-", "*", "/", "%", *CALLS}
    found: set[Expr] = set()
    if node.op in numeric_ops:
        found.update(arg for arg in node.args if arg.op == "ref")
    for arg in node.args:
        found |= _numeric_ref_nodes(arg)
    return found


NF_EXPRESSION_FUNCTIONS = f'''def {NF_FINITE_HELPER}(double value, String where, String part, Map inputs) {{
    if( Double.isNaN(value) || Double.isInfinite(value) ) {{
        String message = "Sophios expression " + where + ": " + part + " is " + value + " with inputs " + inputs
        // A when is evaluated by an operator, and Nextflow reports an operator's failure without its message.
        log.error(message)
        throw new IllegalStateException(message)
    }}
    // + 0.0d turns -0.0 into 0.0: JavaScript never tells them apart, Groovy == does.
    return value + 0.0d
}}
def {NF_ROUND_HELPER}(double value) {{
    double floor = Math.floor(value)
    return value - floor >= 0.5d ? floor + 1.0d : floor
}}'''
