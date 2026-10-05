"""The safe JavaScript subset: parse, type, serialize, and render to Groovy.

The locked design (§6, Expressions) admits one closed subset of CWL
``$( … )`` JavaScript. Every admitted expression means the same under
JavaScript and under the fixed Groovy rendering below; nothing here
evaluates JavaScript. Text outside the grammar or the typing rules is
rejected with a ValueError naming the construct.
"""

from dataclasses import dataclass
import math
import re
from typing import Any, Mapping, Sequence

NF_FINITE_HELPER = "__sophios_finite_9f72e"
NF_ROUND_HELPER = "__sophios_round_9f72e"
NF_NUMBER_TEXT_HELPER = "__sophios_number_text_9f72e"

NUMBER = "number"
BOOLEAN = "boolean"
STRING = "string"
NULL = "null"

_NUMERIC_TYPES = frozenset({"int", "long", "float", "double"})
_SCALAR_TYPES = {**{name: NUMBER for name in _NUMERIC_TYPES}, "boolean": BOOLEAN, "string": STRING}
_UNARY = frozenset({"!", "-", "+"})
# JavaScript precedence, loosest first; every level is left-associative.
_BINARY_LEVELS: tuple[frozenset[str], ...] = (
    frozenset({"||"}),
    frozenset({"&&"}),
    frozenset({"==", "!=", "===", "!=="}),
    frozenset({"<", "<=", ">", ">="}),
    frozenset({"+", "-"}),
    frozenset({"*", "/", "%"}),
)
_BINARY = frozenset(op for level in _BINARY_LEVELS for op in level)
_CALLS = {
    "Math.abs": 1, "Math.sqrt": 1, "Math.floor": 1, "Math.ceil": 1, "Math.round": 1,
    "Math.pow": 2, "Math.min": -2, "Math.max": -2,
}
_WHOLE_FIELD = re.compile(r"^\$\((?P<body>.*)\)$", re.DOTALL)
# JavaScript strict mode rejects a leading zero on an integer (octal). ASCII
# digits only: Python's float() accepts U+0663, and Double.valueOf does not.
# The exponent is part of the literal, so 1e999 is one non-finite number
# rather than the number 1 followed by an identifier.
_NUMBER_BODY = r"(?:0|[1-9][0-9]*)(?:\.[0-9]*)?|\.[0-9]+"
_NUMBER = re.compile(rf"(?:{_NUMBER_BODY})(?:[eE][+-]?[0-9]+)?")
_TOKEN = re.compile(
    r"\s*(?:"
    rf"(?P<number>0[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?|(?:{_NUMBER_BODY})(?:[eE][+-]?[0-9]+)?)"
    r"|(?P<string>\"[ !#-\[\]-~]*\"|'[ -&(-\[\]-~]*')"
    r"|(?P<name>[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*)"
    # ++ and -- lex as JavaScript's increment and decrement, never as two signs.
    r"|(?P<op>===|!==|==|!=|<=|>=|&&|\|\||\*\*|\+\+|--|[-+*/%<>!(),?:&|^~=\[\]{}`;])"
    r")"
)


