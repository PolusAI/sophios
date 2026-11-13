cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
$namespaces: {myns: 'http://other.example/'}
inputs: {g: {type: File, format: 'myns:format_1'}}
outputs: {}
