cwlVersion: v1.2
class: CommandLineTool
doc: Write the message to a file whose name holds braces, and give the first match of that name.
baseCommand: echo
inputs:
  message:
    type: string
    inputBinding: {position: 1}
stdout: 'out{1,2}.txt'
outputs:
  result:
    type: File
    outputBinding:
      glob: 'out{1,2}.txt'
      outputEval: $(self[0])
