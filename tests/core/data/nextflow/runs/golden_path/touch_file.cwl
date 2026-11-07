{cwlVersion: v1.2, class: CommandLineTool, baseCommand: touch, inputs: {filename: {type: string, inputBinding:
 {position: 1}}}, outputs: {result: {type: File, outputBinding: {glob: $(inputs.filename)}}}}
