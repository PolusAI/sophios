cwlVersion: v1.2
class: CommandLineTool
doc: Concatenate two files, `f` then `r`, into b.txt.
baseCommand: cat
inputs:
  f:
    type: File
    inputBinding: {position: 1}
  r:
    type: File
    inputBinding: {position: 2}
stdout: b.txt
outputs:
  out:
    type: File
    outputBinding: {glob: b.txt}
