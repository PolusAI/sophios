cwlVersion: v1.2
class: CommandLineTool
doc: Bindings of booleans the target cannot write as cwltool does, and a binding's loadContents.
baseCommand: echo
arguments:
- {position: 0, valueFrom: x, loadContents: true}
inputs:
  bools: {type: 'boolean[]', inputBinding: {position: 1, prefix: -x}}            # cwltool writes -x alone
  bare_bools: {type: {type: array, items: boolean}, inputBinding: {position: 2}}  # and nothing at all
  maybe_bools: {type: {type: array, items: ['null', boolean]}, inputBinding: {position: 3, prefix: -x}}
  aliased: {type: boolean, inputBinding: {position: 5, prefix: --aliased, valueFrom: $(inputs.other)}}
  literal: {type: boolean, inputBinding: {position: 6, prefix: --literal, valueFrom: 'true'}}
  suffixed: {type: boolean, inputBinding: {position: 7, prefix: --suffixed, valueFrom: $(inputs.suffixed.basename)}}
  other: boolean
outputs: {}
