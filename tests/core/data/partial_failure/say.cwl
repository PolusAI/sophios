# An output written as its type alone, a stream the tool names.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
stdout: said.txt
inputs: {message: string}
outputs: {said: stdout}
