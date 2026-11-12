cwlVersion: v1.2
class: CommandLineTool
doc: Print the text to copy.txt.
baseCommand: echo
inputs:
  text:
    type: string
    inputBinding: {position: 1}
stdout: copy.txt
outputs:
  result:
    type: File
    outputBinding: {glob: copy.txt}
