import copy
from shutil import copytree, ignore_patterns
import json
from pathlib import Path
from typing import Any

import yaml

from . import auto_gen_header
from .runtime_inputs import normalize_artifact_cwl, normalize_artifact_job_inputs
from .ir.artifacts import CompilationArtifact
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


def write_artifacts_to_disk(artifact: CompilationArtifact, path: Path,
                            relative_run_path: bool, inputs_file: str = '') -> None:
    """Write a graph-derived artifact tree and its job-input documents."""
    inputs: Yaml = {}
    if inputs_file:
        with open(inputs_file, mode='r', encoding='utf-8') as stream:
            inputs = yaml.safe_load(stream.read())
        for value in inputs.values():
            if 'location' in value and not Path(value['location']).is_absolute():
                value['location'] = '../' + value['location']
    _write_artifacts_to_disk(artifact, path, relative_run_path, inputs)


def _write_artifacts_to_disk(artifact: CompilationArtifact, path: Path,
                             relative_run_path: bool, inputs: Yaml) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if relative_run_path:
        filename_cwl = f'{artifact.name}.cwl'
        filename_yml = f'{artifact.name}_inputs.yml'
    else:
        filename_cwl = '___'.join((*artifact.namespace, f'{artifact.name}.cwl'))
        filename_yml = '___'.join((*artifact.namespace, f'{artifact.name}_inputs.yml'))

    cwl = normalize_artifact_cwl(artifact)
    job = normalize_artifact_job_inputs(artifact, {**artifact.job_inputs, **inputs})
    (path / filename_cwl).write_text(
        f'#!/usr/bin/env cwl-runner\n{auto_gen_header}{dump_wic_yaml(cwl)}', encoding='utf-8')
    (path / filename_yml).write_text(
        f'{auto_gen_header}{dump_wic_yaml(job)}', encoding='utf-8')

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


def get_config(config_file: Path, default_config_file: Path) -> Json:
    """Returns the config json object from config_file with absolute paths

    Args:
        config_file (Path): The path of the user specified config file
        default_config_file (Path): The default path of the config file if user hasn't specified one

    Returns:
        Json: The config json object with absolute filepaths
    """
    global_config: Json = {}
    if not config_file.exists():
        global_config = get_basic_config()
        # write the basic config object to the 'global_config.json' file in user's ~/wic directory
        # for user to inspect and or modify the config json file
        write_config_to_disk(global_config, default_config_file)
        move_adapters_and_examples(global_config)
        print(f'default config file : {default_config_file} generated')
    else:
        # reading user specified config file only if it exists
        # never overwrite user's config file or generate another file in user's non-default directory
        # TODO : Validate the json inside 'read_config_from_disk' function
        global_config = read_config_from_disk(config_file)
    return global_config


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


def read_config_from_disk(config_file: Path, abspath: bool = True) -> Json:
    """Returns the config json object from config_file with absolute paths

    Args:
        config_file (Path): The path of json file where it is to be read from

    Returns:
        Json: The config json object with absolute filepaths
    """
    # config_file can contain absolute or relative paths
    config: Json = json.loads(config_file.read_text(encoding='utf-8'))
    conf_tags = ['search_paths_cwl', 'search_paths_wic']
    for tag in conf_tags:
        if abspath:
            config[tag] = get_absolute_paths(config[tag])
        else:  # this is a hacky way to fix global paths wrt ~/home/wic/
            config[tag] = get_home_paths(config[tag])
    return config


def get_basic_config() -> Json:
    """Returns the (default) basic config with absolute paths

    Returns:
        Json: The config json object with absolute filepaths
    """
    # basic config doesn't contain mm-workflows or other
    # domain cwl or wic files search paths
    src_dir = Path(__file__).parent
    basic_config: Json = {}
    # read_config_from_disk handles converting them to absolute paths
    basic_config = read_config_from_disk(src_dir/'config_basic.json', False)
    return basic_config


def get_absolute_paths(sub_config: Json) -> Json:
    """Update the paths within the sub_config json object as absolute paths

    Args:
        sub_config (dict): The json (sub)object where filepaths are stored

    Returns:
        Json: The json (sub)object with absolute filepaths
    """
    abs_sub_config = copy.deepcopy(sub_config)
    for ns in abs_sub_config:
        abs_paths = [str(Path(path).absolute()) for path in abs_sub_config[ns]]
        abs_sub_config[ns] = abs_paths
    return abs_sub_config


def get_home_paths(sub_config: Json) -> Json:
    """Update the paths within the sub_config json object as absolute paths

    Args:
        sub_config (dict): The json (sub)object where filepaths are stored

    Returns:
        Json: The json (sub)object with absolute filepaths
    """
    abs_sub_config = copy.deepcopy(sub_config)
    for ns in abs_sub_config:
        abs_paths = [str(Path.home() / path) for path in abs_sub_config[ns]]
        abs_sub_config[ns] = abs_paths
    return abs_sub_config
