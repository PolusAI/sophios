import copy
from collections.abc import Iterator
from shutil import copytree, ignore_patterns
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from . import auto_gen_header
from .runtime_inputs import normalize_artifact_cwl, normalize_artifact_job_inputs
from .ir.names import NAMESPACE_SEPARATOR, Names, names_map
from .ir.artifacts import CompilationArtifact
from .lang.diagnostics import SophiosError
from .lang.error_codes import SophiosErrorCode
from .wic_types import Yaml, Json


# snakeyaml (a cromwell dependency) refuses to parse yaml files with more than
# 50 anchors/aliases to prevent Billion Laughs attacks.
# See https://en.wikipedia.org/wiki/Billion_laughs_attack
# Solution: Inline the contents of the aliases into the anchors.
# See https://ttl255.com/yaml-anchors-and-aliases-and-how-to-disable-them/#override


class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_wic_yaml(document: Json) -> str:
    """Serializes a compiled CWL document or job inputs to a YAML string.

    Uses NoAliasDumper to inline anchors/aliases (see module docstring above)
    and preserves key order (sort_keys=False) so e.g. workflow steps stay in order.

    Args:
        document (Json): The document to serialize, e.g. a compiled CWL tree
            or a job inputs mapping.

    Returns:
        str: The YAML-serialized document.
    """
    return yaml.dump(document, sort_keys=False, line_break='\n', indent=2, Dumper=NoAliasDumper)


def names_map_path(directory: Path, name: str) -> Path:
    """Where the compile writes, and run-time messages read, the map from emitted ids to authored names."""
    return directory / f'{name}.names.json'


def read_inputs_file(path: str) -> Yaml:
    """The job values in an `--inputs_file`; empty for an empty file.

    Raises:
        SophiosError: The file is not YAML, or its top level is not a mapping of input names to values.
    """
    try:
        with open(path, mode='r', encoding='utf-8') as stream:
            values = yaml.safe_load(stream.read())
    except yaml.YAMLError as error:
        raise SophiosError.error(SophiosErrorCode.INVALID_YAML,
                                 f'The inputs file {path} is not valid YAML: {" ".join(str(error).split())}') from error
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise SophiosError.error(
            SophiosErrorCode.NOT_A_MAPPING,
            f'The inputs file {path} must be a mapping of input names to values, not a {type(values).__name__}: '
            'write one `name: value` entry per input.')
    return values


def relative_local_path(written: Any) -> str | None:
    """`written` when it names a local file or directory by a relative path; None for anything else.

    Not relative: a non-string, a URI (`file:`, `https:`, ...) and an absolute path. A one-letter scheme
    is a Windows drive.
    """
    if not isinstance(written, str) or len(urlparse(written).scheme) > 1 or Path(written).is_absolute():
        return None
    return written


