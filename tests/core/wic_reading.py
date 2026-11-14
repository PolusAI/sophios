"""What a `.wic` text means, and what YAML it is, for tests that read one back: `sophios.lang.parse` and `to_json` for
content, `yaml.SafeLoader` (`YamlWithSophiosTagsOpaque` for tagged text) for YAML's own verdict."""
from typing import Any

import yaml

from sophios.lang import parse, to_json
from sophios.utils_yaml import Tag


def read_wic(text: str, filename: str = 'written.wic') -> dict[str, Any]:
    """`text` as `parse` reads it, in the desugared spelling `to_json` writes; it must parse cleanly, or a claim about a
    valid document is vacuous."""
    result = parse(text, filename)
    assert not result.diagnostics.has_errors and result.document is not None, [str(d) for d in result.diagnostics]
    return to_json(result.document)


class YamlWithSophiosTagsOpaque(yaml.SafeLoader):  # pylint: disable=too-many-ancestors  # SafeLoader's own depth
    """`yaml.SafeLoader` with every Sophios tag transparent (a tagged node is built as its plain content), as an editor
    declaring the tags sees a `.wic`; what a tag means is `parse`'s to say."""


def _content(loader: YamlWithSophiosTagsOpaque, node: yaml.nodes.Node) -> Any:
    """A tagged node's plain YAML content: a scalar's text, a collection's content."""
    match node:
        case yaml.nodes.ScalarNode():
            return loader.construct_scalar(node)
        case yaml.nodes.MappingNode():
            return loader.construct_mapping(node, deep=True)
        case yaml.nodes.SequenceNode():
            return loader.construct_sequence(node, deep=True)
    raise TypeError(f'not a YAML node: {node!r}')


for _tag in Tag.ALL:
    YamlWithSophiosTagsOpaque.add_constructor(_tag, _content)
