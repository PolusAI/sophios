"""A script whose annotations are all the generator reads: it writes the file its input names."""
from pathlib import Path

inputs = {'name': {'type': 'string', 'format': 'edam:format_2330'}}
outputs = {'file': ('$(inputs.name)', {'type': 'File', 'format': 'edam:format_2330'})}


def main(name: str) -> None:
    """Write the empty file `name`."""
    Path(name).touch()
