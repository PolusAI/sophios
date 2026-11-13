# Copies a file under a name.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: cp
inputs:
  file: {type: File, inputBinding: {position: 1}}
  name: {type: string, inputBinding: {position: 2}}
outputs:
  tagged: {type: File, outputBinding: {glob: $(inputs.name)}}
