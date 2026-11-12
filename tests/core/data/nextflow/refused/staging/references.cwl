cwlVersion: v1.2
class: CommandLineTool
doc: References where the target does not take them; a plain File or Directory reference is the object, `.path` is
  absolute, and a glob takes a `.path` only of an entry.
baseCommand: 'true'
inputs:
  f: File
  d: Directory
outputs:
  plain: {type: File, outputBinding: {glob: $(inputs.f)}}
  suffixed: {type: File, outputBinding: {glob: $(inputs.f).out}}
  path: {type: File, outputBinding: {glob: $(inputs.f.path)}}
  path_suffixed: {type: File, outputBinding: {glob: $(inputs.f.path).out}}
  inside: {type: File, outputBinding: {glob: $(inputs.d)/keep.txt}}
  path_inside: {type: File, outputBinding: {glob: $(inputs.d.path)/keep.txt}}
stdin: $(inputs.f)
stdout: $(inputs.f.path).log
stderr: $(inputs.f.path)
