# What tests/core/test_python_api_tool_builder.py's tool builds: every builder method reaches the document, a `/` in
# the tool's name is its id, and `extra` adds keys the builder does not model. Its default is the text `1e3`.
class: CommandLineTool
id: tools/aligner
label: Align reads
doc: [Toy CLT, for serialization coverage]
inputs:
- id: reads
  secondaryFiles:
  - {pattern: .bai, required: false}
  format: edam:format_2572
  type: {type: array, items: File}
  inputBinding: {prefix: --reads}
- id: mode
  type:
    type: enum
    symbols: [fast, accurate]
    name: Mode
  inputBinding: {prefix: --mode}
- id: settings
  loadListing: shallow_listing
  type:
    type: record
    fields:
    - {name: threads, type: int}
    - name: preset
      type:
        type: enum
        symbols: [fast, accurate]
        name: Mode
    - name: tags
      type: {type: array, items: string}
    name: Settings
- {id: message, type: string, default: '1e3'}
- id: model
  type: ['null', File]
  inputBinding: {prefix: --model}
outputs:
- {id: sam, type: stdout}
requirements:
- {class: ShellCommandRequirement}
- class: InlineJavascriptRequirement
  expressionLib: ['function passthrough(x) { return x; }']
- class: SchemaDefRequirement
  types:
  - type: enum
    symbols: [fast, accurate]
    name: Mode
  - type: record
    fields:
    - {name: threads, type: int}
    - name: preset
      type:
        type: enum
        symbols: [fast, accurate]
        name: Mode
    - name: tags
      type: {type: array, items: string}
    name: Settings
- {class: DockerRequirement, dockerPull: 'alpine:3.20'}
- {class: ResourceRequirement, coresMin: 1.5, ramMin: 1024, outdirMin: 256}
- class: EnvVarRequirement
  envDef:
  - {envName: LC_ALL, envValue: C}
- class: InitialWorkDirRequirement
  listing:
  - {entryname: config.txt, entry: "threads=4\n"}
- {class: NetworkAccess, networkAccess: false}
- {class: ToolTimeLimit, timelimit: 60}
hints:
- {class: WorkReuse, enableReuse: false}
cwlVersion: v1.2
intent: ['edam:operation_3198']
baseCommand: [bash, -lc]
arguments:
- {position: 0, valueFrom: run-aligner}
stdout: aligned.sam
successCodes: [0, 2]
$namespaces: {edam: 'https://edamontology.org/'}
$schemas: ['https://example.org/formats.rdf']
sbol_intent: example:custom
customExtension: {enabled: true}
