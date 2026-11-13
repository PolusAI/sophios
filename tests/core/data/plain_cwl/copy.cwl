cwlVersion: v1.2
class: CommandLineTool
baseCommand: cat
inputs:
  text:
    type: File
    inputBinding: {position: 1}
stdout: copied.txt
outputs:
  copied: stdout
