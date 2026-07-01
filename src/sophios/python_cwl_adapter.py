import importlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

DRIVER_SCRIPT = '/python_cwl_driver.py'
TYPES_SCRIPT = '/workflow_types.py'

TYPES_SCRIPT_REL = '../sophios/examples/scripts/workflow_types.py'

# NOTE: VERY IMPORTANT: Since we have to programmatically import the python file in the compiler,
# and since the act of importing it executes the entire file (i.e. including import statements),

# USERS SHOULD NOT USE TOP-LEVEL IMPORT STATEMENTS!

# See the following links for a more detailed explanation
# https://stackoverflow.com/questions/2724260/why-does-pythons-import-require-fromlist
# https://stackoverflow.com/questions/8790003/dynamically-import-a-method-in-a-file-from-a-string


def import_python_file(python_module_name: str, python_file_path: Path) -> ModuleType:
    """This function import a python file directly, as per the documentation\n
    https://docs.python.org/3/library/importlib.html#importing-a-source-file-directly

    Args:
        python_module_name (str): The name of the python module
        python_file_path (Path): The path to the python file.

    Returns:
        ModuleType: The module that was loaded.
    """
    # NOTE: Apparently import_module resolves symlinks before attempting to import.
    # Since python uses relative paths to determine modules, as you can imagine
    # this causes massive problems. By default, CWL symlinks every single input
    # file into its own temporary directory. If you use an initial workdir, the
    # symlinks are now all in the same directory, but their sources are still
    # pointing wherever. Thus, import_module will never work!
    # The solution is in the examples at the bottom of the documentation
    # https://docs.python.org/3/library/importlib.html#importing-a-source-file-directly
    # and https://stackoverflow.com/questions/65206129/importlib-not-utilising-recognising-path
    spec = importlib.util.spec_from_file_location(
        name=python_module_name,  # module name (not file name)
        location=str(python_file_path.absolute())  # ABSOLUTE path!
    )
    if spec:
        module_ = importlib.util.module_from_spec(spec)
        sys.modules[python_module_name] = module_

        try:
            if spec.loader:
                spec.loader.exec_module(module_)  # guard behind if to satisfy mypy
            else:
                raise Exception
        except Exception as e:
            raise Exception(f'Error! Cannot load python_script {python_file_path}') from e
    else:
        raise Exception(f'Error! Cannot load python_script spec {spec} from file\n{python_file_path}')
    return module_


def get_main_args(module_: ModuleType) -> dict[str, Any]:
    """Uses inspect to get the arguments to the main() function of the given module.

    Args:
        module_ (ModuleType): A ModuleType object returned from import_python_file

    Returns:
        Dict[str, Any]: A dictionary of keys value pairs
    """
    # importing at the top-level causes a circular import error
    # (jsonschema transitively imports inspect)
    import inspect  # pylint: disable=import-outside-toplevel

    anns = inspect.getfullargspec(module_.main).annotations
    if 'return' in anns:
        del anns['return']
    return anns


def check_args_match_inputs(module_: ModuleType, args: dict[str, Any], check: bool = False) -> None:
    """Checks that the keys (only) of the args dict match the keys of the top-level inputs attribute.

    Args:
        module_ (ModuleType): A ModuleType object returned from import_python_file
        args (Dict[str, Any]): A dictionary of keys value pairs
    """
    error = False
    for arg in args:
        if arg not in module_.inputs:
            print(f'Error! wic argument {arg} not in python arguments {module_.inputs}')
            error = True
    # Wait until after inference
    if check:
        for arg in module_.inputs:
            if arg not in args:
                print(f'Error! Python argument {arg} not in wic arguments {args}')
                error = True
    if error:
        sys.exit(1)


def generate_CWL_CommandLineTool(module_inputs: dict[str, Any], module_outputs: dict[str, Any],
                                 python_script_docker_pull: str = '') -> dict[str, Any]:
    """Generates a CWL CommandLineTool for an arbitrary (annotated) python script.

    Args:
        module_inputs (Dict[str, Any]): The top-level inputs attribute of the python module.
        module_outputs (Dict[str, Any]): The top-level inputs attribute of the python module.
        python_script_docker_pull (str): The username/image to use with docker pull ...

    Returns:
        Dict[str, Any]: A CWL CommandLineTool with the given inputs and outputs.
    """
    yaml_tree: dict[str, Any] = {}
    yaml_tree['cwlVersion'] = 'v1.0'
    yaml_tree['class'] = 'CommandLineTool'
    yaml_tree['$namespaces'] = {'edam': 'https://edamontology.org/'}
    yaml_tree['$schemas'] = ['https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl']
    yaml_tree['baseCommand'] = 'python3'

    requirements: dict[str, Any] = {'InlineJavascriptRequirement': {}}
    if python_script_docker_pull:
        requirements['DockerRequirement'] = {'dockerPull': python_script_docker_pull}
    yaml_tree['requirements'] = requirements

    def input_binding(position: int, prefix: str = '') -> dict[str, Any]:
        if prefix == '':
            return {'inputBinding': {'position': position}}
        return {'inputBinding': {'position': position, 'prefix': f'--{prefix}'}}

    inputs: dict[str, Any] = {}
    inputs['driver_script'] = {'type': 'string', 'format': 'edam:format_2330',
                               **input_binding(1), 'default': DRIVER_SCRIPT}
    inputs['workflow_types'] = {'type': 'string', 'format': 'edam:format_2330',
                                **input_binding(2), 'default': TYPES_SCRIPT}
    inputs['script'] = {'type': 'File', 'format': 'edam:format_2330', **input_binding(3)}
    for i, (arg_key, arg_val) in enumerate(module_inputs.items()):
        inputs[arg_key] = {**arg_val, **input_binding(i+4, arg_key)}
    yaml_tree['inputs'] = inputs

    outputs: dict[str, Any] = {}
    for arg_key, (glob_pattern, arg_val) in module_outputs.items():
        outputs[arg_key] = {**arg_val, 'outputBinding': {'glob': glob_pattern}}
    yaml_tree['outputs'] = outputs

    yaml_tree['stdout'] = 'stdout'
    return yaml_tree


def get_module(python_script_mod: str, python_script_path: Path, yml_args: dict[str, Any]) -> ModuleType:
    """Imports the given python script and validates its top-level annotations.

    Args:
        python_script_mod (str): The module name of the given python script.
        python_script_path (Path): The path to the given python script.
        yml_args (Dict[str, Any]): The contents of the python_script in: yml tag.

    Returns:
        ModuleType: The Module object associated with the given python script.
    """
    import_python_file('workflow_types', Path(TYPES_SCRIPT_REL))
    module_ = import_python_file(python_script_mod, python_script_path)

    main_args = get_main_args(module_)
    check_args_match_inputs(module_, main_args)
    # TODO: validate module_.inputs values (check types and formats, and nothing else)
    # TODO: validate module_.outputs

    check_args_match_inputs(module_, yml_args)
    return module_
