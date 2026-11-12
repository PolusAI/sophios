cwlVersion: v1.2
class: CommandLineTool
doc: Print the text with no newline; its step runs only when `go` is true, so its output may be null.
baseCommand: printf
inputs:
  text:
    type: string
    inputBinding: {position: 1}
  go: boolean
stdout: said.txt
outputs:
  said:
    type: string
    outputBinding:
      glob: said.txt
      loadContents: true
      outputEval: $(self[0].contents)
