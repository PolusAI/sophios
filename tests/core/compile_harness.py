"""One way to compile an in-memory workflow with the real registry."""
import sophios.compiler
from sophios.ir.artifacts import CompilationResult
from sophios.wic_types import Yaml

from .budgets import budget
from .hermetic import bundle
from .test_setup import load_test_registry

#: Shared Hypothesis budgets; compiled properties are far slower than parse-only ones, so COMPILED gets a smaller count.
FAST = budget(200)
COMPILED = budget(100)


def compile_info(yml: Yaml, name: str = 'harness') -> CompilationResult:
    """Compile one in-memory workflow against the discovered registry."""
    return sophios.compiler.compile_source(
        bundle(yml, name, load_test_registry().tools), sophios.compiler.CompileSettings(), quiet=True)
