"""The safe JavaScript subset: parse, type, and serialize.

The locked design (design_docs/sophios_nextflow_backend.md §6, Expressions)
admits one closed subset of CWL ``$( … )`` JavaScript. Every admitted
expression means the same under JavaScript and under each target that
renders it; nothing here evaluates JavaScript. Text outside the grammar or
the typing rules is rejected with a ValueError naming the construct. The
Nextflow target's Groovy rendering and reading are ``sophios.nextflow.expr``.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import chain
import math
import re
from typing import Any, Final, TypeAlias

from cwl_utils.errors import SubstitutionError
from cwl_utils.expression import scanner

NUMBER = "number"
BOOLEAN = "boolean"
STRING = "string"
NULL = "null"

#: Where a CWL expression starts: a parameter reference or a JavaScript expression, `$(`, or a function body, `${`.
EXPRESSION: Final = re.compile(r'\$[({]')

_SCALAR_TYPES = {"int": NUMBER, "float": NUMBER, "boolean": BOOLEAN, "string": STRING}
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
CALLS = {
    "Math.abs": 1, "Math.sqrt": 1, "Math.floor": 1, "Math.ceil": 1, "Math.round": 1,
    "Math.pow": 2, "Math.min": -2, "Math.max": -2,
}
# What each operator and call takes and gives; an equality is typed apart, as it alone admits null.
_SIGNATURES = {
    **dict.fromkeys(("u!", "&&", "||"), (BOOLEAN, BOOLEAN)),
    **dict.fromkeys(("<", "<=", ">", ">="), (NUMBER, BOOLEAN)),
    **dict.fromkeys(("u-", "u+", "+", "-", "*", "/", "%", *CALLS), (NUMBER, NUMBER)),
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
class Number:
    """A number literal, kept as written: the text is checked to be a finite strict-mode decimal."""

    text: str

    def to_dict(self) -> dict[str, object]:
        """The node as JSON."""
        return {"op": "number", "value": self.text}


@dataclass(frozen=True, slots=True)
class Text:
    """A string literal."""

    value: str

    def to_dict(self) -> dict[str, object]:
        """The node as JSON."""
        return {"op": "string", "value": self.value}


@dataclass(frozen=True, slots=True)
class Boolean:
    """``true`` or ``false``."""

    value: bool

    def to_dict(self) -> dict[str, object]:
        """The node as JSON."""
        return {"op": "boolean", "value": self.value}


@dataclass(frozen=True, slots=True)
class Null:
    """``null``."""

    def to_dict(self) -> dict[str, object]:
        """The node as JSON."""
        return {"op": "null"}


@dataclass(frozen=True, slots=True)
class Ref:
    """``inputs.<name>``."""

    name: str

    def to_dict(self) -> dict[str, object]:
        """The node as JSON."""
        return {"op": "ref", "value": self.name}


@dataclass(frozen=True, slots=True)
class Op:
    """An operator or a call applied to its arguments: ``op`` is the operator, or ``u`` and the operator of a
    unary one, or the call's name."""

    op: str
    args: tuple["Expr", ...] = ()

    def to_dict(self) -> dict[str, object]:
        """The node and its arguments as JSON."""
        item: dict[str, object] = {"op": self.op}
        if self.args:
            item["args"] = [arg.to_dict() for arg in self.args]
        return item


#: One typed node of the subset.
Expr: TypeAlias = Number | Text | Boolean | Null | Ref | Op


def is_safe_subset_text(value: object) -> bool:
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
            node = Op(current[1], (node, self.binary(level + 1)))
        return node

    def unary(self) -> Expr:
        if (current := self.peek()) is not None and current[0] == "op" and current[1] in _UNARY:
            self.index += 1
            return Op(f"u{current[1]}", (self.unary(),))
        return self.primary()

    def primary(self) -> Expr:
        current = self.peek()
        if current is None:
            raise ValueError("expression ends early")
        kind, text = current
        self.index += 1
        match kind:
            case "number":
                return Number(text)
            case "string":
                return Text(text[1:-1])
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
                return Boolean(text == "true")
            case ["null"]:
                return Null()
            case ["inputs", name]:
                return Ref(name)
            case _ if text in CALLS:
                self.expect("(")
                args = [self.binary(0)]
                while self.take(","):
                    args.append(self.binary(0))
                self.expect(")")
                return Op(text, tuple(args))
        raise ValueError(f"unsupported construct {text!r}")


