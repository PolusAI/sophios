cwlVersion: v1.2
class: CommandLineTool
doc: Streams that name the File object, under its own basename, or a gzip beside its path.
requirements:
  InitialWorkDirRequirement:
    listing: [$(inputs.linked)]
baseCommand: 'true'
inputs:
  f: File
  linked: File
outputs: {}
stdin: $(inputs.f.path).gz
stdout: $(inputs.f)
stderr: $(inputs.linked.basename).log
