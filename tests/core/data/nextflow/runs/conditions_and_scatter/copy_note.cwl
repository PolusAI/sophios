cwlVersion: v1.2
class: CommandLineTool
doc: Copy the note to <name>.txt.
baseCommand: cp
inputs:
  note:
    type: File
    inputBinding: {position: 1}
  name: string
arguments:
- {position: 2, valueFrom: $(inputs.name).txt}
outputs:
  copy:
    type: File
    outputBinding: {glob: $(inputs.name).txt}
