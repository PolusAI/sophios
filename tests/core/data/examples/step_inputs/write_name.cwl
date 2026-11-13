cwlVersion: v1.2
class: CommandLineTool
doc: Write one name, and a newline, to out.txt.
baseCommand: echo
inputs:
  name:
    type: string
    inputBinding: {position: 1}
stdout: out.txt
outputs:
  file:
    type: File
    outputBinding: {glob: out.txt}
