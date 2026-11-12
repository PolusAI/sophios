cwlVersion: v1.2
class: CommandLineTool
doc: Two inputs whose names normalize alike.
baseCommand: echo
inputs:
  out-dir: {type: string, default: first}
  out_dir: {type: string, default: second}
outputs: {}
