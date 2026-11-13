cwlVersion: v1.2
class: CommandLineTool
baseCommand: [touch, out.log]
inputs: {}
outputs: {log: {type: File, outputBinding: {glob: out.log}}}
