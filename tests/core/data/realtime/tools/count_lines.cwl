# Counts the lines of a log.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: [wc, -l]
inputs:
  log: {type: File, inputBinding: {position: 1}}
stdout: count.txt
outputs:
  count: {type: stdout}
