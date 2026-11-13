cwlVersion: v1.2
class: CommandLineTool
doc: Print the numbers on one line; `prev`, `flag` and `maybe` are read only by the condition of the step.
baseCommand: echo
inputs:
  prev: string?
  flag: boolean
  xs:
    type: int[]
    inputBinding: {position: 1}
  maybe: int?
stdout: out.txt
outputs:
  result:
    type: string
    outputBinding:
      glob: out.txt
      loadContents: true
      outputEval: $(self[0].contents)
