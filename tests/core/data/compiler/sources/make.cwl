cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs: {seed: string}
outputs: {value: {type: File, outputBinding: {glob: v}}}
