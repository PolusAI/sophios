cwlVersion: v1.2
class: CommandLineTool
doc: Print ok; `go` and `n` are read only by the condition of the step.
baseCommand: [echo, ok]
inputs:
  go: boolean
  n: int
stdout: out.txt
outputs:
  result:
    type: string
    outputBinding:
      glob: out.txt
      loadContents: true
      outputEval: $(self[0].contents)
