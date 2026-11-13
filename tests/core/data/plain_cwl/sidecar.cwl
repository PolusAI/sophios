# A `wic:` block makes this a document to compile.
wic:
  graphviz:
    label: x
cwlVersion: v1.2
class: Workflow
inputs: {message: string}
outputs: {}
steps:
  say: {run: say.cwl, in: {message: message}, out: [said]}
