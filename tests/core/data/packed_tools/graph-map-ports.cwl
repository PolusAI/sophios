cwlVersion: v1.2
$namespaces: {edam: https://edamontology.org/}
$graph:
- {id: '#main', class: CommandLineTool, baseCommand: echo, inputs: {text: {type: string, inputBinding: {position: 1}}},
   outputs: {out: {type: stdout, format: 'edam:format_1964'}}}
