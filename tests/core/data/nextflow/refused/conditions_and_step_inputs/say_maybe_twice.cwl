cwlVersion: v1.2
class: CommandLineTool
doc: Echo an optional word with its prefix, and again on its own, where its absence would be written.
baseCommand: echo
arguments: [{position: 2, valueFrom: $(inputs.word)}]
inputs:
  word: {type: 'string?', inputBinding: {position: 1, prefix: --word}}
outputs: {}
