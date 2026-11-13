cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs:
  bare: string?
  text: {type: 'string?', doc: the file name, label: File name}
  listed: {type: 'string?', doc: [one, two]}
outputs: {}