@dataclass(frozen=True, slots=True)
class Expr:
    """One typed node: ``op`` is a literal tag, ``ref``, an operator, or a call."""

    op: str
    args: tuple["Expr", ...] = ()
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize without a ``kind`` key, so schema gates never mistake a node for a token."""
        item: dict[str, Any] = {"op": self.op}
        if self.args:
            item["args"] = [arg.to_dict() for arg in self.args]
        if self.op in {"number", "string", "boolean", "ref"}:
            item["value"] = self.value
        return item

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Expr":
        """Hydrate one node; the tree is re-typed by its owning token."""
        return cls(
            str(value["op"]),
            tuple(cls.from_dict(arg) for arg in value.get("args", ())),
            value.get("value"),
        )


def is_safe_subset_text(value: Any) -> bool:
    """Whether a field is one whole ``$( … )`` reference the subset parser should own.

    A body whose parentheses are unbalanced is a template that happens to
    start with ``$(`` and end with ``)``, such as ``$(inputs.a)-$(inputs.b)``.
    That stays a projection template.
    """
    if not isinstance(value, str) or (match := _WHOLE_FIELD.match(value.strip())) is None:
        return False
    return _parentheses_balance(match.group("body"))


def _parentheses_balance(body: str) -> bool:
    depth = 0
    for character in body:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = self._lex(text)
        self.index = 0

    @staticmethod
    def _lex(text: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(text):
            if text[position:].strip() == "":
                break
            match = _TOKEN.match(text, position)
            if match is None or match.end() == position:
                raise ValueError(f"unsupported character {text[position:].lstrip()[:1]!r}")
            kind = match.lastgroup or ""
            tokens.append((kind, match.group(kind)))
            position = match.end()
        return tokens

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self, text: str) -> bool:
        if (current := self.peek()) is not None and current[0] == "op" and current[1] == text:
            self.index += 1
            return True
        return False

    def expect(self, text: str) -> None:
        if not self.take(text):
            raise ValueError(f"expected {text!r}")

    def parse(self) -> Expr:
        node = self.binary(0)
        if (current := self.peek()) is not None:
            raise ValueError(f"unsupported construct {current[1]!r}")
        return node

    def binary(self, level: int) -> Expr:
        if level == len(_BINARY_LEVELS):
            return self.unary()
        node = self.binary(level + 1)
        while (current := self.peek()) is not None and current[0] == "op" and current[1] in _BINARY_LEVELS[level]:
            self.index += 1
            node = Expr(current[1], (node, self.binary(level + 1)))
        return node

    def unary(self) -> Expr:
        if (current := self.peek()) is not None and current[0] == "op" and current[1] in _UNARY:
            self.index += 1
            return Expr(f"u{current[1]}", (self.unary(),))
        return self.primary()

    def primary(self) -> Expr:
        current = self.peek()
        if current is None:
            raise ValueError("expression ends early")
        kind, text = current
        self.index += 1
        match kind:
            case "number":
                return Expr("number", value=text)
            case "string":
                return Expr("string", value=text[1:-1])
            case "name":
                return self.name(text)
            case "op" if text == "(":
                node = self.binary(0)
                self.expect(")")
                return node
        raise ValueError(f"unsupported construct {text!r}")

    def name(self, text: str) -> Expr:
        match text.split("."):
            case ["true"] | ["false"]:
                return Expr("boolean", value=text == "true")
            case ["null"]:
                return Expr("null")
            case ["inputs", name]:
                return Expr("ref", value=name)
            case _ if text in _CALLS:
                self.expect("(")
                args = [self.binary(0)]
                while self.take(","):
                    args.append(self.binary(0))
                self.expect(")")
                return Expr(text, tuple(args))
        raise ValueError(f"unsupported construct {text!r}")


def parse(text: str) -> Expr:
    """Parse one whole-field ``$( … )`` reference into an untyped tree."""
    match = _WHOLE_FIELD.match(text.strip())
    if match is None:
        raise ValueError("the safe JavaScript subset requires the whole field to be one $( … ) reference")
    return _Parser(match.group("body")).parse()


def check(node: Expr, input_types: Mapping[str, Any]) -> str:
    """Return the node's subset type, or raise naming the ill-typed construct.

    ``input_types`` maps each visible input to its CWL type. An optional
    input may appear only beside ``null`` in an equality, because
    JavaScript arithmetic would coerce its absence to 0.
    """
    def operand(child: Expr) -> str:
        return check(child, input_types)

    match node.op:
        case "number":
            if not isinstance(node.value, str) or _NUMBER.fullmatch(node.value) is None:
                raise ValueError(f"number literal {node.value!r} is not a strict-mode decimal")
            try:
                parsed = float(node.value)
            except ValueError as exc:
                raise ValueError(f"number literal {node.value} is not finite") from exc
            if not math.isfinite(parsed):
                raise ValueError(f"number literal {node.value} is not finite")
            return NUMBER
        case "string" | "boolean" | "null":
            return {"string": STRING, "boolean": BOOLEAN, "null": NULL}[node.op]
        case "ref":
            declared = input_types.get(node.value)
            if node.value not in input_types:
                raise ValueError(f"inputs.{node.value} is not an input of this tool")
            if _optional_scalar(declared) is not None:
                raise ValueError(f"optional inputs.{node.value} may only be compared with null")
            if not isinstance(declared, str) or declared not in _SCALAR_TYPES:
                raise ValueError(f"inputs.{node.value} has type {declared!r}; only scalar inputs are admitted")
            return _SCALAR_TYPES[declared]
        case "u!":
            return _require(BOOLEAN, operand(node.args[0]), "!")
        case "u-" | "u+":
            return _require(NUMBER, operand(node.args[0]), node.op[1])
        case "&&" | "||":
            for child in node.args:
                _require(BOOLEAN, operand(child), node.op)
            return BOOLEAN
        case "<" | "<=" | ">" | ">=":
            for child in node.args:
                _require(NUMBER, operand(child), node.op)
            return BOOLEAN
        case "==" | "!=" | "===" | "!==":
            left, right = (_equality_operand(child, input_types) for child in node.args)
            if NULL in (left, right):
                if {left, right} != {NULL, "optional"}:
                    raise ValueError(f"{node.op} with null requires an optional input on the other side")
                return BOOLEAN
            if left != right or "optional" in (left, right):
                raise ValueError(f"{node.op} requires two operands of the same type, not {left} and {right}")
            return BOOLEAN
        case "+" | "-" | "*" | "/" | "%":
            for child in node.args:
                _require(NUMBER, operand(child), node.op)
            return NUMBER
        case call if call in _CALLS:
            arity = _CALLS[call]
            if (arity > 0 and len(node.args) != arity) or (arity < 0 and len(node.args) < -arity):
                raise ValueError(f"{call} takes {abs(arity)}{' or more' if arity < 0 else ''} arguments")
            for child in node.args:
                _require(NUMBER, operand(child), call)
            return NUMBER
    raise ValueError(f"unsupported construct {node.op!r}")


def _optional_scalar(declared: Any) -> str | None:
    match declared:
        case list() as members if "null" in members and len(members) == 2:
            inner = next(member for member in members if member != "null")
            return inner if isinstance(inner, str) and inner in _SCALAR_TYPES else None
        case str() as text if text.endswith("?") and text[:-1] in _SCALAR_TYPES:
            return text[:-1]
    return None


def _equality_operand(node: Expr, input_types: Mapping[str, Any]) -> str:
    if node.op == "ref" and _optional_scalar(input_types.get(node.value)) is not None:
        return "optional"
    return check(node, input_types)


def _require(expected: str, actual: str, construct: str) -> str:
    if actual != expected:
        raise ValueError(f"{construct} requires {expected} operands, not {actual}")
    return expected


def validate(node: Expr) -> None:
    """Reject a hydrated tree the closed grammar would not have built.

    ``from_dict`` rebuilds nodes without parsing, and the renderer writes a
    call's ``op`` into the script. Hydration therefore re-checks each operator
    against the closed set, each arity, and each literal.
    """
    match node.op:
        case "number":
            if not isinstance(node.value, str) or _NUMBER.fullmatch(node.value) is None:
                raise ValueError(f"number literal {node.value!r} is not a strict-mode decimal")
            try:
                parsed = float(node.value)
            except ValueError as exc:
                raise ValueError(f"number literal {node.value} is not finite") from exc
            if not math.isfinite(parsed):
                raise ValueError(f"number literal {node.value} is not finite")
            if node.args:
                raise ValueError("a number literal takes no arguments")
        case "string":
            if not isinstance(node.value, str) or not re.fullmatch(r"[ -~]*", node.value) or "\\" in node.value:
                raise ValueError(f"string literal {node.value!r} is outside the admitted alphabet")
            if node.args:
                raise ValueError("a string literal takes no arguments")
        case "boolean":
            if not isinstance(node.value, bool):
                raise ValueError("a boolean literal must be true or false")
            if node.args:
                raise ValueError("a boolean literal takes no arguments")
        case "null":
            if node.args or node.value is not None:
                raise ValueError("null takes no arguments and no value")
        case "ref":
            if not isinstance(node.value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", node.value):
                raise ValueError(f"input reference {node.value!r} is not an identifier")
            if node.args:
                raise ValueError("an input reference takes no arguments")
        case "u!" | "u-" | "u+":
            if len(node.args) != 1:
                raise ValueError(f"{node.op[1]} takes one operand")
        case op if op in _BINARY:
            if len(node.args) != 2:
                raise ValueError(f"{op} takes two operands")
        case call if call in _CALLS:
            arity = _CALLS[call]
            if (arity > 0 and len(node.args) != arity) or (arity < 0 and len(node.args) < -arity):
                raise ValueError(f"{call} takes {abs(arity)}{' or more' if arity < 0 else ''} arguments")
        case _:
            raise ValueError(f"unsupported construct {node.op!r}")
    for arg in node.args:
        validate(arg)


def references(node: Expr) -> set[str]:
    """Every input name the tree reads."""
    found = {node.value} if node.op == "ref" else set()
    for arg in node.args:
        found |= references(arg)
    return found


def source_text(node: Expr) -> str:
    """Canonical JavaScript text of a node, used only in diagnostics."""
    match node.op:
        case "number":
            return str(node.value)
        case "string":
            return repr(node.value)
        case "boolean":
            return "true" if node.value else "false"
        case "null":
            return "null"
        case "ref":
            return f"inputs.{node.value}"
        case op if op.startswith("u"):
            return f"{op[1]}{source_text(node.args[0])}"
        case op if op in _CALLS:
            return f"{op}({', '.join(source_text(arg) for arg in node.args)})"
    return f"({source_text(node.args[0])} {node.op} {source_text(node.args[1])})"


CONTROL_ESCAPES = {code: f"\\u{code:04x}" for code in (*range(32), 127)}
_GROOVY_LITERAL_TABLE = {ord("\\"): "\\\\", ord("'"): "\\'", **CONTROL_ESCAPES}


def groovy_literal(value: str) -> str:
    """``value`` as a single-quoted Groovy string: the one escaper for that literal."""
    return f"'{value.translate(_GROOVY_LITERAL_TABLE)}'"


def render_groovy(node: Expr, *, where: str, inputs: str) -> str:
    """Render the fixed Groovy idiom for a typed tree.

    Every numeric node passes through the finite check, so NaN or ±Infinity
    anywhere fails with a diagnostic naming ``where``, the subexpression,
    and the ``inputs`` map expression.
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
                return f"(({child.value}) as double)" if child in _numeric_refs else str(child.value)
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
            case call if call in _CALLS:
                return numeric(f"{call}({', '.join(args)})", child)
            case _:
                raise ValueError(f"unsupported construct {child.op!r}")

    _numeric_refs = _numeric_ref_nodes(node)
    return go(node)


