from typing import Dict
from pathlib import Path


class default_values:
    default_run_args_dict: Dict[str, str] = {'cwl-runner': 'cwltool',
                                             'container_engine': 'docker',
                                             'pull_dir': str(Path().cwd())}
