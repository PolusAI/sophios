cwlVersion: v1.2
class: CommandLineTool
doc: Make an empty file of the name given.
baseCommand: touch
inputs:
  filename:
    type: string
    inputBinding: {position: 1}
outputs:
  result:
    type: File
    outputBinding: {glob: $(inputs.filename)}
