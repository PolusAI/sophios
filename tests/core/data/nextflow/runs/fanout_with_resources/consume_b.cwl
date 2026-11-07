{cwlVersion: v1.2, class: CommandLineTool, baseCommand: cat, inputs: {source: {type: File, inputBinding: {position:
 1}}}, stdout: b.txt, outputs: {result: {type: File, outputBinding: {glob: b.txt}}}}
