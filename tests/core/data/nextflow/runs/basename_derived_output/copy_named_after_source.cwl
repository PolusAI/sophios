{cwlVersion: v1.2, class: CommandLineTool, baseCommand: cp, inputs: {source: {type: File, inputBinding: {position: 1}}},
 arguments: [{position: 2, valueFrom: $(inputs.source.basename).copy}], outputs: {result: {type: File, outputBinding:
 {glob: $(inputs.source.basename).copy}}}}
