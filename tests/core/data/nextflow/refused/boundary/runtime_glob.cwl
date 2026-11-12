cwlVersion: v1.2
class: CommandLineTool
doc: Collect a file by a glob that reads the runtime, not an input.
baseCommand: [touch, result.txt]
inputs: {}
outputs:
  result: {type: File, outputBinding: {glob: $(runtime.outdir)/result.txt}}
