cwlVersion: v1.2
class: CommandLineTool
doc: Write the message, and a newline, to message.txt.
baseCommand: echo
inputs:
  message:
    type: string
    inputBinding: {position: 1}
stdout: message.txt
outputs:
  result:
    type: File
    outputBinding: {glob: message.txt}