def parse(text: str) -> Expr:
    """Parse one whole-field ``$( … )`` reference into an untyped tree."""
    match = _WHOLE_FIELD.match(text.strip())
    if match is None:
        raise ValueError("the safe JavaScript subset requires the whole field to be one $( … ) reference")
    return _Parser(match.group("body")).parse()


def _number_literal(text: str) -> None:
    """Reject a number literal that is not a finite strict-mode decimal."""
    if _NUMBER.fullmatch(text) is None:
        raise ValueError(f"number literal {text!r} is not a strict-mode decimal")
    if not math.isfinite(float(text)):
        raise ValueError(f"number literal {text} is not finite")


def _call_arity(call: str, args: tuple[Expr, ...]) -> None:
    """Reject a call given a number of arguments its function does not take."""
    arity = CALLS[call]
    if (arity > 0 and len(args) != arity) or (arity < 0 and len(args) < -arity):
        raise ValueError(f"{call} takes {abs(arity)}{' or more' if arity < 0 else ''} arguments")


_LITERAL_TYPES = {Text: STRING, Boolean: BOOLEAN, Null: NULL}


def check(node: Expr, input_types: Mapping[str, object]) -> str:
    """Return the node's subset type, or raise naming the ill-typed construct.

    ``input_types`` maps each visible input to its CWL type. An optional
    input may appear only beside ``null`` in an equality, because
    JavaScript arithmetic would coerce its absence to 0.
    """
    match node:
        case Number(text=text):
            _number_literal(text)
            return NUMBER
        case Text() | Boolean() | Null():
            return _LITERAL_TYPES[type(node)]
        case Ref(name=name):
            declared = input_types.get(name)
            if name not in input_types:
                raise ValueError(f"inputs.{name} is not an input of this tool")
            if _optional_scalar(declared) is not None:
                raise ValueError(f"optional inputs.{name} may only be compared with null")
            if not isinstance(declared, str) or declared not in _SCALAR_TYPES:
                raise ValueError(f"inputs.{name} has type {declared!r}; only scalar inputs are admitted")
            return _SCALAR_TYPES[declared]
        case Op(op="==" | "!=" | "===" | "!==" as op, args=args):
            left, right = (_equality_operand(child, input_types) for child in args)
            if NULL in (left, right):
                if {left, right} != {NULL, "optional"}:
                    raise ValueError(f"{op} with null requires an optional input on the other side")
                return BOOLEAN
            if left != right or "optional" in (left, right):
                raise ValueError(f"{op} requires two operands of the same type, not {left} and {right}")
            return BOOLEAN
        case Op(op=op, args=args) if op in _SIGNATURES:
            if op in CALLS:
                _call_arity(op, args)
            expected, result = _SIGNATURES[op]
            for child in args:
                if (actual := check(child, input_types)) != expected:
                    raise ValueError(f"{op.removeprefix('u')} requires {expected} operands, not {actual}")
            return result
        case Op(op=op):
            raise ValueError(f"unsupported construct {op!r}")


def _optional_scalar(declared: object) -> str | None:
    match declared:
        case list() as members if "null" in members and len(members) == 2:
            inner = next(member for member in members if member != "null")
            return inner if isinstance(inner, str) and inner in _SCALAR_TYPES else None
        case str() as text if text.endswith("?") and text[:-1] in _SCALAR_TYPES:
            return text[:-1]
    return None


def _equality_operand(node: Expr, input_types: Mapping[str, object]) -> str:
    if isinstance(node, Ref) and _optional_scalar(input_types.get(node.name)) is not None:
        return "optional"
    return check(node, input_types)


