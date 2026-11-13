cwlVersion: v1.2
class: CommandLineTool
doc: Fail. A step running this is one its condition must skip, so a run that starts it does not complete.
baseCommand: [sh, -c, 'echo "a skipped step was started" >&2; exit 1']
inputs:
  go: boolean
  n: int
stdout: out.txt
outputs:
  result:
    type: string
    outputBinding:
      glob: out.txt
      loadContents: true
      outputEval: $(self[0].contents)
