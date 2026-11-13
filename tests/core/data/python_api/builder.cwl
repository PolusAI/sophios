# What builder.py's workflow() compiles to: see the comments there.
steps:
- id: emit_text
  in: {message: message}
  run:
    class: CommandLineTool
    id: emit_text
    inputs:
    - id: message
      type: string
      inputBinding: {position: 1}
    outputs:
    - id: file
      type: File
      outputBinding: {glob: stdout.txt}
    baseCommand: echo
    stdout: stdout.txt
  out: [file]
- id: child
  in:
    file: {source: emit_text/file}
    count~scale: child~count~scale
  run:
    steps:
    - id: count
      in:
        file: file
        scale: {source: count~scale}
      run:
        class: CommandLineTool
        id: count
        inputs:
        - id: file
          type: File
          inputBinding: {position: 1}
        - id: scale
          type: float
          inputBinding: {prefix: -s}
        outputs:
        - {id: counts, type: stdout}
        baseCommand: [wc, -c]
        stdout: counts.txt
      out: [counts]
    class: Workflow
    inputs:
      file: {type: File}
      count~scale: {type: float}
    outputs:
      counts: {type: File, outputSource: count/counts}
      count~counts: {type: File, outputSource: count/counts}
  out: [counts, count~counts]
- id: write_dir
  in:
    outDir: {source: write_dir~outDir}
  run:
    class: CommandLineTool
    requirements:
      InitialWorkDirRequirement:
        listing:
        - {entry: $(inputs.outDir), writable: true}
      InlineJavascriptRequirement: {}
    baseCommand: [bash, -lc]
    arguments:
    - {valueFrom: touch $(inputs.outDir.basename)/done.txt}
    inputs:
      outDir:
        type: Directory
        inputBinding: {prefix: --outDir}
    outputs:
      outDir:
        type: Directory
        outputBinding: {glob: $(inputs.outDir.basename)}
  out: [outDir]
- id: cat
  in:
    f: {source: cat~f}
    go: {source: message}
  run:
    class: CommandLineTool
    baseCommand: cat
    inputs:
      f:
        type: File
        inputBinding: {position: 1}
    outputs:
      out:
        type: File
        outputBinding: {glob: out.txt}
    stdout: out.txt
  out: [out]
  when: $(inputs.go == "hello")
cwlVersion: v1.2
class: Workflow
inputs:
  message: {type: string}
  child~count~scale: {type: float}
  write_dir~outDir: {type: Directory}
  cat~f: {type: File}
outputs:
  said: {type: string, outputSource: message}
  _Counted: {type: File, outputSource: child/counts}
requirements:
  InlineJavascriptRequirement: {}
  SubworkflowFeatureRequirement: {}
