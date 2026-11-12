cwlVersion: v1.2
class: CommandLineTool
doc: References to a linked entry and a writable one, which name the object or its absolute path.
requirements:
  InitialWorkDirRequirement:
    listing: [$(inputs.f), {entry: $(inputs.d), writable: true}]
baseCommand: 'true'
inputs:
  f: File
  d: Directory
outputs:
  plain: {type: File, outputBinding: {glob: $(inputs.f)}}
  writable: {type: Directory, outputBinding: {glob: $(inputs.d)}}
stdin: $(inputs.d.path)
stdout: $(inputs.f.path)
stderr: $(inputs.f)
