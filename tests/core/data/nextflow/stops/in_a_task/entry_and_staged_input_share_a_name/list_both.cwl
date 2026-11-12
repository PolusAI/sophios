cwlVersion: v1.2
class: CommandLineTool
doc: List the directory `b`, with `a` staged as a writable entry of the working directory.
requirements:
  InitialWorkDirRequirement:
    listing:
    - entry: $(inputs.a)
      writable: true
baseCommand: ls
inputs:
  a: Directory
  b:
    type: Directory
    inputBinding: {position: 1}
stdout: listing.txt
outputs:
  listing:
    type: File
    outputBinding: {glob: listing.txt}
