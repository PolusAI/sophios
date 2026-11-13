# A CWL Workflow, which a Python Step cannot run as its clt_path.
class: Workflow
cwlVersion: v1.2
inputs: {f: File}
outputs: {said: {type: File, outputSource: print/out}}
steps: {print: {run: cat.cwl, in: {f: f}, out: [out]}}
