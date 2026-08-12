"""Nextflow symbol rules derived from the pinned Nextflow language grammar."""

from unicodedata import category


NEXTFLOW_GRAMMAR_VERSION = "26.04.2"

# Sources pinned to the runtime version used by this backend:
# - modules/nf-lang/src/main/antlr/ScriptLexer.g4, JavaLetter and
#   JavaLetterOrDigit
# - modules/nf-lang/src/main/antlr/ScriptParser.g4, identifier
# - modules/nf-lang/src/main/java/nextflow/script/parser/ScriptAstBuilder.java,
#   checkInvalidVarName and GROOVY_KEYWORDS
_NEXTFLOW_GRAMMAR_ROOT = (
    "https://github.com/nextflow-io/nextflow/blob/v26.04.2/"
    "modules/nf-lang/src/main"
)
NEXTFLOW_LEXER_SOURCE = f"{_NEXTFLOW_GRAMMAR_ROOT}/antlr/ScriptLexer.g4"
NEXTFLOW_PARSER_SOURCE = f"{_NEXTFLOW_GRAMMAR_ROOT}/antlr/ScriptParser.g4"
NEXTFLOW_SEMANTICS_SOURCE = (
    f"{_NEXTFLOW_GRAMMAR_ROOT}/java/nextflow/script/parser/ScriptAstBuilder.java"
)

# ScriptParser.identifier explicitly admits these keyword tokens as symbols.
NEXTFLOW_IDENTIFIER_KEYWORDS = frozenset(
    {
        "in",
        "nextflow",
        "params",
        "from",
        "record",
        "process",
        "exec",
        "input",
        "output",
        "script",
        "shell",
        "stage",
        "stub",
        "topic",
        "tuple",
        "when",
        "workflow",
        "emit",
        "main",
        "onComplete",
        "onError",
        "publish",
        "take",
    }
)

# Word-shaped tokens declared before Identifier in ScriptLexer.
_NEXTFLOW_LEXER_WORDS = frozenset(
    {
        "as",
        "def",
        "in",
        "assert",
        "boolean",
        "byte",
        "catch",
        "char",
        "double",
        "else",
        "enum",
        "float",
        "if",
        "import",
        "instanceof",
        "int",
        "long",
        "new",
        "record",
        "return",
        "short",
        "throw",
        "try",
        "nextflow",
        "params",
        "include",
        "from",
        "process",
        "exec",
        "input",
        "output",
        "script",
        "shell",
        "stage",
        "stub",
        "topic",
        "tuple",
        "when",
        "workflow",
        "emit",
        "main",
        "onComplete",
        "onError",
        "publish",
        "take",
    }
)

# ScriptAstBuilder rejects these when a symbol is used as a variable/callable.
_GROOVY_KEYWORDS = frozenset(
    {
        "abstract",
        "assert",
        "break",
        "case",
        "class",
        "const",
        "continue",
        "default",
        "do",
        "extends",
        "final",
        "finally",
        "for",
        "goto",
        "implements",
        "interface",
        "native",
        "package",
        "private",
        "protected",
        "public",
        "static",
        "super",
        "switch",
        "synchronized",
        "this",
        "throws",
        "transient",
        "void",
        "volatile",
        "while",
    }
)

NEXTFLOW_RESERVED_WORDS = (
    _NEXTFLOW_LEXER_WORDS
    | _GROOVY_KEYWORDS
    | {"true", "false", "null", "_"}
) - NEXTFLOW_IDENTIFIER_KEYWORDS

# ScriptLexer.JavaLetter delegates to Character.isJavaIdentifierStart and
# excludes identifier-ignorable characters. These Unicode general categories
# are the corresponding Java identifier categories.
_JAVA_LETTER_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo", "Nl", "Sc", "Pc"})
_JAVA_LETTER_OR_DIGIT_CATEGORIES = _JAVA_LETTER_CATEGORIES | {"Nd", "Mc", "Mn"}


def _is_java_letter(character: str) -> bool:
    return category(character) in _JAVA_LETTER_CATEGORIES


def _is_java_letter_or_digit(character: str) -> bool:
    return category(character) in _JAVA_LETTER_OR_DIGIT_CATEGORIES


def is_nextflow_identifier(value: object) -> bool:
    """Return whether a value is a callable symbol in the Nextflow grammar."""
    match value:
        case str() as identifier if identifier and identifier not in NEXTFLOW_RESERVED_WORDS:
            return _is_java_letter(identifier[0]) and all(
                _is_java_letter_or_digit(character) for character in identifier[1:]
            )
        case _:
            return False


def validate_nextflow_identifier(value: object, *, field_name: str) -> str:
    """Return a valid Nextflow symbol or raise a grammar-specific error."""
    match value:
        case str() as identifier if is_nextflow_identifier(identifier):
            return identifier
        case _:
            raise ValueError(
                f"{field_name} must be a valid Nextflow identifier per the "
                f"Nextflow {NEXTFLOW_GRAMMAR_VERSION} grammar, got {value!r}"
            )


def normalize_nextflow_identifier(value: object) -> str:
    """Normalize text to a callable identifier using Nextflow grammar symbols."""
    match value:
        case str() as source if source:
            pass
        case _:
            raise ValueError("Nextflow identifier source must be a non-empty string")

    identifier = "".join(
        character if _is_java_letter_or_digit(character) else "_"
        for character in source
    )
    if not _is_java_letter(identifier[0]):
        identifier = f"_{identifier}"
    if identifier in NEXTFLOW_RESERVED_WORDS:
        identifier = f"_{identifier}"
    return validate_nextflow_identifier(identifier, field_name="normalized identifier")
