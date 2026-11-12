cwlVersion: v1.2
class: CommandLineTool
doc: Copy the sources into out/.
baseCommand: cp
arguments: [{position: 2, valueFrom: out}]
inputs:
  source: {type: 'File[]', inputBinding: {position: 1}}
outputs:
  result: {type: 'File[]', outputBinding: {glob: 'out/*'}}
