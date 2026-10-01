"""The discovery config: a file the user names is only read; the default is generated once."""
from pathlib import Path

import pytest

from sophios import input_output as io
from sophios import main as cli


@pytest.mark.fast
def test_a_config_file_that_does_not_exist_leaves_the_users_config_alone(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A typo in --config_file used to write a fresh default config over the user's own."""
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    users_config = tmp_path / 'wic' / 'global_config.json'
    users_config.parent.mkdir()
    users_config.write_text('{"mine": true}', encoding='utf-8')
    (tmp_path / 'w.wic').write_text('steps:\n  touch:\n', encoding='utf-8')
    monkeypatch.setattr('sys.argv', ['sophios', '--yaml', 'w.wic', '--generate_cwl_workflow',
                                     '--homedir', str(tmp_path), '--config_file', 'typo.json'])

    with pytest.raises(SystemExit) as caught:
        cli.main()

    assert caught.value.code == 2
    assert 'typo.json' in capsys.readouterr().err
    assert users_config.read_text(encoding='utf-8') == '{"mine": true}'


@pytest.mark.fast
def test_the_default_config_is_generated_under_homedir_once_then_only_read(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without --config_file, the first run writes the default; after that, the user's edits are what is read."""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    homedir = tmp_path / 'homedir'

    generated = io.get_config(None, homedir)

    default = io.default_config_file(homedir)
    assert io.read_config_from_disk(default) == generated
    default.write_text('{"search_paths_cwl": {}, "search_paths_wic": {}}', encoding='utf-8')
    assert io.get_config(None, homedir) == {'search_paths_cwl': {}, 'search_paths_wic': {}}
