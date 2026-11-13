# Writes into the output-target Directory `outDir`, an entry it may write, captured as its output; reads `source`.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: touch
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
- $(inputs.outDir.basename)/result.txt
