cwlVersion: v1.2
class: CommandLineTool
doc: Echo a list of words.
baseCommand: echo
inputs:
  words: {type: 'string[]', inputBinding: {position: 1}}
outputs: {}
