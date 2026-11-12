cwlVersion: v1.2
class: CommandLineTool
doc: Make c.txt; `i` is read only by the condition of the step.
baseCommand: [touch, c.txt]
inputs:
  i: int
outputs:
  out:
    type: File
    outputBinding: {glob: c.txt}
