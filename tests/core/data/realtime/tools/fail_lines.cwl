# Fails, given a log.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: ['false']
inputs:
  log: {type: File, inputBinding: {position: 1}}
stdout: count.txt
outputs:
  count: {type: stdout}
