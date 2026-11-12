cwlVersion: v1.2
class: CommandLineTool
doc: Write message.txt.
baseCommand: [echo, hi]
inputs: {}
stdout: message.txt
outputs:
  result: {type: File, outputBinding: {glob: message.txt}}
