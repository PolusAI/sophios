"""One way to compile an in-memory workflow with the real registry."""
import sophios.compiler
from sophios.ir.artifacts import CompilationResult
from sophios.wic_types import Yaml

from .hermetic import bundle
from .test_setup import load_test_registry


def compile_info(yml: Yaml, name: str = 'harness') -> CompilationResult:
    """Compile one in-memory workflow against the discovered registry."""
    return sophios.compiler.compile_source(
        bundle(yml, name, load_test_registry().tools), sophios.compiler.CompileSettings(), quiet=True)
