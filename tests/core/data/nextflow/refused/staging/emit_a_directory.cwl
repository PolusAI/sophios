cwlVersion: v1.2
class: CommandLineTool
doc: Write the directory outdir.
baseCommand: [mkdir, outdir]
inputs: {}
outputs:
  out: {type: Directory, outputBinding: {glob: outdir}}
