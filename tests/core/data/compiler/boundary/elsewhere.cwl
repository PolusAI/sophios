cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
$namespaces: {myns: 'http://other.example/', elsens: 'http://else.example/'}
inputs: {g: {type: File, format: 'elsens:format_2'}}
outputs: {}
