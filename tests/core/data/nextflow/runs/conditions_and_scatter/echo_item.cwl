cwlVersion: v1.2
class: CommandLineTool
doc: Write one item to a.txt.
baseCommand: echo
inputs:
  item:
    type: string
    inputBinding: {position: 1}
stdout: a.txt
outputs:
  out:
    type: File
    outputBinding: {glob: a.txt}
