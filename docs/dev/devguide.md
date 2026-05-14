# Developer Guide

See [algorithms](algorithms.md) for a description of the compilation algorithms and some high-level implementation considerations. I hope you like recursion! ;)

## Coding Standards

See [coding standards](codingstandards.md)

## Git Etiquette

See [git etiquette](gitetiquette.md)

## Known Issues

### Globbing Unexpected File Order

If a workflow step generates a file with some association between a filename and numerical value, if the numerical values are being extracted in order of each row/column from the file then there is no guarantee that using glob to retrieve files will produce the same order consistent with the extracted numerical value array. Another example is illustrated below without scattering, where using glob will result in inconsistency between input order and output order of files. With scattering it is possible in some cases to induce the correct output ordering consistent with input order, however it is best practice to adopt reading filename indices from an output file rather than using glob to ensure consistent order. This way developer does not have to think about which cases glob might or might now work in.

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

As can be seen from the output json blob the order returned is not the same as input order.


## Partial Failures
When the partial failures feature is enabled although the subprocess for the workflow step itself will pass, the post-processing javascript can potentially crash as seen below. The Sophios compiler only semantically understands Sophios/CWL. It is theoretically impossible to correct mistakes in the embedded JS of any arbitrary workflow. The corresponding cwl snippet is also shown.
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
To fix this the developer needs to add a javascript snippet to check if the self object being globbed exists, shown below.
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
When adding new .cwl or .wic files its best to remove the .wic folder containing paths to .cwl and .yml files
```
rm -r ~/wic
```

## Singularity
When building images with Singularity its best to clean the cache to avoid potential errors with cwltool or cwl-docker-extract.
```
singularity cache clean
```

## Toil
When working with toil be sure to clean the working state as well as the configuration file, otherwise if you change input flags the configuration file will not be updated.
```
toil clean
rm -r ~/.toil
```
