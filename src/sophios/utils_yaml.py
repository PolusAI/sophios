"""The YAML tags Sophios owns, and the loader that understands them."""
from typing import Any, ClassVar, Final, final

import yaml

# NOTE: In the following constructors, you CANNOT return the exact same yaml tag
# Otherwise, the loader is not idempotent. Specifically, then other tooling
# (i.e. the python api) cannot simply emit the dictionaries returned here,
# because then these constructors will fire again.


@final
class Tag:  # pylint: disable=too-few-public-methods  # a namespace, not a type
    """The custom YAML tags Sophios owns.

    A namespace rather than loose module constants: these four are one
    vocabulary and are always reasoned about together, and grouping them keeps
    `TAG_` prefixes from being the only thing relating them. Class attributes
    are read-only in practice and shared safely across threads.
    """

    ANCHOR: Final = '!&'
    ALIAS: Final = '!*'
    INLINE_INPUT: Final = '!ii'
    RAW_CWL: Final = '!cwl'

    #: Every tag, for membership tests. Assigned below, from the members
    #: declared above, so adding or renaming one cannot leave it behind.
    ALL: ClassVar[frozenset[str]]


@final
class Key:  # pylint: disable=too-few-public-methods  # a namespace, not a type
    """The desugared spelling each tag is rewritten to.

    Deliberately different from the tags themselves; see the NOTE above. A
    constructor that re-emitted its own tag would fire again on reload, so the
    loader would not be idempotent.
    """

    ANCHOR: Final = 'wic_anchor'
    ALIAS: Final = 'wic_alias'
    INLINE_INPUT: Final = 'wic_inline_input'
    RAW_CWL: Final = 'wic_raw_cwl'

    #: Every desugared key, for membership tests. Derived, as above.
    ALL: ClassVar[frozenset[str]]


def _declared_spellings(vocabulary: type) -> frozenset[str]:
    """Every spelling declared on a vocabulary namespace.

    Derived rather than restated. A hand-written membership set is one edit
    away from being wrong, and the failure is quiet in the worst way: the
    parser reads these sets to decide which tags it owns, so a tag missing
    from `ALL` is diagnosed as unknown even though a constructor for it is
    registered right here.
    """
    return frozenset(value for name, value in vars(vocabulary).items()
                     if name.isupper() and isinstance(value, str))


Tag.ALL = _declared_spellings(Tag)
Key.ALL = _declared_spellings(Key)


def anchor_constructor(loader: yaml.SafeLoader, node: yaml.nodes.ScalarNode) -> dict[str, Any]:
    """PyYAML constructor for the custom `!&` (wic anchor) tag."""
    val = loader.construct_scalar(node)
    return {Key.ANCHOR: val}


def alias_constructor(loader: yaml.SafeLoader, node: yaml.nodes.ScalarNode) -> dict[str, Any]:
    """PyYAML constructor for the custom `!*` (wic alias) tag."""
    val = loader.construct_scalar(node)
    return {Key.ALIAS: val}


def inlineinput_constructor(loader: yaml.SafeLoader, node: yaml.nodes.Node) -> dict[str, dict[str, Any]]:
    """PyYAML constructor for the custom `!ii` (wic inline input) tag."""
    val: Any
    match node:
        case yaml.nodes.ScalarNode():
            try:
                # loader.construct_scalar always returns a string, whereas
                if node.value == "":
                    val = ""
                else:
                    val = yaml.safe_load(node.value)
                # yaml.safe_load returns the correct primitive types
            except Exception:
                # but fallback to a string if it is not actually a primitive type.
                val = loader.construct_scalar(node)
        case yaml.nodes.MappingNode():
            val = loader.construct_mapping(node)
        case yaml.nodes.SequenceNode():
            val = loader.construct_sequence(node)
        case _:
            raise TypeError(f'Unknown yaml node type! {node}')
    return {Key.INLINE_INPUT: val}


@final
class WicLoader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors  # SafeLoader's own depth
    """A `SafeLoader` that understands the Sophios tags.

    A subclass, not `yaml.SafeLoader` itself. Registering constructors on
    `SafeLoader` changes how *every* `yaml.safe_load` in the process behaves —
    including calls from unrelated libraries — for the rest of the program's
    life, and mutates a class dictionary that other threads may be reading. The
    subclass confines the tags to callers that ask for them.
    """


def rawcwl_constructor(loader: yaml.SafeLoader, node: yaml.nodes.ScalarNode) -> dict[str, Any]:
    """PyYAML constructor for the custom `!cwl` (raw CWL reference) tag.

    The expression is opaque to Sophios and handed to CWL unresolved; like the
    other three constructors, the tag desugars to a key so the loader stays
    idempotent (see NOTE above).
    """
    val = loader.construct_scalar(node)
    return {Key.RAW_CWL: val}


# Registered once, at import. Module import is serialised by the interpreter,
# so this happens exactly once no matter how many threads reach it, and the
# class is read-only afterwards.
WicLoader.add_constructor(Tag.ANCHOR, anchor_constructor)
WicLoader.add_constructor(Tag.ALIAS, alias_constructor)
WicLoader.add_constructor(Tag.INLINE_INPUT, inlineinput_constructor)
WicLoader.add_constructor(Tag.RAW_CWL, rawcwl_constructor)


def wic_loader() -> type[yaml.SafeLoader]:
    """Return the loader that understands the Sophios tags.

    Returns the same class every call. It carries no per-load state, so it is
    safe to share across threads and processes.
    """
    return WicLoader
