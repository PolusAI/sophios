class: CommandLineTool
cwlVersion: v1.0

# Appends str as one line to a copy of file, and returns the copy.
requirements:
  ShellCommandRequirement: {}
  InitialWorkDirRequirement:
    listing:
    - entry: $(inputs.file)
      writable: true

inputs:
  str:
    type: string
  file:
    type: File

arguments:
- printf
- '%s\n'
- $(inputs.str)
- valueFrom: ">>"
  shellQuote: false
- $(inputs.file.basename)

outputs:
  file:
    type: File
    outputBinding:
      glob: $(inputs.file.basename)
