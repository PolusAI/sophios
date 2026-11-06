cwlVersion: v1.2
$namespaces: {edam: https://edamontology.org/}
id: '#main'
class: CommandLineTool
baseCommand: echo
inputs: [{id: '#main/text', type: string, inputBinding: {position: 1}}]
outputs: [{id: '#main/out', type: stdout, format: 'edam:format_1964'}]
