cwlVersion: v1.2
class: CommandLineTool
doc: Stage the source under its own basename, and print it to out.txt.
requirements:
  InitialWorkDirRequirement:
    listing:
    - $(inputs.source)
baseCommand: cat
inputs:
  source:
    type: File
    inputBinding: {position: 1}
stdout: out.txt
outputs:
  result:
    type: File
    outputBinding: {glob: out.txt}