def input_paths(values: Yaml) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every File and Directory object in a job, with the input it is under, in written order.

    Found in lists, in records, in a File's `secondaryFiles` and in a Directory's `listing`.
    """
    for name, value in values.items():
        stack = [value]
        while stack:
            match stack.pop():
                case list() as items:
                    stack.extend(reversed(items))
                case {'class': 'File' | 'Directory'} as found:
                    yield name, found
                    stack.extend(reversed(found.get('secondaryFiles') or []))
                    stack.extend(reversed(found.get('listing') or []))
                case dict() as record:
                    stack.extend(reversed(list(record.values())))


def absolute_paths(values: Yaml, base: Path) -> Yaml:
    """A copy of `values` in which each relative `location` or `path` of a File or Directory, at any
    depth, is resolved against `base` and written absolute, so it names the same place wherever the
    run reads it from."""
    absolute = copy.deepcopy(values)
    for _name, found in input_paths(absolute):
        for key in ('location', 'path'):
            if (written := relative_local_path(found.get(key))) is not None:
                found[key] = str(base / written)
    return absolute


def write_artifacts_to_disk(artifact: CompilationArtifact, path: Path,
                            relative_run_path: bool, inputs_file: str = '') -> None:
    """Write a graph-derived artifact tree and its job-input documents.

    A relative `location` or `path` of a File or Directory in `inputs_file`, at any depth, is
    resolved against the inputs file's own directory (CWL v1.2 section 5.1.5: the base IRI of the
    document) and written absolute.
    """
    inputs = absolute_paths(read_inputs_file(inputs_file), Path(inputs_file).absolute().parent) if inputs_file else {}
    _write_artifacts_to_disk(artifact, path, relative_run_path, inputs)


def _write_artifacts_to_disk(artifact: CompilationArtifact, path: Path,
                             relative_run_path: bool, inputs: Yaml) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if relative_run_path:
        filename_cwl = f'{artifact.name}.cwl'
        filename_yml = f'{artifact.name}_inputs.yml'
    else:
        filename_cwl = NAMESPACE_SEPARATOR.join((*artifact.namespace, f'{artifact.name}.cwl'))
        filename_yml = NAMESPACE_SEPARATOR.join((*artifact.namespace, f'{artifact.name}_inputs.yml'))

    cwl = normalize_artifact_cwl(artifact)
    job = normalize_artifact_job_inputs(artifact, {**artifact.job_inputs, **inputs})
    (path / filename_cwl).write_text(
        f'#!/usr/bin/env cwl-runner\n{auto_gen_header}{dump_wic_yaml(cwl)}', encoding='utf-8')
    (path / filename_yml).write_text(
        f'{auto_gen_header}{dump_wic_yaml(job)}', encoding='utf-8')
    if artifact.graph is not None and artifact.namespace == ():
        # The root carries the whole tree: one map names every emitted id in it.
        names_map_path(path, artifact.name).write_text(
            json.dumps(names_map(artifact.graph, Names.of(artifact.graph)), indent=2), encoding='utf-8')

    for child in artifact.children:
        subpath = path / child.namespace[-1] if relative_run_path else path
        _write_artifacts_to_disk(child, subpath, relative_run_path, inputs)


def write_config_to_disk(config: Json, config_file: Path) -> None:
    """Writes config json object to config_file

    Args:
        config (Json): The json object that is to be written to disk
        config_file (Path): The file path where it is to be written
    """
    config_dir = Path(config_file).parent
    # make the full path if it doesn't exist
    config_dir.mkdir(parents=True, exist_ok=True)
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, sort_keys=True)


def default_config_file(homedir: Path) -> Path:
    """The config file Sophios uses when the user does not name one."""
    return homedir / 'wic' / 'global_config.json'


def generate_config(homedir: Path) -> Json:
    """Writes the default config, adapters and examples under the selected home directory.

    Args:
        homedir (Path): The home directory whose wic directory will hold the config and resources

    Returns:
        Json: The config json object with absolute filepaths
    """
    config = get_basic_config(homedir)
    write_config_to_disk(config, default_config_file(homedir))
    move_adapters_and_examples(config)
    return config


def get_config(config_file: Path | None, homedir: Path) -> Json:
    """Returns the config json object with absolute paths

    A config file the user names is only ever read. Without one, the default
    under homedir is used, and generated first if it does not exist yet.

    Args:
        config_file (Path | None): The user specified config file, if any
        homedir (Path): The user's home directory

    Raises:
        FileNotFoundError: If config_file does not exist.

    Returns:
        Json: The config json object with absolute filepaths
    """
    if config_file is None:
        config_file = default_config_file(homedir)
        if not config_file.exists():
            config = generate_config(homedir)
            print(f'default config file : {config_file} generated')
            return config
    return read_config_from_disk(config_file)


def move_adapters_and_examples(config: Json) -> None:
    """Copies all the adapters and examples into the given search paths

    Args:
        config (Json): Json containing search path information
    """
    # when using dev/git install
    paren_dir = 'src'
    if str(Path(__file__).parent.parent).endswith(paren_dir):
        adapters_dir = Path(__file__).parent.parent.parent / 'cwl_adapters'
        examples_dir = Path(__file__).parent.parent.parent / 'docs' / 'tutorials'
    # if the sophios installation is through pip package
    else:
        adapters_dir = Path(__file__).parent / 'cwl_adapters'
        examples_dir = Path(__file__).parent / 'examples'

    extlist = ['*.png', '*.md', '*.rst', '*.pyc', '__pycache__', '*.json']
    # the default search paths will be the first entry in the 'global' namespace
    # but we also want to preserve the ability of the users to add more search paths
    # so we keep the namespace tag as a list of paths rather than a single path
    copytree(adapters_dir, Path(config['search_paths_cwl']['global'][0]))
    copytree(examples_dir, Path(config['search_paths_wic']['global'][0]),
             ignore=ignore_patterns(*extlist))


def read_config_from_disk(config_file: Path, base_dir: Path | None = None) -> Json:
    """Returns the config json object from config_file with absolute paths

    Args:
        config_file (Path): The path of json file where it is to be read from
        base_dir (Path | None): Base for relative search paths; defaults to the working directory

    Returns:
        Json: The config json object with absolute filepaths

    Raises:
        TypeError: A namespace under ``search_paths_cwl`` or ``search_paths_wic`` is not a list of paths.
    """
    # config_file can contain absolute or relative paths
    config: Json = json.loads(config_file.read_text(encoding='utf-8'))
    base_dir = Path.cwd() if base_dir is None else base_dir.absolute()
    conf_tags = ['search_paths_cwl', 'search_paths_wic']
    for tag in conf_tags:
        sub_config = copy.deepcopy(config[tag])
        for ns in sub_config:
            if not isinstance(sub_config[ns], list):
                raise TypeError(f'{tag}.{ns} must be a list of paths, not {type(sub_config[ns]).__name__}')
            sub_config[ns] = [str(base_dir / path) for path in sub_config[ns]]
        config[tag] = sub_config
    return config


def get_basic_config(homedir: Path | None = None) -> Json:
    """Returns the (default) basic config with absolute paths

    Args:
        homedir (Path | None): Base for the packaged search paths; defaults to the process home

    Returns:
        Json: The config json object with absolute filepaths
    """
    # basic config doesn't contain mm-workflows or other
    # domain cwl or wic files search paths
    src_dir = Path(__file__).parent
    basic_config: Json = {}
    # read_config_from_disk handles converting them to absolute paths
    basic_config = read_config_from_disk(
        src_dir/'config_basic.json', base_dir=Path.home() if homedir is None else homedir)
    return basic_config
