import json
import sys
import traceback
from pathlib import Path

import sophios
import sophios.plugins
from sophios import input_output as io
from sophios.python_cwl_adapter import import_python_file


def test_compile_python_workflows() -> None:
    """This function imports (read: blindly executes) all python files in 'search_paths_wic'
       The python files are assumed to have a top-level workflow() function
       which returns a sophios.api.pythonapi.Workflow object.
       The python files should NOT call the .run() method!
       (from any code path that is automatically executed on import)
    """
    from sophios.api import pythonapi  # pylint: disable=C0415:import-outside-toplevel
    # Since this is completely different test path we have to copy
    # default .txt files to default global_config.json
    config_file = Path().home()/'wic'/'global_config.json'
    global_config = io.read_config_from_disk(config_file)
    pythonapi.global_config = sophios.plugins.get_tools_cwl(global_config)  # Use path fallback in the CI
    paths = sophios.plugins.get_py_paths(global_config)
    # Above we are assuming that config is default
    paths_tuples = [(path_str, path)
                    for namespace, paths_dict in paths.items()
                    for path_str, path in paths_dict.items()]
    any_import_errors = False
    for path_stem, path in paths_tuples:
        if 'mm-workflows' in str(path) or 'docs/tutorials/' in str(path):
            # Exclude paths that only contain 'regular' python files.
            continue
        # NOTE: Use anything (unique?) for the python_module_name.
        try:
            module = import_python_file(path_stem, path)
            # Let's require all python API files to define a function, say
            # def workflow() -> Workflow
            # so we can programmatically call it here:
            retval: pythonapi.Workflow = module.workflow()  # no arguments
            # which allows us to programmatically call Workflow methods:
            compiler_info = retval.compile()  # hopefully retval is actually a Workflow object!
            # But since this is python (i.e. not Haskell) that in no way eliminates
            # the above security considerations.

            # This lets us use path.parent to write a *.wic file in the
            # auto-discovery path, and thus reuse the existing wic CI
            retval.write_ast_to_disk(path.parent)

            # Programmatically blacklist subworkflows from running in config_ci.json
            # (Again, because subworkflows are missing inputs and cannot run.)
            config_ci = path.parent / 'config_ci.json'
            json_contents = {}
            if config_ci.exists():
                with open(config_ci, mode='r', encoding='utf-8') as r:
                    json_contents = json.load(r)
            run_blacklist: list[str] = json_contents.get('run_blacklist', [])
            # Use [1:] for proper subworkflows only
            subworkflows: list[pythonapi.Workflow] = retval.flatten_subworkflows()[1:]
            run_blacklist += [wf.process_name for wf in subworkflows]
            json_contents['run_blacklist'] = run_blacklist
            with open(config_ci, mode='w', encoding='utf-8') as f:
                json.dump(json_contents, f)

        except Exception as e:
            any_import_errors = True
            traceback.print_exception(type(e), value=e, tb=None)
    if any_import_errors:
        sys.exit(1)  # Make sure the CI fails
