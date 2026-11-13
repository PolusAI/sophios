cwlVersion: v1.2
class: CommandLineTool
doc: Stage the source as renamed_by_iwdr.txt, the name the command reads, and print it to out.txt.
requirements:
  InitialWorkDirRequirement:
    listing:
    - entry: $(inputs.source)
      entryname: renamed_by_iwdr.txt
baseCommand: cat
arguments: [renamed_by_iwdr.txt]
inputs:
  source: File
stdout: out.txt
outputs:
  result:
    type: File
    outputBinding: {glob: out.txt}
