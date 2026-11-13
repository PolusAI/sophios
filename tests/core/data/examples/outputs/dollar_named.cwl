cwlVersion: v1.2
class: CommandLineTool
doc: Make a file whose name holds a dollar that starts no reference.
baseCommand: [touch, out$name.txt]
inputs: {}
outputs:
  result:
    type: File
    outputBinding: {glob: out$name.txt}
