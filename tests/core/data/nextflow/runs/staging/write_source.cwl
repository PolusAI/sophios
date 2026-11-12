cwlVersion: v1.2
class: CommandLineTool
doc: Write the message, and a newline, to lines.txt.
baseCommand: echo
inputs:
  message:
    type: string
    inputBinding: {position: 1}
stdout: lines.txt
outputs:
  result:
    type: File
    outputBinding: {glob: lines.txt}
