{cwlVersion: v1.2, class: CommandLineTool, requirements: {ResourceRequirement: {coresMin: 2, ramMin: 64}}, baseCommand:
 [printf, '%s', fanout], inputs: {}, stdout: data.txt, outputs: {result: {type: File, outputBinding: {glob: data.txt}}}}
