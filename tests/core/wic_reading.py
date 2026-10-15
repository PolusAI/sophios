"""What a `.wic` text means, and what YAML it is, for tests that read one back.

`sophios.lang.parse` is the one reader of `.wic`, so a test that needs a
written document's content parses it and reads `to_json`'s desugared spelling.
A test that needs YAML's own verdict on a text asks `yaml.SafeLoader`, which is
what the parser builds YAML with; `YamlWithSophiosTagsOpaque` lets it read a
text that carries the Sophios tags, without giving them any meaning.
"""
from typing import Any

import yaml

from sophios.lang import parse, to_json
from sophios.utils_yaml import Tag


def read_wic(text: str, filename: str = 'written.wic') -> dict[str, Any]:
    """`text` as `parse` reads it, in the desugared spelling `to_json` writes.

    The text must parse cleanly: a test reading a document back is asserting
    what a valid document says, and a diagnostic would make that claim vacuous.
    """
    result = parse(text, filename)
    assert result.ok and result.document is not None, [str(d) for d in result.diagnostics]
    return to_json(result.document)


class YamlWithSophiosTagsOpaque(yaml.SafeLoader):  # pylint: disable=too-many-ancestors  # SafeLoader's own depth
    """`yaml.SafeLoader`, with every Sophios tag transparent.

    It carries no Sophios meaning: a tagged node is built as its plain YAML
    content, a scalar as its text and a collection as its content, the same
    rule for every Sophios tag. This is how an editor that declares the tags
    sees a `.wic`. Every other YAML rule is SafeLoader's, so a tag Sophios
    does not own is SafeLoader's error. What a Sophios tag means is `parse`'s
    to say, and tests assert it there.
    """


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
