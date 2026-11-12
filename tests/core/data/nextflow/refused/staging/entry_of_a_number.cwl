cwlVersion: v1.2
class: CommandLineTool
doc: An entry that is neither a reference nor a Dirent; a malformed listing stages what no check can know, so neither
  the glob of the entry beside it nor the link it is under is checked.
requirements:
  InitialWorkDirRequirement: {listing: [{entry: $(inputs.f), entryname: sorted.txt}, 123]}
baseCommand: 'true'
stdout: sorted.txt
inputs:
  f: File
outputs:
  result: {type: File, outputBinding: {glob: $(inputs.f.path)}}
