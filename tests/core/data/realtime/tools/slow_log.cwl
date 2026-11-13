# Writes out.log one line a second for six seconds: long enough to be watched while it grows.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: [bash, -c, 'for i in 1 2 3 4 5 6; do echo $i >> out.log; sleep 1; done']
inputs: {}
outputs:
  log: {type: File, outputBinding: {glob: out.log}}
