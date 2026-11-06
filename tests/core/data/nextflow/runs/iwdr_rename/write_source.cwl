{cwlVersion: v1.2, class: CommandLineTool, baseCommand: echo, inputs: {message: {type: string, inputBinding:
 {position: 1}}}, stdout: original_name.txt, outputs: {result: {type: File, outputBinding: {glob: original_name.txt}}}}
