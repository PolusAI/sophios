# Prints a line on each stream, and fails when told to.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: [sh, -c]
arguments: ['echo "$0 out"; echo "$0 err" >&2; exit $1']
inputs:
  word: {type: string, default: say, inputBinding: {position: 1}}
  status: {type: int, default: 0, inputBinding: {position: 2}}
outputs: {}
