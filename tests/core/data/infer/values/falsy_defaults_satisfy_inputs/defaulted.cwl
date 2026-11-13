cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs:
  number: {type: int, default: 0}
  flag: {type: boolean, default: false}
  text: {type: string, default: ''}
  items: {type: 'string[]', default: []}
outputs: {}
