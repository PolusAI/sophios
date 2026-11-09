"""`cwltool --validate` without cwltool's per-call schema reload (`--skip-schemas`, a kept schema cache); each call
builds its own loader and id index, so one document's names cannot mask another's errors."""
from cwltool.process import custom_schemas, use_standard_schema


def _use_standard_schemas() -> None:
    """Return to cwltool's cached standard schema for every version that has a custom one."""
    for version in list(custom_schemas):
        use_standard_schema(version)


def validate_cwl(*paths: str) -> int:
    """The exit status of `cwltool --validate --skip-schemas` on `paths` (a document, then optionally its job)."""
    import cwltool.main  # pylint: disable=import-outside-toplevel  # pulls in spython, which needs `pwd` (no Windows)
    return int(cwltool.main.main(['--validate', '--quiet', '--skip-schemas', *paths],
                                 custom_schema_callback=_use_standard_schemas))
