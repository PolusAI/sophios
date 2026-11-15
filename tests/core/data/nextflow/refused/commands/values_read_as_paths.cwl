cwlVersion: v1.2
class: CommandLineTool
doc: .path and .basename of values that are not a File or Directory, and a File object embedded in text.
baseCommand: echo
arguments: ['--input=$(inputs.f)']
stdout: $(inputs.s.path).log
inputs:
  s: {type: string, inputBinding: {prefix: -s, valueFrom: $(inputs.s.path)}}
  f: File
outputs:
  result: {type: File, outputBinding: {glob: $(inputs.s.basename).txt}}
