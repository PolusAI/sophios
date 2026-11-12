cwlVersion: v1.2
class: CommandLineTool
doc: Print the source to read.txt.
baseCommand: cat
inputs:
  source:
    type: File
    inputBinding: {position: 1}
stdout: read.txt
outputs:
  result:
    type: File
    outputBinding: {glob: read.txt}