def _numeric_ref_nodes(node: Expr) -> set[Expr]:
    """References used as numbers: operands of arithmetic, comparison, or calls."""
    numeric_ops = {"u-", "u+", "<", "<=", ">", ">=", "+", "-", "*", "/", "%", *_CALLS}
    found: set[Expr] = set()
    if node.op in numeric_ops:
        found.update(arg for arg in node.args if arg.op == "ref")
    for arg in node.args:
        found |= _numeric_ref_nodes(arg)
    return found


def referenced_names(nodes: Sequence[Expr]) -> set[str]:
    """Every input name read by any of ``nodes``."""
    found: set[str] = set()
    for node in nodes:
        found |= references(node)
    return found


NF_EXPRESSION_FUNCTIONS = f'''def {NF_FINITE_HELPER}(double value, String where, String part, Map inputs) {{
    if( Double.isNaN(value) || Double.isInfinite(value) )
        throw new IllegalStateException("Sophios expression " + where + ": " + part \
+ " is " + value + " with inputs " + inputs)
    // + 0.0d turns -0.0 into 0.0: JavaScript never tells them apart, Groovy == does.
    return value + 0.0d
}}
def {NF_ROUND_HELPER}(double value) {{
    double floor = Math.floor(value)
    return value - floor >= 0.5d ? floor + 1.0d : floor
}}
def {NF_NUMBER_TEXT_HELPER}(double value, boolean integral, String where, Map inputs) {{
    if( value == Math.rint(value) && Math.abs(value) < 1.0e21d )
        return new BigDecimal(value).toBigInteger().toString()
    if( integral )
        throw new IllegalStateException("Sophios expression " + where + ": result " + value \
+ " is not integral but binds an integer port, with inputs " + inputs)
    double magnitude = Math.abs(value)
    if( magnitude < 1.0e-4d || magnitude >= 1.0e16d )
        throw new IllegalStateException("Sophios expression " + where + ": result " + value \
+ " needs exponent notation, which is not supported, with inputs " + inputs)
    return new BigDecimal(Double.toString(value)).stripTrailingZeros().toPlainString()
}}'''
