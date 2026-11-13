# A Sophios tag makes this a document to compile.
cwlVersion: v1.2
class: Workflow
inputs: {}
outputs: {}
steps:
  say: {run: say.cwl, in: {message: !ii hello}, out: [said]}