def validate(node: Expr) -> None:
    """Reject a tree the closed grammar would not have built.

    A tree can be built without parsing, as the reader builds one from its
    Groovy, and the renderer writes a call's ``op`` into the script, so a
    process re-checks each operator against the closed set, each arity, and
    each literal.
    """
    match node:
        case Number(text=text):
            _number_literal(text)
        case Text(value=value):
            if not re.fullmatch(r"[ -~]*", value) or "\\" in value:
                raise ValueError(f"string literal {value!r} is outside the admitted alphabet")
        case Boolean(value=bool()) | Null():
            pass
        case Boolean(value=value):
            raise ValueError(f"a boolean literal must be true or false, not {value!r}")
        case Ref(name=name):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"input reference {name!r} is not an identifier")
        case Op(op="u!" | "u-" | "u+" as op, args=args):
            if len(args) != 1:
                raise ValueError(f"{op[1]} takes one operand")
        case Op(op=op, args=args) if op in _BINARY:
            if len(args) != 2:
                raise ValueError(f"{op} takes two operands")
        case Op(op=call, args=args) if call in CALLS:
            _call_arity(call, args)
        case Op(op=op):
            raise ValueError(f"unsupported construct {op!r}")
    if isinstance(node, Op):
        for arg in node.args:
            validate(arg)


def references(node: Expr) -> set[str]:
    """Every input name the tree reads."""
    match node:
        case Ref(name=name):
            return {name}
        case Op(args=args):
            return {name for arg in args for name in references(arg)}
    return set()


_SPELLINGS = {"===": "==", "!==": "!="}


def canonical(node: Expr) -> Expr:
    """The one tree among those the Groovy rendering writes alike (design §5.2).

    With operands typed as the subset types them, ``===`` and ``!==`` mean
    ``==`` and ``!=``, a unary ``+`` changes no number, ``null`` sits on the
    right of an equality, and ``!(x == null)`` is ``x != null``.
    """
    match node:
        case Op(op=named, args=children):
            args = tuple(canonical(arg) for arg in children)
            op = _SPELLINGS.get(named, named)
            if op == "u+":
                return args[0]
            if op in ("==", "!=") and isinstance(args[0], Null):
                args = (args[1], args[0])
            if op == "u!" and isinstance(args[0], Op) and args[0].op == "==" and isinstance(args[0].args[1], Null):
                return Op("!=", args[0].args)
            return Op(op, args)
    return node


def source_text(node: Expr) -> str:
    """Canonical JavaScript text of a node, used only in diagnostics."""
    match node:
        case Number(text=text):
            return text
        case Text(value=value):
            return repr(value)
        case Boolean(value=value):
            return "true" if value else "false"
        case Null():
            return "null"
        case Ref(name=name):
            return f"inputs.{name}"
        case Op(op=op, args=args) if op.startswith("u"):
            # A unary operand of a unary is parenthesized: `--x` is a decrement.
            operand = source_text(args[0])
            return f"{op[1]}({operand})" if isinstance(args[0], Op) and args[0].op.startswith("u") \
                else f"{op[1]}{operand}"
        case Op(op=op, args=args) if op in CALLS:
            return f"{op}({', '.join(source_text(arg) for arg in args)})"
        case Op(op=op, args=args):
            return f"({source_text(args[0])} {op} {source_text(args[1])})"


def written_text(node: Expr) -> str:
    """The `$( … )` field a tree is written as: its operators spaced, a string in double quotes, and a parenthesis
    only where the grammar's precedence needs one."""
    return f"$({_body(node, -1)})"


