cwlVersion: v1.2
class: CommandLineTool
doc: Copy `source` into the directory it is told to write into, which it stages writable and gives back.
baseCommand: cp
requirements:
  InitialWorkDirRequirement:
    listing:
    - entry: $(inputs.outDir)
      writable: true
inputs:
  outDir:
    type: Directory
  source:
    type: File
outputs:
  outDir:
    type: Directory
    outputBinding:
      glob: $(inputs.outDir.basename)
arguments:
- $(inputs.source)
- $(inputs.outDir.basename)/copied.txt
