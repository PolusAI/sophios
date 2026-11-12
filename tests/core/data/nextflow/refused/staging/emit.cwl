cwlVersion: v1.2
class: CommandLineTool
doc: Write sorted.txt.
baseCommand: [echo, b]
stdout: sorted.txt
inputs: {}
outputs:
  out: {type: File, outputBinding: {glob: sorted.txt}}
