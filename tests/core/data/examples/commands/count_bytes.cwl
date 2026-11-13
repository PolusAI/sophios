cwlVersion: v1.2
class: CommandLineTool
doc: Count the bytes of the source into count.txt.
baseCommand: [wc, -c]
inputs:
  source: File
stdin: $(inputs.source.path)
stdout: count.txt
outputs:
  result:
    type: File
    outputBinding: {glob: count.txt}
