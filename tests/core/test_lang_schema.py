"""The exported JSON Schema and the parser are two readers of one declaration: the schema refuses a document exactly
when the parser reports its shape or its CWL version, and `sophios --generate_schemas` writes it and nothing else."""
import json
from pathlib import Path
from typing import Any, Final

import jsonschema
import pytest
import yaml
from cwltool.update import ALLUPDATES, UPDATES
from hypothesis import example, given
from hypothesis import strategies as st

from sophios.lang import SophiosErrorCode, parse, to_json, wic_schema

from .budgets import budget
from .cases import DATA
from .strategies import pinned, shapes

VALIDATOR: Final = jsonschema.Draft202012Validator(wic_schema())

#: What the parser reports about a document's shape and its CWL version, as distinct from what it says about names,
#: tags and edges, which JSON cannot spell.
_SHAPE_CODES: Final = frozenset({
    SophiosErrorCode.EXPECTED_MAPPING, SophiosErrorCode.EXPECTED_SEQUENCE, SophiosErrorCode.EXPECTED_SCALAR,
    SophiosErrorCode.MISSING_STEP_ID, SophiosErrorCode.EMPTY_STEP_ID, SophiosErrorCode.MALFORMED_WIC_STEP_KEY,
    SophiosErrorCode.UNKNOWN_WIC_KEY, SophiosErrorCode.MALFORMED_WIC_VALUE, SophiosErrorCode.UNSUPPORTED_CWL_VERSION})

#: A `cwlVersion` value: each version CWL ever named, drafts and snapshots among them, and anything else.
_VERSIONS = st.one_of(st.sampled_from(sorted(ALLUPDATES)), st.text('v1.023-dev ', max_size=8), st.floats(0, 2),
                      st.none(), st.lists(st.just('v1.2'), max_size=1))


#: Shapes the parser reports, each alone, as a drawn document seldom holds one fault without another.
_ALONE: Final = (
    {'steps': 'nope'}, {'steps': {'s': 3}}, {'steps': {'s': {'in': 3}}}, {'steps': {'s': {'out': 3}}},
    {'steps': {'s': {'out': [['s']]}}}, {'wic': 3}, {'wic': {'steps': 3}}, {'wic': {'steps': {'not a key': {'x': 1}}}},
    {'wic': {'scatter': ['x']}}, {'steps': [{'touch': {'in': {'f': 'x'}}}]}, {'steps': [{'touch': None}]},
    {'steps': [{'in': {'value': 'something'}}]}, {'steps': [{'id': ''}]})


def _tag_free(path: Path) -> Any:
    """The mapping the document at `path` is, as YAML loads it, where it holds no tag and no alias; else None."""
    text = path.read_text(encoding='utf-8')
    try:
        loaded = None if '!' in text or '&' in text else yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


@pytest.mark.fast
@given(st.builds(lambda shape, version: {**shape, **version}, shapes(),
                 st.fixed_dictionaries({}, optional={'cwlVersion': _VERSIONS})))
@pinned(*_ALONE, *filter(None, map(_tag_free, sorted((DATA / 'parse').glob('*/*.wic')))),
        *({'cwlVersion': version} for version in ['draft-2', 'draft-3', 'v1.0.dev4', 'v1.2.0-dev5', 'v1.3', '',
                                                  *sorted(UPDATES)]))
@example({'steps': [{'id': 1}]}).xfail(raises=AssertionError, reason="the parser reads the id as the name '1', the "
                                       'schema wants a string')
@example({'steps': {'s': {'out': [{'f': 'x'}]}}}).xfail(raises=AssertionError, reason='wic005: an out: entry binds '
                                                        'only !&, which the schema does not say')
@example({'wic': {'steps': {'s': {'out': 3}}}}).xfail(raises=AssertionError, reason='wic004: the schema leaves out: '
                                                      'under wic: steps: open')
@example({'steps': {'s': {'in': {'f': {'wic_alias': ['x']}}}}}).xfail(raises=AssertionError, reason='wic005: the '
                                                                      'schema leaves a construct open')
@budget(100)
def test_the_schema_refuses_exactly_what_the_parser_reports(document: dict[str, Any]) -> None:
    """Over tag-free documents, where loading one is its projection: the schema refuses a document, as an editor loads
    it, exactly when the parser reports its shape or its CWL version; the parser reports a `cwlVersion` exactly when
    cwltool cannot run it without `--enable-dev`, where it is written rather than later inside the runner; and what the
    parser accepts projects to JSON the schema accepts."""
    result = parse(json.dumps(document), 'shape.wic')
    reported = [str(d) for d in result.diagnostics if d.code in _SHAPE_CODES]
    assert VALIDATOR.is_valid(document) == (not reported), ([str(d) for d in result.diagnostics],
                                                            list(VALIDATOR.iter_errors(document)))
    if 'cwlVersion' in document:
        assert ((SophiosErrorCode.UNSUPPORTED_CWL_VERSION in {d.code for d in result.diagnostics})
                == (document['cwlVersion'] not in tuple(UPDATES)))
    if not result.diagnostics.has_errors and result.document is not None:
        projection = json.loads(json.dumps(to_json(result.document)))
        assert VALIDATOR.is_valid(projection), list(VALIDATOR.iter_errors(projection))


@pytest.mark.fast
def test_generate_schemas_writes_the_schema_and_compiles_nothing(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    """`sophios --generate_schemas` writes `wic_schema()`, a legal JSON Schema of the dialect it claims, and compiles
    nothing. The schema it replaced compiled each discovered workflow and read the compiled inputs back, so a
    subworkflow with an unbound inner input advertised the compiler's generated name for it (`mk_file~name`, `__step__`)
    as something a caller may bind, where a caller may name only what the callee declares (wic032)."""
    import sophios.compiler  # pylint: disable=import-outside-toplevel
    import sophios.main  # pylint: disable=import-outside-toplevel
    import sophios.plugins  # pylint: disable=import-outside-toplevel

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError('--generate_schemas reached discovery or compilation')

    for module, name in [(sophios.compiler, 'compile_source'), (sophios.plugins, 'get_tools_cwl'),
                         (sophios.plugins, 'get_yml_paths'), (sophios.main.io, 'get_config')]:
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['sophios', '--generate_schemas', '--homedir', str(tmp_path)])
    with pytest.raises(SystemExit) as caught:
        sophios.main._main(*sophios.cli.parser.parse_known_args())  # pylint: disable=protected-access
    assert caught.value.code == 0

    written = json.loads((tmp_path / 'autogenerated' / 'schemas' / 'wic.json').read_text(encoding='utf-8'))
    jsonschema.Draft202012Validator.check_schema(written)
    assert written == wic_schema()
