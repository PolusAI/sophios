cwlVersion: v1.2
class: CommandLineTool
# The same tool as write_dir.cwl with its requirements, inputs and outputs written as lists, as the Python
# API's tool builder writes them, and its input read as inputs["outDir"].
requirements:
  - class: InitialWorkDirRequirement
    listing:
      - entry: $(inputs["outDir"])
        writable: true
baseCommand: touch
arguments:
  - valueFrom: $(inputs.outDir.basename)/done.txt
inputs:
  - id: outDir
    type: Directory
outputs:
  - id: outDir
    type: Directory
    outputBinding:
      glob: $(inputs["outDir"].basename)
