cwlVersion: v1.2
class: CommandLineTool
baseCommand: echo
inputs:
  pair:
    type:
      type: record
      name: Pair
      fields: {first: string, second: int}
outputs: {}
