# No cwlVersion: not a CWL workflow, so a document to compile.
class: Workflow
inputs: {message: string}
outputs: {}
steps:
  say: {run: say.cwl, in: {message: message}, out: [said]}
