"""One way to compile an in-memory workflow with the real registry: the typed result, or only the emitted CWL."""
import sophios.cli
import sophios.compiler
from sophios.ir.artifacts import CompilationResult
from sophios.wic_types import Yaml

from .budgets import budget
from .hermetic import bundle
from .test_setup import load_test_registry

#: Shared Hypothesis budgets; compiled properties are far slower than parse-only ones, so COMPILED gets a smaller count.
FAST = budget(200)
COMPILED = budget(100)

#: A minimal real workflow: one tool step, one inline literal input.
TOUCH: Yaml = {'steps': [{'id': 'touch', 'in': {'filename': {'wic_inline_input': 'empty.txt'}}}]}


def compile_info(yml: Yaml, name: str = 'harness', *,
                 lang_version: str | None = None,
                 allow_raw_cwl: bool | None = None) -> CompilationResult:
    """Compile one in-memory workflow; the overrides are named so a typo is a type error."""
    compiler_options = sophios.cli.default_compilation_settings()
    if lang_version is not None:
        compiler_options['lang_version'] = lang_version
    if allow_raw_cwl is not None:
        compiler_options['allow_raw_cwl'] = allow_raw_cwl
    return sophios.compiler.compile_source(
        bundle(yml, name, load_test_registry().tools), compiler_options,
        quiet=True)


def compile_cwl(yml: Yaml, name: str = 'harness', *,
                lang_version: str | None = None,
                allow_raw_cwl: bool | None = None) -> Yaml:
    """Compile one in-memory workflow and return the emitted CWL."""
    return compile_info(yml, name, lang_version=lang_version, allow_raw_cwl=allow_raw_cwl).artifact.cwl
