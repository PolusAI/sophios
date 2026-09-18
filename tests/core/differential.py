"""Shared assertions for compiler differential properties."""
from sophios.ir.artifacts import CompilationArtifact, CompilationResult
from sophios.wic_types import Yaml

from .equivalence import Strength, equivalent


def compiled_documents(info: CompilationResult) -> tuple[Yaml, ...]:
    """Every workflow artifact in a compilation, root first."""
    found: list[Yaml] = []

    def visit(artifact: CompilationArtifact) -> None:
        if artifact.cwl.get('class') == 'Workflow':
            found.append(artifact.cwl)
        for child in artifact.children:
            visit(child)

    visit(info.artifact)
    return tuple(found)


def assert_compilations_equivalent(left: CompilationResult, right: CompilationResult,
                                   strength: Strength) -> None:
    """Assert the same artifact tree and the requested semantic strength."""
    left_docs, right_docs = compiled_documents(left), compiled_documents(right)
    assert len(left_docs) == len(right_docs), (
        f'artifact count differs: {len(left_docs)} != {len(right_docs)}')
    for index, (old, new) in enumerate(zip(left_docs, right_docs, strict=True)):
        divergence = equivalent(old, new, strength)
        assert divergence is None, f'artifact {index}: {divergence}'
