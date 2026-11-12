cwlVersion: v1.2
class: CommandLineTool
doc: Echo lists of words.
baseCommand: echo
inputs:
  lists: {type: {type: array, items: {type: array, items: string}}, inputBinding: {position: 1}}
outputs: {}
