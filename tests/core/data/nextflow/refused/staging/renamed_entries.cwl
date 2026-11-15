cwlVersion: v1.2
class: CommandLineTool
doc: An input staged twice, and two inputs staged under one name.
requirements:
  InitialWorkDirRequirement:
    listing:
    - {entry: $(inputs.twice), entryname: one.txt}
    - {entry: $(inputs.twice), entryname: two.txt}
    - {entry: $(inputs.first), entryname: same.txt}
    - {entry: $(inputs.second), entryname: same.txt}
baseCommand: cat
inputs:
  twice: File
  first: File
  second: File
outputs: {}
