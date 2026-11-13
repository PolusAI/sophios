cwlVersion: v1.2
class: CommandLineTool
$namespaces: {edam: https://edamontology.org/}
baseCommand: cat
stdout: used.txt
inputs:
  file: {type: File, format: edam:format_2330, inputBinding: {position: 1}}
outputs:
  out: {type: File, outputBinding: {glob: used.txt}}
