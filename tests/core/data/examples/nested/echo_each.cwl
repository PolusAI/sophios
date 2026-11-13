cwlVersion: v1.2
class: CommandLineTool
doc: Write one item to out.txt.
baseCommand: echo
inputs:
  item:
    type: string
    inputBinding: {position: 1}
stdout: out.txt
outputs:
  result:
    type: File
    outputBinding: {glob: out.txt}
