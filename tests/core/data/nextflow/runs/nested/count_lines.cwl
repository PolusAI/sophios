cwlVersion: v1.2
class: CommandLineTool
doc: Count the lines of the source, read from standard input, into count.txt.
baseCommand: [wc, -l]
inputs:
  source: File
stdin: $(inputs.source.path)
stdout: count.txt
outputs:
  count:
    type: File
    outputBinding: {glob: count.txt}
