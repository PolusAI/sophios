"""A real-time analysis: cat prints greeting.txt as the workflow writes it."""
from pathlib import Path

from sophios import config, plugins
from sophios.api.python.workflow import Step, Workflow


#: The tools that ship with Sophios, where `sophios --generate_config` put them.
ADAPTERS = Path.home() / 'wic' / 'cwl_adapters'


def workflow() -> Workflow:
    """Create and append to a file, watched by a cat analysis."""
    touch = Step(clt_path=ADAPTERS / 'touch.cwl')
    touch.inputs.filename = 'greeting.txt'
    append = Step(clt_path=ADAPTERS / 'append.cwl')
    append.inputs.str = 'Hello, Sophios'
    watch = Step(clt_path=ADAPTERS / 'file_watchers' / 'cwl_subinterpreter.cwl')
    watch.inputs.file_pattern = 'greeting.txt'
    watch.inputs.cwl_tool = 'cat'
    watch.inputs.max_times = 3
    watch.inputs.config = {'in': {'file': 'greeting.txt'}}
    return Workflow([touch, append, watch], 'watched')


if __name__ == '__main__':
    # The analysis names its tool by stem, so the run is given the tools of the configuration.
    tools = plugins.get_tools_cwl(config.get_config(None, Path.home()))
    workflow().run(tool_registry=tools)
