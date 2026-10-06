"""`cwltool --validate` for the tests, without cwltool's per-call schema reload.

`cwltool.main.main` rebuilds the CWL schema salad on every call unless it is
told to use the standard one, and, given a `$schemas` entry, parses EDAM with
rdflib. `--skip-schemas` drops the second cost; `custom_schema_callback` keeps
the standard schema cache for the first. Each call still builds its own loader
and id index, so one document's names cannot mask another's errors.
"""
import cwltool.main
from cwltool.process import custom_schemas, use_standard_schema


def _use_standard_schemas() -> None:
    """Return to cwltool's cached standard schema for every version that has a custom one."""
    for version in list(custom_schemas):
        use_standard_schema(version)


def validate_cwl(*paths: str) -> int:
    """The exit status of `cwltool --validate --skip-schemas` on `paths` (a document, then optionally its job)."""
    return cwltool.main.main(['--validate', '--quiet', '--skip-schemas', *paths],
                             custom_schema_callback=_use_standard_schemas)
