from typing import Any

import yaml

# NOTE: In the following constructors, you CANNOT return the exact same yaml tag
# Otherwise, the loader is not idempotent. Specifically, then other tooling
# (i.e. the python api) cannot simply emit the dictionaries returned here,
# because then these constructors will fire again.

# Custom wic yaml tags (used when constructing yaml strings to feed back into wic_loader()).
TAG_ANCHOR = '!&'
TAG_ALIAS = '!*'
TAG_INLINE_INPUT = '!ii'

# The dict keys the tags above are rewritten to by the constructors below.
# (Deliberately different from the tags themselves; see NOTE above.)
KEY_ANCHOR = 'wic_anchor'
KEY_ALIAS = 'wic_alias'
KEY_INLINE_INPUT = 'wic_inline_input'


def anchor_constructor(loader: yaml.SafeLoader, node: yaml.nodes.ScalarNode) -> dict[str, Any]:
    """PyYAML constructor for the custom `!&` (wic anchor) tag."""
    val = loader.construct_scalar(node)
    return {KEY_ANCHOR: val}


def alias_constructor(loader: yaml.SafeLoader, node: yaml.nodes.ScalarNode) -> dict[str, Any]:
    """PyYAML constructor for the custom `!*` (wic alias) tag."""
    val = loader.construct_scalar(node)
    return {KEY_ALIAS: val}


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
    return {KEY_INLINE_INPUT: val}


def wic_loader() -> type[yaml.SafeLoader]:
    """Return a `yaml.SafeLoader` with the `!&`, `!*`, and `!ii` wic tags registered."""
    loader = yaml.SafeLoader
    loader.add_constructor(TAG_ANCHOR, anchor_constructor)
    loader.add_constructor(TAG_ALIAS, alias_constructor)
    loader.add_constructor(TAG_INLINE_INPUT, inlineinput_constructor)
    return loader
