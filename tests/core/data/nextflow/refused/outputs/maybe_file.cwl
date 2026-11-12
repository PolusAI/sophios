cwlVersion: v1.2
class: CommandLineTool
doc: Do nothing; maybe.txt is not made, so the output is null.
baseCommand: 'true'
inputs: {}
outputs:
  o:
    type: File?
    outputBinding: {glob: maybe.txt}
