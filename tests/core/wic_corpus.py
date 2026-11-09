"""The Sophios conformance corpus: every workflow the compiler's own discovery (`get_yml_paths`) reaches, so corpus and
compiler cannot disagree. CI's config reaches every corpus repository and is the arbiter; locally you test yours."""
from pathlib import Path
from typing import Final

from sophios import input_output as io
from sophios import plugins
from sophios.cli import get_args

#: In-repo fallback when no config exists: a pure read, so collecting the
#: suite on a fresh machine provisions nothing. Reading the user's config
#: instead writes ~/wic/global_config.json and copies adapters, which makes
#: `pytest --collect-only` provision the environment it is meant to inspect.
_IN_REPO_DIRS: Final = (
    Path(__file__).resolve().parents[2] / 'docs' / 'tutorials',
    Path(__file__).resolve().parents[2] / 'docs' / 'cookbook',
    Path(__file__).resolve().parents[2] / 'examples',
)


def _discover() -> tuple[Path, ...]:
    """Every reachable workflow, without side effects: the compiler's discovery when a config exists, else the in-repo
    directories, never provisioning the user's home."""
    args = get_args()
    config_path = args.config_file or io.default_config_file(Path(args.homedir))
    if config_path.exists():
        config = io.read_config_from_disk(config_path)
        return tuple(sorted(
            path
            for namespace in plugins.get_yml_paths(config).values()
            for path in namespace.values()
        ))
    return tuple(sorted(
        path for directory in _IN_REPO_DIRS if directory.is_dir()
        for path in directory.rglob('*.wic')
    ))


#: Sorted for stable test ids. One namespace's stem shadows another's in
#: `get_yml_paths` exactly as it does during compilation, so the corpus tests
#: precisely the files Sophios itself would use.
CORPUS: Final = _discover()


def corpus_id(path: Path) -> str:
    """Name a corpus file in test output, disambiguating repeated basenames."""
    return f'{path.parent.name}/{path.name}'
