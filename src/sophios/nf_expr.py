"""The safe JavaScript subset in Groovy: the Nextflow target's rendering and its reading.

The subset itself, its grammar, types and canonical form, is
``sophios.lang.subset``. Every admitted expression renders to Groovy that
means what the JavaScript means (design §6, Expressions), and the reader
takes back exactly what the renderer writes (design §4.2).
"""

import re
from typing import Mapping

from .lang.subset import CALLS, Boolean, Expr, Null, Number, Op, Ref, Text, canonical, source_text

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

    def reference() -> Ref:
        nonlocal at
        if (found := GROOVY_IDENTIFIER.match(text, at)) is None or found.group() not in variables:
            raise ValueError(f"expected an input at {text[at:at + 40]!r}")
        at = found.end()
        return Ref(variables[found.group()])

    def operand() -> Expr:
        nonlocal at
        if take("Double.valueOf("):
            number = literal()
            expect(")")
            return Number(number)
        if text.startswith("'", at):
            return Text(literal())
        for word, constant in (("true", Boolean(True)), ("false", Boolean(False)), ("null", Null())):
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
            return Op("u!", (node,))
        if take("("):
            left = operand()
            if take(" == null || "):
                operand()
                expect(" == [])")
                return Op("==", (left, Null()))
            return binary(left, _COMPARISONS)
        return reference()

    def binary(left: Expr, operators: tuple[str, ...]) -> Expr:
        for operator in operators:
            if take(operator):
                right = operand()
                expect(")")
                return Op(operator.strip(), (left, right))
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
            return Op("u-", (node,))
        for call in ("Math.min", "Math.max"):
            if text.startswith(f"{call}(", at):
                return Op(call, tuple(folded(call)))
        for spelling, call in ((f"{NF_ROUND_HELPER}(", "Math.round"), ("StrictMath.pow(", "Math.pow"),
                               *((f"{name}(", name) for name in ("Math.abs", "Math.sqrt", "Math.floor", "Math.ceil"))):
            if take(spelling):
                args = [operand()]
                while take(", "):
                    args.append(operand())
                expect(")")
                return Op(call, tuple(args))
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
    def numeric(expression: str, child: Op) -> str:
        return (
            f"{NF_FINITE_HELPER}({expression}, {groovy_literal(where)}, "
            f"{groovy_literal(source_text(child))}, {inputs})"
        )

    def go(child: Expr) -> str:
        match child:
            case Number(text=text):
                return f"Double.valueOf({groovy_literal(text)})"
            case Text(value=value):
                return groovy_literal(value)
            case Boolean() | Null():
                return source_text(child)
            case Ref(name=name):
                spelled = (variables or {}).get(name, name)
                return f"(({spelled}) as double)" if child in _numeric_refs else spelled
            case Op(op=op, args=operands):
                return operation(child, op, operands, [go(arg) for arg in operands])

    def operation(child: Op, op: str, operands: tuple[Expr, ...], args: list[str]) -> str:
        match op:
            case "u!":
                return f"(!{args[0]})"
            case "u-":
                return numeric(f"(-{args[0]})", child)
            case "u+":
                return args[0]
            case "&&" | "||" | "<" | "<=" | ">" | ">=":
                return f"({args[0]} {op} {args[1]})"
            case "==" | "===" | "!=" | "!==" if any(isinstance(arg, Null) for arg in operands):
                # An absent optional value arrives as the reserved [] sentinel
                # (design §6, Inputs and channels), so a null check tests for it.
                other = args[1] if isinstance(operands[0], Null) else args[0]
                absent = f"({other} == null || {other} == [])"
                return absent if op in {"==", "==="} else f"(!{absent})"
            case "==" | "===":
                return f"({args[0]} == {args[1]})"
            case "!=" | "!==":
                return f"({args[0]} != {args[1]})"
            case "+" | "-" | "*" | "/" | "%":
                return numeric(f"({args[0]} {op} {args[1]})", child)
            case "Math.round":
                return numeric(f"{NF_ROUND_HELPER}({args[0]})", child)
            case "Math.pow":
                return numeric(f"StrictMath.pow({', '.join(args)})", child)
            case "Math.min" | "Math.max":
                folded = args[0]
                for arg in args[1:]:
                    folded = f"{op}({folded}, {arg})"
                return numeric(folded, child)
            case call if call in CALLS:
                return numeric(f"{call}({', '.join(args)})", child)
            case _:
                raise ValueError(f"unsupported construct {op!r}")

    _numeric_refs = _numeric_ref_nodes(node)
    return go(node)


def _numeric_ref_nodes(node: Expr) -> set[Ref]:
    """References used as numbers: operands of arithmetic, comparison, or calls."""
    numeric_ops = {"u-", "u+", "<", "<=", ">", ">=", "+", "-", "*", "/", "%", *CALLS}
    found: set[Ref] = set()
    if isinstance(node, Op):
        if node.op in numeric_ops:
            found.update(arg for arg in node.args if isinstance(arg, Ref))
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
