cwlVersion: v1.2
class: CommandLineTool
doc: Echo one word.
baseCommand: echo
inputs:
  word: {type: string, inputBinding: {position: 1}}
outputs: {}
