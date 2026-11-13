# Its `$schemas` stay as written: packing never goes to the network for an ontology.
cwlVersion: v1.2
class: CommandLineTool
$schemas: ['http://schemas.invalid/ontology.owl']
baseCommand: echo
inputs: {message: {type: string, default: hi, inputBinding: {position: 1}}}
outputs: {out: {type: stdout}}
stdout: out.txt
