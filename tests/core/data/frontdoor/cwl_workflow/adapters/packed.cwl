cwlVersion: v1.2
$graph:
  - {id: main, class: Workflow, inputs: {text: string},
     outputs: {said: {type: File, outputSource: echo/out}},
     steps: {echo: {run: "#echo", in: {text: text}, out: [out]}}}
  - {id: echo, class: CommandLineTool, baseCommand: echo, inputs: {text: string}, outputs: {out: stdout}}
