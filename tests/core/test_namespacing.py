"""The language layer's shared state is namespaced and safe to share.

Two things are checked here, and neither is style:

**Nothing leaks into a global.** `wic_loader()` used to register the Sophios
tags on `yaml.SafeLoader` itself, which changed how every `yaml.safe_load` in
the process behaved — including calls from libraries that had never heard of
Sophios — for the rest of the program's life.

**Shared tables cannot be mutated.** A module-level `dict` is reachable from
every thread. One caller mutating it changes parsing for all of them, and the
failure surfaces somewhere else entirely.

Neither is caught by ordinary tests, because both are invisible until a second
thread or a second library is involved.
"""
from concurrent.futures import ThreadPoolExecutor
from types import MappingProxyType
from typing import Any

import pytest
import yaml

from sophios.lang import Forms, Grammar, parse, wic_schema
from sophios.lang import schema as schema_module
from sophios.lang.render import render
from sophios.utils_yaml import Key, Tag, WicLoader, wic_loader

SOURCE = 'steps:\n- id: touch\n  in:\n    f: !ii empty.txt\n  out:\n  - file: !& out\n'


# --------------------------------------------------------------------------
# Nothing leaks into a global
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_the_loader_does_not_touch_the_global_safeloader() -> None:
    """Using the Sophios loader leaves `yaml.safe_load` alone.

    Registering on `yaml.SafeLoader` would make every later `safe_load` in the
    process interpret `!ii`, anywhere, including in unrelated libraries.
    """
    yaml.load(SOURCE, Loader=wic_loader())

    for tag in Tag.ALL:
        assert tag not in yaml.SafeLoader.yaml_constructors, f'{tag} leaked onto SafeLoader'

    with pytest.raises(yaml.YAMLError):
        yaml.safe_load('x: !ii 5')


@pytest.mark.fast
def test_the_loader_is_a_subclass_not_safeloader_itself() -> None:
    """The tags live on a dedicated class, so their scope is the callers who ask."""
    assert wic_loader() is WicLoader
    assert issubclass(WicLoader, yaml.SafeLoader)
    assert WicLoader is not yaml.SafeLoader


@pytest.mark.fast
def test_the_loader_still_understands_every_tag() -> None:
    """Isolation did not cost the tags themselves."""
    loaded = yaml.load('a: !ii 5\nb: !& e\nc: !* e\n', Loader=wic_loader())
    assert loaded == {'a': {Key.INLINE_INPUT: 5}, 'b': {Key.ANCHOR: 'e'}, 'c': {Key.ALIAS: 'e'}}


# --------------------------------------------------------------------------
# Shared tables cannot be mutated
# --------------------------------------------------------------------------

# Grammar.SCALAR_TAGS is deliberately absent: scalar resolution is delegated
# to PyYAML's SafeConstructor (one instance per call), so there is no shared
# table to protect.
SHARED_MAPPINGS = [
    ('Forms.TAGGED', Forms.TAGGED),
    ('Forms.DESUGARED', Forms.DESUGARED),
    # Private, but reachable from every thread all the same, and swapping a
    # builder in it would change every later schema export process-wide.
    ('schema._SHAPE_SCHEMA', schema_module._SHAPE_SCHEMA),
]


@pytest.mark.fast
@pytest.mark.parametrize('name,mapping', SHARED_MAPPINGS, ids=[n for n, _ in SHARED_MAPPINGS])
def test_shared_mappings_are_read_only(name: str, mapping: Any) -> None:
    """A table every thread reads is one no thread can write."""
    assert isinstance(mapping, MappingProxyType), f'{name} is a mutable mapping'
    with pytest.raises(TypeError):
        mapping['injected'] = None  # type: ignore[index]  # the point of the test


@pytest.mark.fast
def test_the_membership_sets_are_derived_from_the_members() -> None:
    """`Tag.ALL` and `Key.ALL` are computed, never restated.

    The two facts are established independently — a constructor is registered
    on the loader for each tag, and the parser asks `Tag.ALL` which tags the
    language owns — so they can disagree, and the disagreement is silent in
    the direction that matters: a tag with a working constructor but missing
    from the set is reported as an unknown tag. Deriving the set removes the
    edit that could go missing; this checks the two views still agree.
    """
    # PyYAML keys its own default constructor under None; the wic tags are the
    # ones this class adds beyond what SafeLoader already had.
    registered = set(WicLoader.yaml_constructors) - set(yaml.SafeLoader.yaml_constructors)
    assert registered == Tag.ALL

    assert Tag.ALL == {Tag.ANCHOR, Tag.ALIAS, Tag.INLINE_INPUT, Tag.RAW_CWL}
    assert Key.ALL == {Key.ANCHOR, Key.ALIAS, Key.INLINE_INPUT, Key.RAW_CWL}


@pytest.mark.fast
def test_shared_sets_are_frozen() -> None:
    """The same, for the sets."""
    for shared in (Tag.ALL, Key.ALL, Grammar.INTERPRETED_STEP_KEYS, Forms.DESUGARED_KEYS):
        assert isinstance(shared, frozenset)


@pytest.mark.fast
def test_the_exported_schema_is_not_shared() -> None:
    """Each export is a fresh object, so one caller's edits cannot leak."""
    first = wic_schema()
    first['properties'].clear()
    assert wic_schema()['properties']


# --------------------------------------------------------------------------
# Concurrent use
# --------------------------------------------------------------------------


@pytest.mark.fast
def test_parsing_and_rendering_are_safe_from_many_threads() -> None:
    """The same source parsed concurrently gives every thread the same answer.

    Not a proof of thread safety — no test is — but it exercises the shared
    tables, the loader, and the schema export from several threads at once,
    which is where per-module mutable state would show up as divergence.
    """
    def once(_: int) -> tuple[str, int]:
        document = parse(SOURCE, 'concurrent.wic').document
        assert document is not None
        return render(document), len(wic_schema()['$defs'])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(once, range(64)))

    assert len(set(results)) == 1, 'concurrent parses disagreed'
