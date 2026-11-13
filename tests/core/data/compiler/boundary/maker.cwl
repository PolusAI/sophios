cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
$namespaces: {myns: 'http://tool.example/'}
inputs: {}
outputs: {o: {type: File, format: 'myns:format_1', outputBinding: {glob: o}}}
