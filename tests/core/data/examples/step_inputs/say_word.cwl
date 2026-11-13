cwlVersion: v1.2
class: CommandLineTool
doc: Print one word on one line of out.txt.
baseCommand: echo
inputs:
  words:
    type: string
    inputBinding: {position: 1}
stdout: out.txt
outputs:
  line:
    type: File
    outputBinding: {glob: out.txt}
