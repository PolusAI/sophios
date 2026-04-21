import importlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Dict, Any

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
    # module = importlib.import_module(python_script_mod)
    # ModuleNotFoundError: No module named ...

    # The solution is buried in the examples at the bottom of the documentation
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
        # Note that now (after calling exec_module) we can call import_module without error
        # module_ = importlib.import_module(python_module_name)
    else:
        raise Exception(f'Error! Cannot load python_script spec {spec} from file\n{python_file_path}')
    return module_


def get_main_args(module_: ModuleType) -> Dict[str, Any]:
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
    ret = {'return': anns.get('return')}  # Separate out the return type
    if 'return' in anns:
        del anns['return']
    # print(anns)
    # print(ret)

    # print(inspect.signature(module_.main).parameters)
    # print(inspect.signature(module_.main).return_annotation)
    return anns


def check_args_match_inputs(module_: ModuleType, args: Dict[str, Any], check: bool = False) -> None:
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


def generate_CWL_CommandLineTool(module_inputs: Dict[str, Any], module_outputs: Dict[str, Any],
                                 python_script_docker_pull: str = '') -> Dict[str, Any]:
    """Generates a CWL CommandLineTool for an arbitrary (annotated) python script.

    Args:
        module_inputs (Dict[str, Any]): The top-level inputs attribute of the python module.
        module_outputs (Dict[str, Any]): The top-level inputs attribute of the python module.
        python_script_docker_pull (str): The username/image to use with docker pull ...

    Returns:
        Dict[str, Any]: A CWL CommandLineTool with the given inputs and outputs.
    """
    yaml_tree: Dict[str, Any] = {}
    yaml_tree['cwlVersion'] = 'v1.0'
    yaml_tree['class'] = 'CommandLineTool'
    yaml_tree['$namespaces'] = {'edam': 'https://edamontology.org/'}
    yaml_tree['$schemas'] = ['https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl']
    yaml_tree['baseCommand'] = 'python3'

    types_entry = '$(inputs.workflow_types)'
    driver_entry = '$(inputs.driver_script)'
    script_entry = '$(inputs.script)'
    requirements: Dict[str, Any] = {}
    requirements = {  # 'InitialWorkDirRequirement': {'listing': [script_entry]}, #[types_entry,driver_entry,script_entry]
        'InlineJavascriptRequirement': {}}
    if python_script_docker_pull:
        requirements['DockerRequirement'] = {'dockerPull': python_script_docker_pull}
    yaml_tree['requirements'] = requirements

    def input_binding(position: int, prefix: str = '') -> Dict[str, Any]:
        if prefix == '':
            return {'inputBinding': {'position': position}}
        return {'inputBinding': {'position': position, 'prefix': f'--{prefix}'}}

    inputs: Dict[str, Any] = {}
    # driver_script_file = {'class': 'File', 'path': driver_script}
    inputs['driver_script'] = {'type': 'string', 'format': 'edam:format_2330',
                               **input_binding(1), 'default': DRIVER_SCRIPT}  # driver_script_file
    # workflow_types_file = {'class': 'File', 'path': types_script}
    inputs['workflow_types'] = {'type': 'string', 'format': 'edam:format_2330',
                                **input_binding(2), 'default': TYPES_SCRIPT}  # workflow_types_file
    inputs['script'] = {'type': 'File', 'format': 'edam:format_2330', **input_binding(3)}
    for i, (arg_key, arg_val) in enumerate(module_inputs.items()):
        inputs[arg_key] = {**arg_val, **input_binding(i+4, arg_key)}
    # inputs['args'] = {'type': 'string', **input_binding(4)}
    yaml_tree['inputs'] = inputs

    outputs: Dict[str, Any] = {}
    for i, (arg_key, (glob_pattern, arg_val)) in enumerate(module_outputs.items()):
        outputs[arg_key] = {**arg_val, 'outputBinding': {'glob': glob_pattern}}
    # output_all is optional, but good for debugging bad glob patterns
    output_all = {'type':
                  {'type': 'array',
                   'items': ['Directory', 'File']},
                  'outputBinding': {'glob': '.'},
                  'format': 'edam:format_2330'}  # 'Textual format'
    # This crashes toil-cwl-runner, but not cwltool.
    # outputs['output_all'] = output_all
    yaml_tree['outputs'] = outputs

    yaml_tree['stdout'] = 'stdout'
    return yaml_tree


def get_module(python_script_mod: str, python_script_path: Path, yml_args: Dict[str, Any]) -> ModuleType:
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
    # print(module_.inputs)
    # print(module_.outputs)

    main_args = get_main_args(module_)
    check_args_match_inputs(module_, main_args)
    # TODO: validate module_.inputs values (check types and formats, and nothing else)
    # TODO: validate module_.outputs

    check_args_match_inputs(module_, yml_args)
    return module_


def get_inputs_workflow(module_inputs: Dict[str, Any], python_script_path: str,
                        yml_args: Dict[str, Any]) -> Dict[str, Any]:
    """This generates the contents of the inputs file associated with generate_CWL_CommandLineTool\n
    Note that this is already taken care of in the compiler, but this function\n
    is useful for standalone purposes. (Alternatively, just make a single-step workflow.)

    Args:
        module_inputs (Dict[str, Any]): The top-level inputs attribute of the python module.
        python_script_path (str): The path to the given python script.
        yml_args (Dict[str, Any]): The contents of the python_script in: yml tag.

    Returns:
        Dict[str, Any]: The contents of the CWL inputs file.
    """
    inputs_workflow = {}
    inputs_workflow['script'] = {'class': 'File', 'format': 'edam:format_2330', 'path': python_script_path}
    for i, (arg, yml_val) in enumerate(yml_args.items()):
        if module_inputs[arg]['type'] == 'string':
            inputs_workflow[arg] = yml_val
        else:
            inputs_workflow[arg] = {'class': 'File', 'format': module_inputs[arg]['format'], 'path': yml_val}
    # inputs_workflow = {'script': f'{python_script}.py', **yml_args}
    # inputs_workflow = {'script': f'{python_script}.py', 'args': json.dumps(yml_args)}
    return inputs_workflow
