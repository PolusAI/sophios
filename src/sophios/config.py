"""Configuration bootstrap, packaged renaming conventions, and search-path loading."""

import copy
import json
from pathlib import Path
from shutil import copytree, ignore_patterns

from .wic_types import Json


#: The renamings Sophios ships, read from the packaged configuration.
SHIPPED_RENAMINGS: tuple[tuple[str, str], ...] = tuple(
    (before, after) for before, after in
    json.loads((Path(__file__).parent / 'config_basic.json').read_text('utf-8'))['renaming_conventions'])


def renamings_of(config: Json) -> list[tuple[str, str]]:
    """The renamings a configuration asks for: its `renaming_conventions` as given
    (an empty list means none), or the shipped ones when the key is missing."""
    if 'renaming_conventions' not in config:
        return list(SHIPPED_RENAMINGS)
    return [tuple(pair) for pair in config['renaming_conventions']]


def default_config_file(homedir: Path) -> Path:
    """The config file Sophios uses when the user does not name one."""
    return homedir / 'wic' / 'global_config.json'


def generate_config(homedir: Path) -> Json:
    """Writes the default config, adapters and examples under the selected home directory.

    Args:
        homedir (Path): The home directory whose wic directory will hold the config and resources

    Returns:
        Json: The config json object with absolute filepaths
    """
    # The packaged config names no domain search paths (mm-workflows and the like); its paths are read against homedir.
    config = read_config_from_disk(Path(__file__).parent / 'config_basic.json', base_dir=homedir)
    config_file = default_config_file(homedir)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(config, indent=4, sort_keys=True), encoding='utf-8')
    move_adapters_and_examples(config)
    return config


def get_config(config_file: Path | None, homedir: Path) -> Json:
    """Returns the config json object with absolute paths

    A config file the user names is only ever read. Without one, the default
    under homedir is used, and generated first if it does not exist yet.

    Args:
        config_file (Path | None): The user specified config file, if any
        homedir (Path): The user's home directory

    Raises:
        FileNotFoundError: If config_file does not exist.

    Returns:
        Json: The config json object with absolute filepaths
    """
    if config_file is None:
        config_file = default_config_file(homedir)
        if not config_file.exists():
            config = generate_config(homedir)
            print(f'default config file : {config_file} generated')
            return config
    return read_config_from_disk(config_file)


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


def read_config_from_disk(config_file: Path, base_dir: Path | None = None) -> Json:
    """Returns the config json object from config_file with absolute paths

    Args:
        config_file (Path): The path of json file where it is to be read from
        base_dir (Path | None): Base for relative search paths; defaults to the working directory

    Returns:
        Json: The config json object with absolute filepaths

    Raises:
        TypeError: A namespace under ``search_paths_cwl`` or ``search_paths_wic`` is not a list of paths.
    """
    # config_file can contain absolute or relative paths
    config: Json = json.loads(config_file.read_text(encoding='utf-8'))
    base_dir = Path.cwd() if base_dir is None else base_dir.absolute()
    conf_tags = ['search_paths_cwl', 'search_paths_wic']
    for tag in conf_tags:
        sub_config = copy.deepcopy(config[tag])
        for ns in sub_config:
            if not isinstance(sub_config[ns], list):
                raise TypeError(f'{tag}.{ns} must be a list of paths, not {type(sub_config[ns]).__name__}')
            sub_config[ns] = [str(base_dir / path) for path in sub_config[ns]]
        config[tag] = sub_config
    return config
