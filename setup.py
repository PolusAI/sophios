import json
from pathlib import Path
from shutil import copytree, ignore_patterns
from setuptools import setup, find_packages
from setuptools.command.build_py import build_py as _build_py
import versioneer


class build_py(_build_py):
    """build mode script"""

    def run(self) -> None:
        adapters_dir = Path(__file__).parent / 'cwl_adapters'
        examples_dir = Path(__file__).parent / 'docs' / 'tutorials'
        adapters_target_dir = Path(self.build_lib) / 'sophios/cwl_adapters'
        examples_target_dir = Path(self.build_lib) / 'sophios/examples'
        extlist = ['*.png', '*.md', '*.rst', '*.pyc', '__pycache__', '*.json']
        copytree(adapters_dir, adapters_target_dir, dirs_exist_ok=True)
        copytree(examples_dir, examples_target_dir, ignore=ignore_patterns(*extlist), dirs_exist_ok=True)
        # Never overwrite user config
        global_config_file = Path.home() / 'wic/global_config.json'

        if not global_config_file.exists():
            config_path = global_config_file.parent
            config_path.mkdir(parents=True, exist_ok=True)
            basic_config_file = Path(__file__).parent / 'src' / 'sophios' / 'config_basic.json'
            config = {}
            # config_file can contain absolute or relative paths
            with open(basic_config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            config['search_paths_cwl']['global'] = [str(adapters_dir)]
            config['search_paths_wic']['global'] = [str(examples_dir)]
            # write out the config file with paths
            with open(global_config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4)

        # Continue with the standard build process
        super().run()


setup(
    name='sophios',
    version=versioneer.get_version(),
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    include_package_data=True,
    cmdclass={
        'build_py': build_py,
    },
    package_data={
        "sophios": ["cwl_adapters/*.cwl", "examples/*.wic"]
    }
)
