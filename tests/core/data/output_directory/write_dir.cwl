cwlVersion: v1.2
class: CommandLineTool
# A plugin-style tool: it writes into a Directory it is given and captures it. It declares no
# InlineJavascriptRequirement, so any `${...}` written into it would not run.
requirements:
  InitialWorkDirRequirement:
    listing:
      - entry: $(inputs.outDir)
        writable: true
baseCommand: touch
arguments:
  - valueFrom: $(inputs.outDir.basename)/done.txt
inputs:
  outDir:
    type: Directory
outputs:
  outDir:
    type: Directory
    outputBinding:
      glob: $(inputs.outDir.basename)
