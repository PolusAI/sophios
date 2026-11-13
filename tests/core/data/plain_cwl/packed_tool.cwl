cwlVersion: v1.2
$graph:
- id: say
  class: CommandLineTool
  baseCommand: echo
  inputs: {message: {type: string, inputBinding: {position: 1}}}
  stdout: said.txt
  outputs: {said: stdout}
- id: main
  class: CommandLineTool
  inputs: {message: string}
  outputs: {said: {type: File, outputSource: say/said}}
  steps:
    say: {run: '#say', in: {message: message}, out: [said]}
