# A tool whose output is a directory it is given the name of, and creates.
class: CommandLineTool
cwlVersion: v1.2
requirements:
  InitialWorkDirRequirement:
    listing:
      - entry: $(inputs.outDir)
        writable: true
  InlineJavascriptRequirement: {}
baseCommand: [bash, -lc]
arguments:
  - valueFrom: "touch $(inputs.outDir.basename)/done.txt"
inputs:
  outDir:
    type: Directory
    inputBinding:
      prefix: --outDir
outputs:
  outDir:
    type: Directory
    outputBinding:
      glob: $(inputs.outDir.basename)
