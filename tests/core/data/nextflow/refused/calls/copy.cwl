cwlVersion: v1.2
class: CommandLineTool
doc: Copy the source to copy.txt.
baseCommand: cp
arguments: [{position: 2, valueFrom: copy.txt}]
inputs:
  source: {type: File, inputBinding: {position: 1}}
outputs:
  result: {type: File, outputBinding: {glob: copy.txt}}
