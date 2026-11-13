cwlVersion: v1.2
class: CommandLineTool
baseCommand: alt
inputs: {name: string}
outputs: {file: {type: File, outputBinding: {glob: f}}}
