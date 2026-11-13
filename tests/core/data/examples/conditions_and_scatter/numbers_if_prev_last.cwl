cwlVersion: v1.2
class: CommandLineTool
doc: numbers_if, declaring `prev`, the input an upstream step gives, after the others.
baseCommand: echo
inputs:
  flag: boolean
  xs:
    type: int[]
    inputBinding: {position: 1}
  maybe: int?
  prev: string?
stdout: out.txt
outputs:
  result:
    type: string
    outputBinding:
      glob: out.txt
      loadContents: true
      outputEval: $(self[0].contents)
