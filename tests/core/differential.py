"""Shared assertions for old/new compiler differential properties."""
from sophios.wic_types import CompilerInfo, NodeData, RoseTree, Yaml

from .equivalence import Strength, equivalent


def compiled_documents(info: CompilerInfo) -> tuple[Yaml, ...]:
    """Every workflow artifact in a compilation, root first."""
    found: list[Yaml] = []

    def visit(rose: RoseTree) -> None:
        data: NodeData = rose.data
        if data.compiled_cwl.get('class') == 'Workflow':
            found.append(data.compiled_cwl)
        for child in rose.sub_trees:
            visit(child)

    visit(info.rose)
    return tuple(found)


def assert_compilations_equivalent(left: CompilerInfo, right: CompilerInfo,
                                   strength: Strength) -> None:
    """Assert the same artifact tree and the requested semantic strength."""
    left_docs, right_docs = compiled_documents(left), compiled_documents(right)
    assert len(left_docs) == len(right_docs), (
        f'artifact count differs: {len(left_docs)} != {len(right_docs)}')
    for index, (old, new) in enumerate(zip(left_docs, right_docs, strict=True)):
        divergence = equivalent(old, new, strength)
        assert divergence is None, f'artifact {index}: {divergence}'
