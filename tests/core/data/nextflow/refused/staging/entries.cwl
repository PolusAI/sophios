cwlVersion: v1.2
class: CommandLineTool
doc: Entries the target refuses; an entry is one input, staged under its own basename or one literal name.
requirements:
  InitialWorkDirRequirement:
    listing:
    - {entry: $(inputs.a), writable: 'true'}             # writable is a literal boolean
    - {entry: $(inputs.b), writable: 1}
    - {entry: $(inputs.c), writable: $(inputs.flag)}
    - {entry: $(inputs.d), entryname: .sophios_entry_d}   # the prefix the target stages under
    - {entry: literal file content, entryname: x.txt}     # a file the task would write
    - {entry: $(inputs.e), entryname: $(inputs.e.path)}   # a computed name
    - {entry: $(inputs.f), entryname: sub/dir.txt}        # a name in a directory
    - $(inputs.nope)                                      # an input the tool does not declare
    - $(inputs.name)                                      # a value, not a path
    - $(inputs.files)                                     # an array
    - {entry: $(inputs.g), entryname: g.txt, foo: 1}      # a field nothing lowers
    - {entry: $(inputs.h), entryname: "a\0b"}             # a NUL byte no file name holds
baseCommand: 'true'
inputs:
  a: File
  b: File
  c: File
  d: File
  e: File
  f: File
  g: File
  h: File
  flag: boolean
  name: string
  files: 'File[]'
outputs: {}
