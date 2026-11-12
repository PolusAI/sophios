cwlVersion: v1.2
class: CommandLineTool
doc: Make n.txt; `n` is read only by the condition of the step.
baseCommand: [touch, n.txt]
inputs:
  n: int
outputs:
  out:
    type: File
    outputBinding: {glob: n.txt}
