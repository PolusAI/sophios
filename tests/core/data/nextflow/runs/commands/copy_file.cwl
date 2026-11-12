cwlVersion: v1.2
class: CommandLineTool
doc: Copy the source to copy.txt.
baseCommand: cp
inputs:
  source:
    type: File
    inputBinding: {position: 1}
arguments:
- {position: 2, valueFrom: copy.txt}
outputs:
  result:
    type: File
    outputBinding: {glob: copy.txt}
