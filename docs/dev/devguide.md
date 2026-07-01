# Developer Guide

See [algorithms](algorithms.md) for a description of the compilation
algorithms and high-level implementation considerations.

## Coding Standards

See [coding standards](codingstandards.md)

## Git Etiquette

See [git etiquette](gitetiquette.md)

## Runtime Notes And Known Issues

### Globbing Unexpected File Order

Do not rely on `glob` order when output order matters. CWL runners may return
globbed files in a different order than the input values that produced them.

When order is semantically important, write an explicit manifest or index file
from the tool and read that file downstream.

The simplified example below shows the problem:

```
input: [3, 2, 1]  # Here is the order of the input array.
```

```
#!/usr/bin/env cwl-runner
cwlVersion: v1.2
class: CommandLineTool
baseCommand: ["python", "example.py"]

requirements:
- class: InitialWorkDirRequirement
  listing:
  # See https://www.commonwl.org/user_guide/topics/creating-files-at-runtime.html
    - entryname: example.py
      entry: |
        import sys
        from pathlib import Path

        for arg in sys.argv[1:]:
            Path(f'{arg}.txt').touch()

inputs:
  input:
    type: Any[]
    inputBinding:
      position: 0

outputs:
  output:
    type: File[]
    outputBinding:
      glob: "*.txt"
```

```
cwltool touch_array.cwl touch_array_inputs.yml


INFO [job test.cwl] /tmp/0x0q86bg$ python \
    example.py \
    3 \
    2 \
    1
INFO [job test.cwl] completed success
{
    "output": [
        {
            "location": "file:///home/walkerbd/sophios/cwl_adapters/1.txt",
            "basename": "1.txt",
            "class": "File",
            "checksum": "sha1$da39a3ee5e6b4b0d3255bfef95601890afd80709",
            "size": 0,
            "path": "/home/walkerbd/sophios/cwl_adapters/1.txt"
        },
        {
            "location": "file:///home/walkerbd/sophios/cwl_adapters/2.txt",
            "basename": "2.txt",
            "class": "File",
            "checksum": "sha1$da39a3ee5e6b4b0d3255bfef95601890afd80709",
            "size": 0,
            "path": "/home/walkerbd/sophios/cwl_adapters/2.txt"
        },
        {
            "location": "file:///home/walkerbd/sophios/cwl_adapters/3.txt",
            "basename": "3.txt",
            "class": "File",
            "checksum": "sha1$da39a3ee5e6b4b0d3255bfef95601890afd80709",
            "size": 0,
            "path": "/home/walkerbd/sophios/cwl_adapters/3.txt"
        }
    ]
```

The output array is sorted by filename, not by the original input order.


## Partial Failures

When partial failures are enabled, the command for a workflow step can succeed
while a CWL JavaScript post-processing expression still fails. Sophios can
compile the CWL structure, but it cannot automatically repair arbitrary
JavaScript embedded in a tool.

For example, this output expression assumes that `self[0]` exists:
```
outputs:

  topology_changed:
    type: boolean
    outputBinding:
      glob: valid.txt
      loadContents: true
      outputEval: |
        ${
          // Read the contents of the file
          const lines = self[0].contents.split("\n");
          // Read boolean value from the first line
          const valid = lines[0].trim() === "True";
          return valid;
        }
```
```
stdout was: ''
stderr was: 'evalmachine.<anonymous>:45
  const lines = self[0].contents.split("\n");
                        ^
TypeError: Cannot read properties of undefined (reading 'contents')
```
Fix the tool by checking that the globbed object exists before reading it:
```
outputs:

  topology_changed:
    type: boolean
    outputBinding:
      glob: valid.txt
      loadContents: true
      outputEval: |
        ${
          // check if self[0] exists
          if (!self[0]) {
            return null;
          }
          // Read the contents of the file
          const lines = self[0].contents.split("\n");
          // Read boolean value from the first line
          const valid = lines[0].trim() === "True";
          return valid;
        }
```

## Workflow Development

When adding new `.cwl` or `.wic` files, regenerate the discovery config and
schemas when editor validation or tool discovery looks stale:

```bash
sophios --generate_config
sophios --generate_schemas
```

Sophios uses `~/wic/global_config.json` by default. Inspect that file before
deleting `~/wic`, because it may contain local search paths you want to keep.

## Singularity

When building images with Singularity, clear the cache if `cwltool` or
`cwl-docker-extract` reports stale-image or cache-related failures:

```
singularity cache clean
```

## Toil

When working with Toil, stale job stores can preserve older runtime state. If
changing workflow inputs or runner flags produces surprising behavior, clean the
Toil state:

```
toil clean
rm -r ~/.toil
```