def _body(node: Expr, outer: int) -> str:
    """The text of `node` inside an operator of precedence `outer`: a level of `_BINARY_LEVELS`, or -1 for none, so a
    node of the same level is a right operand and takes a parenthesis (every level is left-associative)."""
    match node:
        case Number(text=text):
            return text
        case Text(value=value):
            return f"'{value}'" if '"' in value else f'"{value}"'
        case Boolean(value=value):
            return "true" if value else "false"
        case Null():
            return "null"
        case Ref(name=name):
            return f"inputs.{name}"
        case Op(op=op, args=(operand,)) if op.startswith("u"):
            # A unary operand of a unary is parenthesized: `--x` is a decrement.
            inner = _body(operand, len(_BINARY_LEVELS))
            return f"{op[1]}({inner})" if isinstance(operand, Op) and operand.op.startswith("u") else f"{op[1]}{inner}"
        case Op(op=op, args=args) if op in CALLS:
            return f"{op}({', '.join(_body(arg, -1) for arg in args)})"
        case Op(op=op, args=(left, right)):
            level = next(index for index, ops in enumerate(_BINARY_LEVELS) if op in ops)
            text = f"{_body(left, level - 1)} {op} {_body(right, level)}"
            return f"({text})" if level <= outer else text
    raise TypeError(f"not a node of the subset: {node!r}")


def boolean_problem(written: object, type_: str | None) -> str | None:
    """Why a `when` written `written`, of the subset's type `type_`, cannot give `true` or `false`, which CWL
    requires of one, the `when` quoted first; `None` when it gives one, and when nothing here can tell: the subset
    cannot parse or type it, or it is not a string, which cwltool's schema validation refuses itself."""
    if isinstance(written, str) and one_expression(written) is None:
        return f'{written!r} is not one $(...) or ${{...}} expression'
    if type_ not in (None, BOOLEAN):
        return f'{written!r} must compute a boolean, not a {type_}'
    return None


def one_expression(field: str) -> str | None:
    """How `field` opens, `$(` or `${`, when it is one CWL expression from end to end; else None.

    Read as cwltool reads an expression field: stripped of surrounding whitespace, then scanned by
    `cwl_utils.expression.scanner`, as `interpolate` in cwl_utils/expression.py scans it. Anything else is a template,
    text with or without expressions in it, which evaluates to text.
    """
    text = field.strip()
    try:
        span = scanner(text)
    except SubstitutionError:
        return None
    return text[:2] if span == (0, len(text)) and text[:2] in ('$(', '${') else None


def strings(value: Any) -> Iterator[str]:
    """Every string in `value`: itself, or the strings at any depth of a list or of a mapping's values."""
    match value:
        case str():
            yield value
        case list():
            yield from chain.from_iterable(map(strings, value))
        case Mapping():
            yield from strings(list(value.values()))


#: An input a CWL expression reads, `inputs.name` or `inputs["name"]`: `inputs.name-1` is `inputs.name` minus 1.
_INPUT_READ: Final = re.compile(r'\binputs(?:\.([A-Za-z_]\w*)|\[\s*["\']([^"\']+)["\']\s*\])')


def inputs_read(value: Any) -> tuple[str, ...]:
    """Every input the CWL expressions in `value` (a string, or the strings at any depth of a list or mapping) read,
    in order of first mention."""
    return tuple(dict.fromkeys(a or b for text in strings(value) for a, b in _INPUT_READ.findall(text)))


@dataclass(frozen=True, slots=True)
class Guard:
    """A step's condition: its tree when the safe subset reads the text, the inputs the text reads, and, where there
    is no tree, why, in the subset's words. `text` is what the author wrote, which only a diagnostic quotes: the text
    the subset does not read rides unread in the step's `passthrough`, and the tree is what a target writes."""

    expr: Expr | None
    reads: tuple[str, ...]
    problem: str | None = None
    text: Any = None


def read_guard(written: object) -> Guard:
    """A step's `when` read: its tree where the safe subset reads the text, the inputs the text reads, and where there
    is no tree, why, in the subset's words."""
    reads = inputs_read(written)
    if not isinstance(written, str):
        return Guard(None, reads, 'a when is a string', written)
    if not is_safe_subset_text(written):
        return Guard(None, reads, 'the safe JavaScript subset requires the whole field to be one $( … ) reference',
                     written)
    try:
        return Guard(parse(written), reads, None, written)
    except ValueError as error:
        return Guard(None, reads, str(error), written)
