{cwlVersion: v1.2, class: CommandLineTool, baseCommand: cat, inputs: {f: {type: File, inputBinding: {position: 1}}, r:
 {type: File, inputBinding: {position: 2}}}, stdout: b.txt, outputs: {out: {type: File, outputBinding: {glob: b.txt}}}}
