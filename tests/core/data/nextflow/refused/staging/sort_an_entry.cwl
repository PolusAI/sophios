cwlVersion: v1.2
class: CommandLineTool
doc: Sort the entry sorted.txt into sorted.txt.
requirements:
  InitialWorkDirRequirement: {listing: [{entry: $(inputs.src), entryname: sorted.txt}]}
baseCommand: [sort, sorted.txt]
stdout: sorted.txt
inputs:
  src: File
outputs:
  out: {type: File, outputBinding: {glob: sorted.txt}}
