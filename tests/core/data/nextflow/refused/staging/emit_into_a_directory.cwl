cwlVersion: v1.2
class: CommandLineTool
doc: Write out/sorted.txt.
baseCommand: [echo, b]
stdout: out/sorted.txt
inputs: {}
outputs:
  out: {type: File, outputBinding: {glob: out/sorted.txt}}
