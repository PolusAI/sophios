# Prints its `config.txt` default, beside it.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: cat
stdout: out.txt
inputs:
  config: {type: File, default: {class: File, location: config.txt}, inputBinding: {position: 1}}
outputs:
  out: {type: stdout}
