# A relative default, and an output whose format inference reads as written: `edam:format_2330`.
cwlVersion: v1.2
class: CommandLineTool
$namespaces: {edam: https://edamontology.org/}
baseCommand: cat
stdout: made.txt
inputs:
  seed: {type: File, default: {class: File, location: seed.txt}, inputBinding: {position: 1}}
outputs:
  out: {type: File, format: edam:format_2330, outputBinding: {glob: made.txt}}
