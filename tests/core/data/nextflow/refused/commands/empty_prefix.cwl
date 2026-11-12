cwlVersion: v1.2
class: CommandLineTool
doc: A flag whose prefix is empty.
baseCommand: echo
inputs:
  b: {type: boolean, inputBinding: {prefix: ''}}
outputs: {}
