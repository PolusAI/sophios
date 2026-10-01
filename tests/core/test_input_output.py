"""Behavioural coverage for `sophios.input_output` config handling."""
import json
from pathlib import Path

import pytest

from sophios import input_output as io


@pytest.mark.fast
@pytest.mark.parametrize('tag', ['search_paths_cwl', 'search_paths_wic'])
def test_a_search_path_given_as_a_string_is_a_type_error(tmp_path: Path, tag: str) -> None:
    """A string is not iterated into characters; the error names the config key."""
    config = tmp_path / 'c.json'
    config.write_text(json.dumps({'search_paths_cwl': {'global': []}, 'search_paths_wic': {'global': []},
                                  tag: {'global': '/whole/disk'}}), encoding='utf-8')
    with pytest.raises(TypeError, match=f'{tag}.global'):
        io.read_config_from_disk(config)
