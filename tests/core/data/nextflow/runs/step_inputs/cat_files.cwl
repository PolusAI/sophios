cwlVersion: v1.2
class: CommandLineTool
doc: Concatenate the files, in the order given, into all.txt.
baseCommand: cat
inputs:
  files:
    type: File[]
    inputBinding: {position: 1}
stdout: all.txt
outputs:
  line:
    type: File
    outputBinding: {glob: all.txt}
