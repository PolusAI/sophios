cwlVersion: v1.2
class: CommandLineTool
doc: List src into outdir/listing.txt.
baseCommand: ls
stdout: outdir/listing.txt
inputs:
  src: {type: Directory, inputBinding: {position: 1}}
outputs:
  out: {type: File, outputBinding: {glob: outdir/listing.txt}}
