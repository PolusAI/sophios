import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

workflow_types_path = sys.argv[1]
workflow_types_mod = Path(workflow_types_path).name[:-3]

python_script_path = sys.argv[2]
python_script_mod = Path(python_script_path).name[:-3]

cli_args = sys.argv[3:]
if len(cli_args) % 2 != 0:
    print("Error! len(cli_args) is not even!")
    for arg in cli_args:
        print(arg)
    sys.exit(1)

cli_args_dict = {}
for idx in range(int(len(cli_args) / 2)):
    arg_key = cli_args[2*idx][2:]  # remove --
    arg_val = cli_args[2*idx+1]  # json.loads() ?
    cli_args_dict[arg_key] = arg_val


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
        if spec.loader:
            spec.loader.exec_module(module_)  # guard behind if to satisfy mypy
        else:
            print(f'Error! Cannot load {spec}')
            sys.exit(1)
    else:
        print(f'Error! Cannot load {spec}')
        sys.exit(1)
    return module_


# Note that both exec_module and import_module execute the entire file, i.e.
# including `from workflow_types import *` which causes
# "ModuleNotFoundError: No module named 'workflow_types'" unless we also stage
# and import workflow_types.py
import_python_file(workflow_types_mod, Path(workflow_types_path))
module = import_python_file(python_script_mod, Path(python_script_path))

# NOW we can call main()
retval = module.main(**cli_args_dict)

print('retval', retval)
sys.exit(retval)
