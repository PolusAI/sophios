# Prints the file it is given; it runs in no container.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: cat
inputs:
  f:
    type: File
    inputBinding: {position: 1}
outputs:
  out:
    type: File
    outputBinding: {glob: out.txt}
stdout: out.txt
