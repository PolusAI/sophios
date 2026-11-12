cwlVersion: v1.2
class: CommandLineTool
doc: A binding whose valueFrom computes from another input, the tool's one problem.
baseCommand: echo
inputs:
  a: {type: int, inputBinding: {position: 1, valueFrom: $(Math.round(inputs.b))}}
  b: float
outputs: {}
