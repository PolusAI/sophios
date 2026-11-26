"""YAML serialization and disk I/O for CWL artifacts and job inputs."""

from collections.abc import Collection
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML as _RuamelYAML
from ruamel.yaml.nodes import ScalarNode as _RuamelScalarNode

from .. import auto_gen_header
from ..ir.artifacts import CompilationArtifact
from ..ir.locations import absolute_paths
from ..lang.values import read_value, write_value
from ..lang.diagnostics import SophiosError
from ..lang.error_codes import SophiosErrorCode
from ..runtime.inputs import EmptyDirectory, directory_literal, runner_job_inputs
from ..wic_types import Json, Yaml


# snakeyaml (a cromwell dependency) refuses to parse yaml files with more than
# 50 anchors/aliases to prevent Billion Laughs attacks.
# See https://en.wikipedia.org/wiki/Billion_laughs_attack
# Solution: Inline the contents of the aliases into the anchors.
# See https://ttl255.com/yaml-anchors-and-aliases-and-how-to-disable-them/#override


# cwltool reads job files and documents with ruamel (YAML 1.2 rules), PyYAML
# writes them with YAML 1.1 rules, and the two disagree on what is a number:
# PyYAML leaves `1e3`, `0o17` and `._5` plain, which ruamel reads as a float, an
# int and a float (`._` as a float it then cannot convert). Every string is
# written quoted when either reader would type it as anything else.
_YAML12_RESOLVER = _RuamelYAML(typ='rt').resolver
_STR_TAG = 'tag:yaml.org,2002:str'


class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    plain = dumper.represent_str(data)
    if plain.style is None and _YAML12_RESOLVER.resolve(_RuamelScalarNode, data, (True, False)) != _STR_TAG:
        return dumper.represent_scalar(_STR_TAG, data, style="'")
    return plain


NoAliasDumper.add_representer(str, _represent_str)


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


def write_artifacts_to_disk(artifact: CompilationArtifact, path: Path, inputs_file: str = '',
                            *, output_targets: Collection[str],
                            empty_directory: EmptyDirectory = directory_literal) -> None:
    """Write a graph-derived artifact tree in `path`, each document under its own name, and the root's job.

    Only the root has a job: a runner reads no other. A relative `location` or `path` of a File or
    Directory in `inputs_file`, at any depth, is resolved against the inputs file's own directory
    (CWL v1.2 section 5.1.5: the base IRI of the document) and written absolute. Each of `output_targets`
    (`runtime.inputs.output_target_inputs`) is a name for the run to create, so it is read as one first and
    written as the empty directory the runner creates, in the form `empty_directory` gives.
    """
    inputs = write_value(absolute_paths(read_value(runner_job_inputs(read_inputs_file(inputs_file), output_targets)),
                                        Path(inputs_file).absolute().parent)) if inputs_file else {}
    path.mkdir(parents=True, exist_ok=True)
    job = runner_job_inputs({**artifact.job_inputs, **inputs}, output_targets, empty_directory)
    (path / f'{artifact.name}_inputs.yml').write_text(
        f'{auto_gen_header}{dump_wic_yaml(job)}', encoding='utf-8')
    _write_documents(artifact, path)


def _write_documents(artifact: CompilationArtifact, path: Path) -> None:
    (path / f'{artifact.name}.cwl').write_text(
        f'#!/usr/bin/env cwl-runner\n{auto_gen_header}{dump_wic_yaml(artifact.cwl)}',
        encoding='utf-8')
    for child in artifact.children:
        _write_documents(child, path)
