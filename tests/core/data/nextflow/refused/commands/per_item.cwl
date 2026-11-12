cwlVersion: v1.2
class: CommandLineTool
doc: Files each bound with its own prefix, and lists of words.
baseCommand: echo
inputs:
  files: {type: {type: array, items: {type: File, inputBinding: {prefix: -I}}}, inputBinding: {position: 1}}
outputs: {}
