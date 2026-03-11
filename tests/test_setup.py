from pathlib import Path
import time
from typing import Dict

from hypothesis.strategies import SearchStrategy
import hypothesis_jsonschema as hj

import sophios
import sophios.cli
import sophios.input_output as io
import sophios.plugins
import sophios.schemas
import sophios.schemas.wic_schema
import sophios.utils
import sophios.apis
import sophios.apis.python.api
from sophios.wic_types import Json, Yaml


args = sophios.cli.get_args()
# Just read from the disk and pass around config object
global_config = io.get_config(Path(args.config_file), Path(args.config_file))
tools_cwl = sophios.plugins.get_tools_cwl(global_config, quiet=args.quiet)
sophios.apis.python.api.global_config = tools_cwl  # Use path fallback in the CI
yml_paths = sophios.plugins.get_yml_paths(global_config)


yaml_stems = sophios.utils.flatten([list(p) for p in yml_paths.values()])
schema_store: Dict[str, Json] = {}
validator = sophios.schemas.wic_schema.get_validator(tools_cwl, yaml_stems, schema_store, write_to_disk=True)

yml_paths_tuples = [(yml_path_str, yml_path)
                    for yml_namespace, yml_paths_dict in yml_paths.items()
                    for yml_path_str, yml_path in yml_paths_dict.items()]


for yml_path_str, yml_path in yml_paths_tuples:
    schema = sophios.schemas.wic_schema.compile_workflow_generate_schema(args.homedir, yml_path_str, yml_path,
                                                                         tools_cwl, yml_paths, validator,
                                                                         args.ignore_validation_errors,
                                                                         args.allow_raw_cwl)
    # overwrite placeholders in schema_store. See comment in get_validator()
    schema_store[schema['$id']] = schema

validator = sophios.schemas.wic_schema.get_validator(tools_cwl, yaml_stems, schema_store, write_to_disk=True)


def wic_yaml_filter_blank_steps(yml: Yaml) -> bool:
    """Filters out Yaml instances with blank steps.

    Args:
        yml (Yaml): A randomly generated Yaml instance.

    Returns:
        bool: True if there are no blank steps.
    """
    steps = yml.get('steps', [])
    return not (steps == [] or any([step == {} for step in steps]))


def wic_yaml_filter_implementations_or_steps(yml: Yaml) -> bool:
    """Filters out Yaml instances with no implementations and no steps.

    Args:
        yml (Yaml): A randomly generated Yaml instance.

    Returns:
        bool: True if there is either implementations or steps (or both).
    """
    return ('implementations' in yml or 'steps' in yml)


time_initial = time.time()

wic_schema = sophios.schemas.wic_schema.wic_main_schema(tools_cwl, yaml_stems, schema_store, hypothesis=True)
wic_strategy: SearchStrategy = hj.from_schema(wic_schema)
# NOTE: The CLI version of mypy and the VSCode version of mypy disagree on the
# following line. The "type: ignore" comment is NOT unused.
wic_strategy = wic_strategy.filter(wic_yaml_filter_blank_steps)
wic_strategy = wic_strategy.filter(wic_yaml_filter_implementations_or_steps)

time_final = time.time()
print(f'from_schema time: {round(time_final - time_initial, 4)} seconds')
print()
