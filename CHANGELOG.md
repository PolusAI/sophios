# Changelog

All notable changes to Sophios will be documented in this file.

Each release's generated notes list what changed. Where a release breaks
something that worked in the one before it, an upgrade section beneath its
notes says what to change in your files and code.

## 0.8.0: upgrading from 0.7.0

Codes in parentheses are the codes Sophios prints with each diagnostic.

### Edge inference

- Naming conventions are on by default: when the nearest step has several
  outputs that fit an input, the one whose name pairs with the input's wins.
  `--inference_use_naming_conventions` is gone; drop it from your scripts.
  `--no_inference_naming_conventions`, or `Inference(naming_conventions=False)`
  in Python, turns them off. Write the edge where you relied on the other output.
- Python's `Inference()` applies the shipped renamings (`energy_` to `edr_`,
  `structure_` to `tpr_`, `traj_` to `trr_`), as the command line does, so a
  Python workflow infers the edges its `.wic` twin does; `renamings=` replaces
  them. A renaming applies only when an input's own name pairs with no output.
- An output with `_log_` in its name is no longer skipped: a log is a `File`
  like any other. Give the input a `format:` to keep logs out of its edge.
- A tie that no name settles goes to the output declared first, where it went
  to the last. Write the edge where you relied on the last.
- Two inputs of one tool step that read one output, one of them inferred, are
  refused (wic055) unless each declares a format the output carries. Write the
  edge each input should read.
- Every guessed edge is one row of one note (wic043), and wic042 is retired:
  under `--inference_strict` a guess fails as wic043.

### Packaging

- The `cyto` extra is gone, with `ipycytoscape.ipynb`, the notebook it served,
  which no longer ran. Drop `[cyto]` from your install lines; `Workflow.draw()`
  and `--graphviz` draw a workflow.

## [0.7.0](https://github.com/PolusAI/sophios/compare/v0.6.0...v0.7.0) (2026-11-04)


### ⚠ BREAKING CHANGES

* **compute:** every output lands in one folder, no outputMode
* **compute:** the compute client runs on macOS, Linux or WSL, no longer on Windows itself, where Service(url) raises cmp002 for every URL: run Sophios inside WSL.
* **compute:** http:// and https:// URLs are refused with ServiceUnavailable: they need an HTTPS gateway in front of the service, which there is not yet. requests 2.32.2 or later is required.
* **compute:** Service.run and its timeout= are removed: submit, then wait. So is RunFailed with its code cmp003, which only Service.run raised; cmp001 and cmp002 keep their numbers. Run.failure(), the step argument of Run.logs(), Run.targets and Run.show_warnings go too, and Run.cancel() returns a bool, not a RunStatus.
* **compute:** Service.over_ssh is gone. Service(url) is the one way to reach the compute service: the URL is the local end of a tunnel the user opens to the socket the service listens on,
* **compute:** RunConfig is no longer a frozen dataclass of the request's fields, and leader= and steps= no longer take dicts of request fields. It is built as tool_builder builds a tool, by methods that set and return it:
* **compute:** ComputeError is no longer a SophiosError, and SophiosErrorCode no longer has COMPUTE_REQUEST_REFUSED, COMPUTE_UNAVAILABLE or COMPUTE_RUN_FAILED. ComputeError is a plain Exception of sophios.compute's own: `code` is its own string, cmp001 (RequestRefused), cmp002 (ServiceUnavailable) or cmp003 (RunFailed), defined on each class, and `messages` holds every problem, a line each, where `diagnostics` held them; str() is each line under its code.
* **compute:** build_request, request_for, Service.submit and Service.run no longer take basepath=, workflow_out_dir=, workflow_in_place=, leader=, steps= or log_level=. They take a RunConfig, a frozen dataclass whose fields are exactly the request's fields besides id, workflow and inputs, named as run-request.schema.json names them, in snake_case, and sent as given: basepath, output_mode, workflow_out_dir, leader, steps, log_level; None leaves a field out. workflow_in_place=True is now output_mode='workflowInPlace', and workflow_out_dir=<folder> is output_mode='workflowOutDir' with workflow_out_dir=<folder>.
* **compute:** sophios.compute_request, its request schema version 1 and its config classes (ComputeRequest, ComputeExecutionConfig, ComputeOutputConfig, SlurmJobConfig, ToilRuntimeConfig), and run_local.run_compute are removed, with nothing in their place: a workflow runs on the compute service through sophios.compute's Service, and build_request returns the request it sends. utils.is_valid_url, which only run_compute used, goes with them.
* **api:** h.wait() returned a RunResult; it is now h.collect(), and h.wait() returns a RunStatus. h.cancel() returns a bool. WorkflowRunError is a SophiosError and no longer an ApiError, so `except ApiError` no longer catches a failed run.
* **api:** replace run_local_async with run_async
* compiled CWL names steps as written
* **api:** never load $schemas when validating a tool
* **lang:** read a quoted !ii scalar as its text
* delete wic_loader
* **api:** Step.from_cwl_document and to_step take step_inputs=, not config=
* **api:** Step(config_path=) is Step(step_inputs_file=)
* **api:** a step's inputs file binds its values as written
* **compiler:** a cwl_subinterpreter step is a real-time analysis declaration
* **api:** Workflow.to_wic_yaml() and the Workflow.yaml property are removed; call Workflow.write_wic() and read the file it writes.
* **api:** write_wic()/to_wic_yaml() take no inline_subworkflows; the subtree: form is gone; a renamed step carries run: <stem>.cwl.
* **tool_builder:** write the glob as a CWL expression, e.g. glob="$(inputs.output.basename)" for a File or Directory input, glob="$(inputs.output)" for a string one.
* **api:** add_input, bind_input, get_output, bind_output, add_output are gone from the public API; use the port objects.
* **ir:** scatter: [x] with x: !ii scalar is rejected; write a list.
* **lang:** in: {x: {source: ..., default: ...}} is reported; write !ii for a literal.
* **ir:** `!ii true`, or a Python-API True, on a string port now reaches the tool as "true" where it was "True". A tool comparing against "True" must compare against "true".
* **ir:** a literal that is not already of its port's type is wic020: `!ii 2.9` on an int input, `!ii yes` on a boolean input, `!ii '007'` on an int input, and a float written as YAML 1.1 text such as `!ii 1e-5` (write `1.0e-5`). A numpy int, float32 or bool from the Python API is no longer converted; pass int(), float() or bool(). string still takes any scalar and an int still feeds a float.
* **api:** Workflow.run() raises WorkflowRunError (api005) instead of returning after a failed run.
* **cli:** the CLI no longer passes --quiet to cwltool unless --quiet is given.
* **cli:** --run_local exits with the runner's non-zero exit code when the run fails; it used to exit 0.
* **cli:** an unrecognised argument exits with code 2 unless --passthrough_flags yes is given, and --passthrough_flags accepts only yes or no.
* **cli:** the CLI prints diagnostics, with file:line:col and code, and its compile-failure banners to stderr instead of stdout.
* fold wic021 into wic006, one code for a step with no id:
* a sequence step carries its name in an id: key
* an edge is defined at its source, so !& belongs on outputs only
* library code raises SophiosError instead of calling sys.exit(1). CLI exit codes are unchanged.
* contrib Python import paths move.   sophios.api.utils.converter   -> sophios.contrib.converter   sophios.api.utils.wfb_util    -> sophios.contrib.wfb_util   sophios.api.utils.ict.*       -> sophios.contrib.ict.*   sophios.api.rest.api          -> sophios.contrib.rest.api REST HTTP behaviour is unchanged; HTTP clients are unaffected. The design document approves this exception explicitly (§3, exception 2).

### Features

* a boundary declaration is its own type ([fbe7a4e](https://github.com/PolusAI/sophios/commit/fbe7a4e3afbf4f76f7fa4d6550a14c560230a3d7))
* a caller scatters only over the inputs its subworkflow declares ([1006418](https://github.com/PolusAI/sophios/commit/1006418a94d6b8745d46080a16b7cdca20c875c2))
* a diagnostic can name the step and port it is about ([67215eb](https://github.com/PolusAI/sophios/commit/67215eb75a6eaa9291a74247935b55c120864707))
* a front door that reads the file the reader wrote ([0bc1009](https://github.com/PolusAI/sophios/commit/0bc1009f84ac53ef97aa17b1113a94c25213a42f))
* a graph that states a document is its own type ([d991691](https://github.com/PolusAI/sophios/commit/d991691359a93c837ce374e496aaab1134698cd0))
* a scatter entry naming no input of its step, or one nothing binds, is wic032 ([c82170d](https://github.com/PolusAI/sophios/commit/c82170d4af41841e6eeb8bab72f17949876f59df))
* a wic: key the language does not have is wic033 ([6846df9](https://github.com/PolusAI/sophios/commit/6846df9e83d6aa6a6c757a0661ec5a7be008cd51))
* a wic: value of the wrong shape is wic034, and the schema says so ([8f87f78](https://github.com/PolusAI/sophios/commit/8f87f784bee0ca192ed77c69f01432dc2d5fa771))
* add a typed AST and total parser for the .wic syntax layer ([e5cd173](https://github.com/PolusAI/sophios/commit/e5cd173b4c32cba5c34f7d7619f1b18e3e0790be))
* an authored cwlVersion the toolchain does not run is wic035 ([cbb268e](https://github.com/PolusAI/sophios/commit/cbb268e5a8c4209115512047dcc12582f5946f6d))
* **api:** a step's inputs file binds its values as written ([3dea455](https://github.com/PolusAI/sophios/commit/3dea455ef972114b957d8d21ac415c40d566eba1))
* **api:** a value bound on a workflow input is its job value, and port names may start with _ ([05c6cac](https://github.com/PolusAI/sophios/commit/05c6cac4079ce6cbcd6132003588779b6a5c5a7b))
* **api:** add Workflow.start() and its run handle ([d623457](https://github.com/PolusAI/sophios/commit/d6234571fb4905052a907d70195e7908fdd823c7))
* **api:** API errors carry the span of the bind that raised them ([8ddf22b](https://github.com/PolusAI/sophios/commit/8ddf22bee6f8e1f15659d3a7b5ea82f2fe630209))
* **api:** compile(target="nextflow") returns CompiledNextflowWorkflow, which writes itself ([9a9dd96](https://github.com/PolusAI/sophios/commit/9a9dd96ff0f91acd53bdaada544df9e4a0709f37))
* **api:** CompiledWorkflow.write() writes the workflow and its job inputs in one call ([15881ef](https://github.com/PolusAI/sophios/commit/15881ef20117898efa21f5cf31c0c8d6a2852770))
* **api:** nodes built from Python carry the span of the line that built them ([eb423a4](https://github.com/PolusAI/sophios/commit/eb423a42a22cfc6bfd258fc82fd9954daf943af7))
* **api:** replace run_local_async with run_async ([a6e7518](https://github.com/PolusAI/sophios/commit/a6e75186a256e9c56cc1d6e01edf1bd01bd59cc6))
* **api:** Step.from_cwl_document and to_step take step_inputs=, not config= ([b2d3857](https://github.com/PolusAI/sophios/commit/b2d38573cc9de12d5e5bb70568cda3bd1e81e311))
* **api:** Step(config_path=) is Step(step_inputs_file=) ([ef81126](https://github.com/PolusAI/sophios/commit/ef81126cf6e7d6cbff2fe2b388e4a580ec97be2f))
* **api:** StepInput binds a CWL step input with Sophios-resolved sources ([44e8037](https://github.com/PolusAI/sophios/commit/44e80373ffa99454b176cdc2f9574c6a0d5750ed))
* **api:** Workflow.draw() for notebooks ([2e410d7](https://github.com/PolusAI/sophios/commit/2e410d7a2e1baa553e40f8d865982ecef472f48d))
* **api:** Workflow.from_wic builds Python objects from a .wic file ([f0415ce](https://github.com/PolusAI/sophios/commit/f0415ce5b9477e9ec6c7fbc36abc87cff2dd1ceb))
* **api:** Workflow.run() finds a .wic analysis through workflow_paths ([8cee7ef](https://github.com/PolusAI/sophios/commit/8cee7efc7e32e1468aee61ac3e12cef58e5ddea7))
* **api:** Workflow.run() raises WorkflowRunError on a non-zero exit ([988b5e1](https://github.com/PolusAI/sophios/commit/988b5e197d0ebb862148b4714d4ff791d4675b5d))
* apply wic: parameter passing in Resolve ([38ad370](https://github.com/PolusAI/sophios/commit/38ad370b7048fe4dc42c5b8981890545b81abc28))
* **cli:** --allow_raw_cwl runs a plain CWL workflow as it is ([f295e00](https://github.com/PolusAI/sophios/commit/f295e006721ad6654bda768a2313bfa066ee997a))
* **cli:** --check compiles and runs the pre-flight, then stops ([0855559](https://github.com/PolusAI/sophios/commit/08555598c2ffaeffc2c5d0ee3d8cc0a1519e2eb2))
* **cli:** --diagnostics json prints one JSON object per diagnostic ([42e77fb](https://github.com/PolusAI/sophios/commit/42e77fb00d7bc7c8441f97b2e4324b05e75bdd8d))
* **cli:** --explain prints what a code means and how to fix it ([2801977](https://github.com/PolusAI/sophios/commit/28019778b6d0f8df198b757eddfc54b9ed5e37f8))
* **cli:** a directory Sophios cannot write is wic021 ([2a4e9ce](https://github.com/PolusAI/sophios/commit/2a4e9ce9eefb78800731cf0fb31879c6a31251a1))
* **cli:** declare each option under the target it belongs to ([8e06a6f](https://github.com/PolusAI/sophios/commit/8e06a6fe51556f05291cfa8794c5ef7705faf4d2))
* close the opaque type and preserve literal spellings ([7b3eb06](https://github.com/PolusAI/sophios/commit/7b3eb06d510b5a4d0fbafe64e5d77722bc530ff4))
* compile from source text, and generate schemas that way ([b9fea97](https://github.com/PolusAI/sophios/commit/b9fea977b2dee22e121555e10907494e90654785))
* compiled CWL names steps as written ([495017c](https://github.com/PolusAI/sophios/commit/495017c11392914d761daf48ed2e38d13cd28225))
* **compiler:** a cwl_subinterpreter step is a real-time analysis declaration ([3d3a8b9](https://github.com/PolusAI/sophios/commit/3d3a8b944fc357762dee09e77692344aafeb4b1a))
* **compiler:** a positional outputSource is refused beside an inferred edge ([753c6f1](https://github.com/PolusAI/sophios/commit/753c6f14c5dfe54fa7fe81bd38072a9f6ba9a71e))
* **compiler:** name a generated step name in outputSource on stderr ([ee3a5b0](https://github.com/PolusAI/sophios/commit/ee3a5b0e9233e2c0ec4c9affb9e7ba7f0551f5fc))
* **compiler:** name a when: that reads a generated or undeclared input ([033fa54](https://github.com/PolusAI/sophios/commit/033fa544d987e9940dc094c7791e688c6dfc5e60))
* **compute:** build a version 2 request from a Workflow ([c1c19df](https://github.com/PolusAI/sophios/commit/c1c19df8529cbb2c276bd3d091f031d21481e5f1))
* **compute:** every output lands in one folder, no outputMode ([60be88d](https://github.com/PolusAI/sophios/commit/60be88d3b7bf1592a44c1ba95c851c19df4dac65))
* **compute:** Service reaches the service over SSH to its Unix socket ([cd2bcb3](https://github.com/PolusAI/sophios/commit/cd2bcb3702306bf465e33ce8065ca247b60dd6e0))
* **compute:** Service submits, waits and reattaches, and opens its own tunnel ([23cbf44](https://github.com/PolusAI/sophios/commit/23cbf44d9e9c52eae6c7d7c38dc26bfc381e9ee4))
* **compute:** where the outputs go, against the driver's bec46f2 ([971208f](https://github.com/PolusAI/sophios/commit/971208f3217826aeaf5a8072b18f0e57f6668e26))
* **diagnostics:** a diagnostic converts to a JSON object with its kind and fix ([35b2677](https://github.com/PolusAI/sophios/commit/35b2677211ca1165eed3523d0dfd33d6e351a15a))
* **diagnostics:** a note severity that reports without failing ([376aa7a](https://github.com/PolusAI/sophios/commit/376aa7a5637a277cb3b7e1c256e1e1180bb107d4))
* **diagnostics:** SophiosError.error takes a span and a locator ([5d9236d](https://github.com/PolusAI/sophios/commit/5d9236dd47c3bbaa221488e414d304c378e90d35))
* **drawing:** unfilled boxes, black inferred edges ([376d60d](https://github.com/PolusAI/sophios/commit/376d60d8ea513f3dcc7d7d002d9b36c2af5e7499))
* **emit:** a names map beside the root CWL maps emitted ids to authored steps ([2e62ce2](https://github.com/PolusAI/sophios/commit/2e62ce2b3f0280d7bac184b84e4863ef97dcf1c4))
* **emit:** the requirements a record needs are added to the document ([26f308e](https://github.com/PolusAI/sophios/commit/26f308ef54eec45781fcdab72651bf434800d2ca))
* export a JSON Schema derived from the language definition ([6d07b99](https://github.com/PolusAI/sophios/commit/6d07b995c6ad582d56e33058f7c2baa8ba448ccd))
* **frontdoor:** a run: path resolves against the referencing document first ([ddb0c56](https://github.com/PolusAI/sophios/commit/ddb0c5697877ad4c9f84b403c20d391ac1f22aa3))
* **frontdoor:** an inline run: body is registered as the step's tool ([ec01442](https://github.com/PolusAI/sophios/commit/ec01442ba66546d2b839da112a48dd4feb359d7d))
* give the library a failure type an embedder can catch ([38840d6](https://github.com/PolusAI/sophios/commit/38840d6819197e94f2d391a6b7f01b1d1e8aebfb))
* **infer:** a choice between equal candidates is noted; --inference_strict makes it an error ([f31431f](https://github.com/PolusAI/sophios/commit/f31431fbe4c363bd5bca3057f8c8a2a0ea41bd2a))
* **ir:** a record binding resolves each source and emits the fields it carries ([930370b](https://github.com/PolusAI/sophios/commit/930370b4a0522a75c96fc13ab23c8c960cf1e39d))
* **lang:** a support matrix over the CWL v1.2 workflow classes ([906358e](https://github.com/PolusAI/sophios/commit/906358e360f0b1b83d22bf07a2ee7514135f046d))
* **lang:** a wic: steps: entry may be keyed by a unique step id ([2b5cc6d](https://github.com/PolusAI/sophios/commit/2b5cc6d2065365f1df2845a9734d3a8d44d1ad7b))
* **lang:** every error code has a meaning, a kind and a fix ([35518a7](https://github.com/PolusAI/sophios/commit/35518a756adb487886e794773a211aa86e44f1a5))
* **lang:** outputSource may address a repeated step as (index, name)/port ([6f98852](https://github.com/PolusAI/sophios/commit/6f98852c0a969f913ce03b614fc1f66c90514520))
* **lang:** refuse names outside [A-Za-z0-9_.-] ([96e535b](https://github.com/PolusAI/sophios/commit/96e535b83727eebdce8c22dc6c2115110b6bab94))
* **lang:** report a positional outputSource port the step does not have ([2d645d4](https://github.com/PolusAI/sophios/commit/2d645d4744ac95c1e855bd6c3e46482ea6869138))
* **lang:** the !cwl record, CWL's WorkflowStepInput at an in: position ([0c5021e](https://github.com/PolusAI/sophios/commit/0c5021e21568c6872642d3c938e0b97b0125946d))
* name the three shapes a step input can be ([7717de5](https://github.com/PolusAI/sophios/commit/7717de5e6090e9a40514bb90841709880b0a9d40))
* **nextflow:** accept absent-optional val inputs ([cd130e7](https://github.com/PolusAI/sophios/commit/cd130e78322ad878c63a0ceb0bdf166f77fa62ad))
* **nextflow:** accept boolean-binding valueFrom self-reference ([c996729](https://github.com/PolusAI/sophios/commit/c996729499f84cb742a23a63cd4dbc6215223bbf))
* **nextflow:** accept shell-mode raw literal command tokens ([931d9c9](https://github.com/PolusAI/sophios/commit/931d9c9fa5de1bbdb9c5d68c27d9b625e6ffb8b6))
* **nextflow:** an optional output may need at most one file ([2f24397](https://github.com/PolusAI/sophios/commit/2f2439771229f445d15a6d4710cb28fe72cd4a66))
* **nextflow:** an optional scalar's own binding adds nothing when absent ([42c6c6e](https://github.com/PolusAI/sophios/commit/42c6c6ee48f3960f5a07a6e6d49387580f5e60e7))
* **nextflow:** an unscattered step's output reaches a scattered step as one value ([704805e](https://github.com/PolusAI/sophios/commit/704805e7db449bec652c174870ff34deb6676f17))
* **nextflow:** carry CUDA fields and Directory outputs in ext; make the model canonical ([927d49d](https://github.com/PolusAI/sophios/commit/927d49d458b2e7f5220ce9605209e6accac004bd))
* **nextflow:** check workflow input values at entry ([ba7d70e](https://github.com/PolusAI/sophios/commit/ba7d70e6c4afca6ea703ac29341c4861c8ce848b))
* **nextflow:** compile Sophios workflows to Nextflow IR ([67c6051](https://github.com/PolusAI/sophios/commit/67c605153b3deb0c50859cc97ec7a5dd2005ed9f))
* **nextflow:** compile, run and import workflows on a Nextflow DSL2 target ([aaca821](https://github.com/PolusAI/sophios/commit/aaca82158f7885158c4b49dbed6c16552db07e92))
* **nextflow:** every generated process refuses a dry run ([f678002](https://github.com/PolusAI/sophios/commit/f6780025e3c9c55cc1e70403e45e765d73d000ee))
* **nextflow:** every generated workflow reads back and promotes exactly ([91e8dc4](https://github.com/PolusAI/sophios/commit/91e8dc48fc79265927c2d2a004fe95f95ab3ef84))
* **nextflow:** expose target through Python and CLI ([c4e797f](https://github.com/PolusAI/sophios/commit/c4e797f29170ffc318092932a1ea0a1a5e8174b9))
* **nextflow:** gather indexed scatters, rescatter gathered arrays, per-invocation when ([4a5c4af](https://github.com/PolusAI/sophios/commit/4a5c4af28624384a5bcedd5dc1be849b73ec8e96))
* **nextflow:** generate and expose runnable workflows ([a07f773](https://github.com/PolusAI/sophios/commit/a07f7739695e5e8e3a23056c6858aa50ecfa1277))
* **nextflow:** import a pipeline Sophios wrote as one workflow built through the Python API ([e038131](https://github.com/PolusAI/sophios/commit/e038131c7d0c4b78a67a9220c250e4a753610248))
* **nextflow:** import and verify phase one workflows ([90a5093](https://github.com/PolusAI/sophios/commit/90a509319b454a13fd3ee049ab95ccbe331b0626))
* **nextflow:** inline one level of nested workflows ([994252b](https://github.com/PolusAI/sophios/commit/994252b857ab62804dd43ccdf17358534debebd0))
* **nextflow:** lower array-typed inputs to array-marked ports and bindings ([4f7cb96](https://github.com/PolusAI/sophios/commit/4f7cb96335e7537d2ef8ef1bccea52d596036295))
* **nextflow:** lower basename references to typed segments ([2007c1f](https://github.com/PolusAI/sophios/commit/2007c1f04360dd19408686bedf095aefe41b6c0e))
* **nextflow:** lower boolean inputBinding flags to conditional tokens ([b14c1ca](https://github.com/PolusAI/sophios/commit/b14c1ca153a20d715f0b291762bae94fa5d74fd0))
* **nextflow:** lower cwltool:CUDARequirement to the accelerator directive ([155b24a](https://github.com/PolusAI/sophios/commit/155b24a2d58d8252f86ba7eb7df8d0e6b96aa5ce))
* **nextflow:** lower EnvVarRequirement to exports before the command ([a18b8ad](https://github.com/PolusAI/sophios/commit/a18b8ad7b33b791087ab8cd138b3a8a633d70e6f))
* **nextflow:** lower loadContents with $(self[0].contents) to file-text capture ([bca1cb9](https://github.com/PolusAI/sophios/commit/bca1cb96f29433ef1fe6e8b6f1bc235a0bc7b06d))
* **nextflow:** lower outputEval $(self[0]) to an output cardinality marker ([c5cb587](https://github.com/PolusAI/sophios/commit/c5cb58798d7b90374c546d4f7e9aa556ad9794ef))
* **nextflow:** lower safe-subset valueFrom to computed argv words ([b8c25b7](https://github.com/PolusAI/sophios/commit/b8c25b75983774ebad5722fe3977406cb00f3552))
* **nextflow:** lower single-input scatter over one channel adapter ([d710e7c](https://github.com/PolusAI/sophios/commit/d710e7c4be9e549c08e698fc5dff5b7618b35c4e))
* **nextflow:** lower step-input default, linkMerge and pickValue ([47af5f1](https://github.com/PolusAI/sophios/commit/47af5f1f94fe3372a24ba5399095132edaf40893))
* **nextflow:** lower when to conditional steps with CWL null ([5a07804](https://github.com/PolusAI/sophios/commit/5a078040cfeee78f08974a03abaae0feb0fb31b9))
* **nextflow:** multi-input dotproduct scatter with index-ordered gather ([5d7712a](https://github.com/PolusAI/sophios/commit/5d7712a34892097e3ee465d8c0f58f0e5e562051))
* **nextflow:** multi-input flat_crossproduct scatter ([b601b55](https://github.com/PolusAI/sophios/commit/b601b5542d259c84aea73d71a88bd34b7672a68d))
* **nextflow:** multi-input nested_crossproduct scatter ([3047c65](https://github.com/PolusAI/sophios/commit/3047c6550ef1712783fed6195ce6c8a53a694ec2))
* **nextflow:** one channel construction for the validator and the renderer ([1cb336e](https://github.com/PolusAI/sophios/commit/1cb336e69cb6c280a54d98ef653779a45f8e8788))
* **nextflow:** one support table drives capability analysis ([8e6c64f](https://github.com/PolusAI/sophios/commit/8e6c64f2f095810397bf81b12d6ec6d0348c067d))
* **nextflow:** read the three files a pipeline is written to back into its model ([27811a1](https://github.com/PolusAI/sophios/commit/27811a1824cda5301c379d138dc764f0e4f9a8b4))
* **nextflow:** reject computed valueFrom on bindings and arguments ([54cb3e5](https://github.com/PolusAI/sophios/commit/54cb3e57545450f2cd8b739968a21af2fd620ed5))
* **nextflow:** run a containerized command after its image's entrypoint ([058b581](https://github.com/PolusAI/sophios/commit/058b581b2bbc15051a4fc55f0e39de8be998b0b6))
* **nextflow:** run host and container tasks in one workflow ([397da15](https://github.com/PolusAI/sophios/commit/397da15d3e2e9d4f4875a889bd7d37ebb69acae2))
* **nextflow:** stage InitialWorkDirRequirement listing entries ([18c88c3](https://github.com/PolusAI/sophios/commit/18c88c3a6022aad8fbd93aec7e8cf497d509aa92))
* **nextflow:** the entry workflow prints each workflow output once it completes ([467510e](https://github.com/PolusAI/sophios/commit/467510ee582e0fb237c5aef8d28be40fe18324c4))
* **nextflow:** the task makes every InitialWorkDirRequirement entry ([5e8e71c](https://github.com/PolusAI/sophios/commit/5e8e71cd05f41aba6792b978fe19ee37658e3be0))
* parse an implementations body as the document it is ([06a2cb4](https://github.com/PolusAI/sophios/commit/06a2cb4c83928b58ff8b9d1d63762a6c1759afa7))
* **post_compile:** flatten_subworkflows dissolves each call that changes nothing when dissolved ([8618f96](https://github.com/PolusAI/sophios/commit/8618f966b0e469a7aa9683155acc29dae36f1d34))
* **realtime:** compile each declared analysis once and write a manifest ([9a5bbd2](https://github.com/PolusAI/sophios/commit/9a5bbd2ac92949c61afa113333f0ccc9f1e83fdd))
* refuse a source spelling that reached emission ([84d405f](https://github.com/PolusAI/sophios/commit/84d405f4b52eca0089573e4077e5dbff45f990be))
* resolve the language version by rule, not by accident ([ee568c4](https://github.com/PolusAI/sophios/commit/ee568c46425729c25bbd1e9425598574f25c4db4))
* **resolve:** name a wic: steps: key that addresses no step on stderr ([d4d9571](https://github.com/PolusAI/sophios/commit/d4d957142f7cbf0bbbc51808d88a7f8c4775b480))
* run executes exactly what compile writes ([838b9d2](https://github.com/PolusAI/sophios/commit/838b9d2f1f7662312a04d7235aca21f252a4e416))
* **run_local:** runner messages are rewritten with authored names ([d25e044](https://github.com/PolusAI/sophios/commit/d25e0446f17250fe8829280262f5024d6ebbaf3e))
* **run:** a backend runs the Nextflow target with nextflow ([8ec8d4a](https://github.com/PolusAI/sophios/commit/8ec8d4a0630e69944d4c03c92f65775f183d1f75))
* **run:** a failed run names each failed step and how it ended ([ddeca89](https://github.com/PolusAI/sophios/commit/ddeca89cbd05d7f538e803b0260a8dad5466bfd5))
* **run:** name a program the run calls that is not installed (wic029) ([6daed5d](https://github.com/PolusAI/sophios/commit/6daed5d11f5a91de5946824a243c697488cd3885))
* **run:** run real-time analysis beside the workflow ([c4045aa](https://github.com/PolusAI/sophios/commit/c4045aa2ffaac35f65184c03b637639a6ca86eb3))
* **run:** Workflow.run and --run_local run the Nextflow target ([fdfc20b](https://github.com/PolusAI/sophios/commit/fdfc20b7a46d91a6dc3d07ce233cedfc2c7d8246))
* surface the resolved language version everywhere it matters ([71a6eea](https://github.com/PolusAI/sophios/commit/71a6eea081fb309a097d1de70e6b87034d8d54e3))
* validate against the schema as each document is read ([64b699b](https://github.com/PolusAI/sophios/commit/64b699bc929d868374d9db9b3ae7f8bda879ce23))
* write the .wic AST back out in both surface spellings ([7b3528e](https://github.com/PolusAI/sophios/commit/7b3528ed1875bdcd7544fd34af1717d04870e849))


### Bug Fixes

* __version__ is the version this copy of the code was built as ([3b800f8](https://github.com/PolusAI/sophios/commit/3b800f8d8fc5735fe81c66572bf5df509581a43c))
* --docker_remove_entrypoints runs the new tags ([cdb9837](https://github.com/PolusAI/sophios/commit/cdb9837c12f9d65af0768a60a8f61343870dd903))
* --ignore_docker_processes gates the docker process check ([aa87d83](https://github.com/PolusAI/sophios/commit/aa87d83af051e3be6e6857330ab304f5d9b8c8c1))
* --quiet leaves the compiled CWL alone ([efa8eb2](https://github.com/PolusAI/sophios/commit/efa8eb24dd8e6c5dabf6dadaa8879e4a4d732ec1))
* a call does not exempt an edge from document order ([1116726](https://github.com/PolusAI/sophios/commit/11167267d2f1ebbf86a6494272ea54fca69d89fd))
* a called workflow selects its implementation too ([426c2e4](https://github.com/PolusAI/sophios/commit/426c2e46f6f248742b7f704dbeb4c3e25f7b8ee4))
* a cwltool test declares its dependency, and nothing runs it off POSIX ([4b06d47](https://github.com/PolusAI/sophios/commit/4b06d4710d8d36eb94a0f7bab725351c5f3a07bf))
* a falsy default is not the absence of a default ([7c8648a](https://github.com/PolusAI/sophios/commit/7c8648a6c8e72c53e0c733901b2b80352702857f))
* a generated process consumes its generation parameters ([f2d319c](https://github.com/PolusAI/sophios/commit/f2d319c1f56985bff6a00c4b72f0436ccee8ba71))
* a generated process does not emit its generation parameters ([df656ef](https://github.com/PolusAI/sophios/commit/df656efe8d31e716a192ca493e3e649ca14fe42c))
* a job value has one normal form, and coercing it again changes nothing ([25971a9](https://github.com/PolusAI/sophios/commit/25971a9e66e584f09ab2c4d3ffebfe9fd79c92ea))
* a list-form outputSource without a type says it needs one ([1d59f24](https://github.com/PolusAI/sophios/commit/1d59f24a0468c8389019a01e45bb4d0caf9d1fcd))
* a mapping bound to a string port is serialized as JSON ([d8bb706](https://github.com/PolusAI/sophios/commit/d8bb706a6b59529b4030afd4622635fb16da07b7))
* a misspelled desugared construct is a diagnostic, not passthrough ([5b975c5](https://github.com/PolusAI/sophios/commit/5b975c59d908693c310ecd3dfd0c8517696fe142))
* a non-finite float bound to an int port is wic020 ([1d8775e](https://github.com/PolusAI/sophios/commit/1d8775e867ec3c7a1ed24d61833fe0fd87558b13))
* a null default satisfies nothing, and the predicate gates inference too ([847914a](https://github.com/PolusAI/sophios/commit/847914a1caf040581306af906b80d87269ea780f))
* a packed CWL tool is read through its main ([4d261f8](https://github.com/PolusAI/sophios/commit/4d261f82f978cd635c4ff7497190fe6de9db0b35))
* a packed tool's ports are bound by their names, not by #main/&lt;port&gt; ([2419b82](https://github.com/PolusAI/sophios/commit/2419b827c2bbc58a8f949baf3da785b9019c3c9f))
* a scatter entry that is not a name is wic032 ([baeeda1](https://github.com/PolusAI/sophios/commit/baeeda119a52d92a1363568a126c5eea06db99e1))
* a scattered lift keeps a list-valued child value whole ([2328a23](https://github.com/PolusAI/sophios/commit/2328a2303f23e4265f1277adf8f58d1b959793af))
* a sequence step carries its name in an id: key ([d35f998](https://github.com/PolusAI/sophios/commit/d35f998aa76a6886d0010f8201edab3c1968e6d1))
* a sequence step with no id: still reports the contents of its body ([ba69b2b](https://github.com/PolusAI/sophios/commit/ba69b2ba6aa58f3293d9189b59f398ca2e7fda98))
* a sequence step's identity and body are read the way the mapping form reads them ([40e95c7](https://github.com/PolusAI/sophios/commit/40e95c71e683b83a2c1f14119e84c6ac052ec669))
* a sink already relayed needs no second boundary name ([5f42c2c](https://github.com/PolusAI/sophios/commit/5f42c2cb32ce95daf32819af05880e3390a587e0))
* a spelling probe that cannot parse has its answer ([51112b4](https://github.com/PolusAI/sophios/commit/51112b46b415f0c956d75c6286e6617df5e77abf))
* a string job value stays a string to cwltool ([7c5d06f](https://github.com/PolusAI/sophios/commit/7c5d06f35be8b5f35e1471e8ce3618625ae87ad8))
* a subworkflow call scatters a name its callee exposes ([263db31](https://github.com/PolusAI/sophios/commit/263db31314d5d2545c16f198195f684b9da105d7))
* a wrong-shaped literal is a TypeError, and it was escaping undiagnosed ([0d99647](https://github.com/PolusAI/sophios/commit/0d99647206487d0f6da5522151b84126204eda6f))
* accept !& under a wic: sidecar step's out: ([0cc5512](https://github.com/PolusAI/sophios/commit/0cc55123c3c363d2a9922b07e704ecc54a1b6bb0))
* an edge is defined at its source, so !& belongs on outputs only ([79d5c69](https://github.com/PolusAI/sophios/commit/79d5c6905f8e606e596dd984ae05eb7e37539878))
* an embedded process is not a document ([49be25e](https://github.com/PolusAI/sophios/commit/49be25e791e9540b613702a6cae628769b3c0059))
* an embedded process keeps no location for its $include ([6135f5b](https://github.com/PolusAI/sophios/commit/6135f5ba6573bc79fd7bfca33885d366605008df))
* an empty wic: wrapper beside its siblings is an empty mapping ([c608ad4](https://github.com/PolusAI/sophios/commit/c608ad4e695d42ddd6c131a260aa360537f6a106))
* an input mapping may name a port below this graph ([828a195](https://github.com/PolusAI/sophios/commit/828a19502cb4edb734c3ce4c9bdc0dcf30ad5104))
* an output-target Directory in an inputs file is a name for the run to create ([152d63b](https://github.com/PolusAI/sophios/commit/152d63b028300079b900928a7b7b0a4187f89fc9))
* an undefined edge and a duplicate definition carry codes ([e2b7f38](https://github.com/PolusAI/sophios/commit/e2b7f386d22d147a25527cf2237f372c967e4f16))
* **api:** a bound source's effective type is computed on read from its owner's scatter ([73d37af](https://github.com/PolusAI/sophios/commit/73d37afb1f4c30710d507266cd0abd64e35d1b04))
* **api:** a list of port objects bound directly is InvalidInputValueError ([d08dba3](https://github.com/PolusAI/sophios/commit/d08dba332d4b6866ccf752f35bc18bbf7e0ec8bf))
* **api:** a multi-source StepInput is type-checked ([4ee1df9](https://github.com/PolusAI/sophios/commit/4ee1df9cccbfe453e250dbcba5a7bce522a9a674))
* **api:** a port name a step refuses is refused where it is written, on import as wic054 ([20fb379](https://github.com/PolusAI/sophios/commit/20fb3796117b5dec112ac29e9ed050ca301be0b6))
* **api:** a port nested anywhere in a bound literal is an error ([83b9a7b](https://github.com/PolusAI/sophios/commit/83b9a7bbb988f0c582d55df99c2622019309d648))
* **api:** a StepInput whose pickValue picks one value is not scatterable ([ba366d6](https://github.com/PolusAI/sophios/commit/ba366d669ff6e52ec05ac4dcb657eca9d156a839))
* **api:** a StepInput's edge names never match a plain link's ([6bcdba8](https://github.com/PolusAI/sophios/commit/6bcdba828310410eeff07e463a03183d01efc267))
* **api:** a subworkflow's output is a source for a sibling step and a parent output ([55949d8](https://github.com/PolusAI/sophios/commit/55949d89721ec24c35aad248c14f74f21f38dc07))
* **api:** a subworkflow's output type is read from its source when needed ([02f60bf](https://github.com/PolusAI/sophios/commit/02f60bfec280123907fb7433c184c44cb9db5aca))
* **api:** a workflow input binds to a port as the source it is ([2289256](https://github.com/PolusAI/sophios/commit/22892560ea2dd2b3f82207e4490e4f36ad6cb6d7))
* **api:** a workflow input's type does not depend on binding order ([1c691e4](https://github.com/PolusAI/sophios/commit/1c691e4550f058395d54f2bbf31d164c02678fb7))
* **api:** a workflow output bound to stdout or stderr declares File ([8c93afc](https://github.com/PolusAI/sophios/commit/8c93afc48c9ad708a43df75e69b4f4dda4418fb4))
* **api:** configure inference from Python ([5bd25b4](https://github.com/PolusAI/sophios/commit/5bd25b4ecb3306858dae9fc66adaa97879323570))
* **api:** from_wic reads an input declared as an empty mapping as untyped ([ee7fdd5](https://github.com/PolusAI/sophios/commit/ee7fdd51d3910dd47d10bf45453eab5b8850436e))
* **api:** keep pseudo-filenames and resolve each file once in caller_span ([5b88e70](https://github.com/PolusAI/sophios/commit/5b88e70e20a941bceac68d743e71ad3c71787b5e))
* **api:** point the implicit-input warning at the reference and the typed declaration ([8178919](https://github.com/PolusAI/sophios/commit/8178919737d76b9e6aff5ef0fc73e8fd1721257d))
* **api:** promote a bound stdin input as File ([7daf0be](https://github.com/PolusAI/sophios/commit/7daf0be1580a6fc8a51e052f050a69dc47b0246b))
* **api:** reach the YAML writer through the tool builder's support module ([8f471a2](https://github.com/PolusAI/sophios/commit/8f471a270dee13712949f513cba80594fcc7872c))
* **api:** removed Workflow.yaml and to_wic_yaml raise an error that names write_wic ([1f3f92a](https://github.com/PolusAI/sophios/commit/1f3f92a35174a8cbeeacb9030ff33be87822081c))
* **api:** Workflow.run() checks the paths bound in Python before the run ([e04bd9f](https://github.com/PolusAI/sophios/commit/e04bd9f4690b6865004a1a997adcd6512984f14b))
* **api:** Workflow.run() reads a relative path bound in Python from the working directory ([0f41209](https://github.com/PolusAI/sophios/commit/0f412093a689808f05e26341a2c580db1bf0832b))
* **api:** write a real-time declaration back as a declaration ([16ef65e](https://github.com/PolusAI/sophios/commit/16ef65eaccb3727fad631f66b131e7608fbc1bb0))
* **api:** write_wic names every step's tool and guards the tools it writes ([d6be51b](https://github.com/PolusAI/sophios/commit/d6be51b36af8db7c1f0a11cf1ab67469ce59cdc8))
* binding an input does not edit the documentation the user wrote ([0b2010d](https://github.com/PolusAI/sophios/commit/0b2010d41c30d5564b2b87a83326ead3833d443b))
* build the fuzz strategy from the store it needs ([10a930b](https://github.com/PolusAI/sophios/commit/10a930be71d3c934c27f105af09ef6831f53943c))
* carry a requirements: shape the compiler does not model ([b77a965](https://github.com/PolusAI/sophios/commit/b77a9659ec2be2bdcb1afbef9b824c303674e1eb))
* check the tag before the position, and keep the name either way ([cea6e31](https://github.com/PolusAI/sophios/commit/cea6e31848d61d257aa4d1194887b33ca3793d82))
* **ci:** isolate nextflow runtime validation ([147dd8b](https://github.com/PolusAI/sophios/commit/147dd8b0a7a8f12d90b28f5eecb67a99b55ac6bc))
* **ci:** Sophios CI never checks out the private compute repository ([3423112](https://github.com/PolusAI/sophios/commit/3423112a39d7b7f878f73e7736f3df51f21e2894))
* **ci:** the home-made actions need no npm packages ([04d0766](https://github.com/PolusAI/sophios/commit/04d0766337b58d15d59637def3fcd2e9a2ee54c5))
* **cli:** --basepath, as run(basepath=) has ([19989aa](https://github.com/PolusAI/sophios/commit/19989aacb8e4f6965f5f2304a04b86e4211a5a3e))
* **cli:** --check leaves the base path untouched ([9b4a778](https://github.com/PolusAI/sophios/commit/9b4a778a504b024b0fe94ce549dd524de88903f5))
* **cli:** --cwl_inline_subworkflows flattens and --cwl_inline_runtag embeds, independently ([f134c17](https://github.com/PolusAI/sophios/commit/f134c179666664e810aed688983f5ef5886c09bd))
* **cli:** --graphviz without dot is a note, not three lines on stdout ([92919d1](https://github.com/PolusAI/sophios/commit/92919d1af18af7efc8364ad6eb082b15c3b6ae87))
* **cli:** --run_local exits with the runner's exit code ([84fdf49](https://github.com/PolusAI/sophios/commit/84fdf49c43cff14da1eddea12ab0e000726ea10a))
* **cli:** a --yaml or --inputs_file that names no file is a usage error ([0e2a7f1](https://github.com/PolusAI/sophios/commit/0e2a7f152e13cc5fa7df44bf62bee3d8f75e0370))
* **cli:** a flag is never read as an abbreviation of another ([caaf576](https://github.com/PolusAI/sophios/commit/caaf5760aeab173702283e0cba2b8c5965152faf))
* **cli:** a plain CWL workflow under --allow_raw_cwl lowers under --target nextflow ([e7bdc64](https://github.com/PolusAI/sophios/commit/e7bdc646355a13eb175ee3c3e01908d43e286119))
* **cli:** a run reads a !ii path beside the workflow in place, without copying it ([1c5a087](https://github.com/PolusAI/sophios/commit/1c5a087da323b56b7718610e2b96c7b645e7d911))
* **cli:** an unexpected failure is one line, and its traceback file keeps the frames ([a346e40](https://github.com/PolusAI/sophios/commit/a346e406de6def566802a58264e37204d0c435db))
* **cli:** name --passthrough_flags for an unknown argument only under --target cwl ([f8eef2a](https://github.com/PolusAI/sophios/commit/f8eef2a210d8b234b091e6ae13465883c33bd179))
* **cli:** name a diagnostic's file as it is on disk ([29c50a2](https://github.com/PolusAI/sophios/commit/29c50a2c2e7a3d347d8c51eed80dafc619f60abd))
* **cli:** pass --quiet to the runner only when the user asked for it ([172ca0b](https://github.com/PolusAI/sophios/commit/172ca0b3c3ab9b476dc32b72764bdf4e34d975c5))
* **cli:** print diagnostics with their position and code, on stderr ([e4eb916](https://github.com/PolusAI/sophios/commit/e4eb91645d548b64655f82925c572b39bb2dde72))
* **cli:** reject unrecognised arguments unless --passthrough_flags yes ([b6bd53a](https://github.com/PolusAI/sophios/commit/b6bd53a9b2eac1d09575bdc4d83db3fe1b19c63d))
* close the review's open findings on the syntax layer ([4e4be8d](https://github.com/PolusAI/sophios/commit/4e4be8d6600cddda56341432bba23f6da8972dbd))
* collect a pin written on an inline implementation body ([d030143](https://github.com/PolusAI/sophios/commit/d0301433a308a8047c8e6e48f9a31e63576d3161))
* collecting tests provisions nothing ([4313214](https://github.com/PolusAI/sophios/commit/4313214a3d90ce26c887e0a9fe3836436655b7a5))
* **compiler:** a scalar literal on a union with an array takes the member of its own type ([221b4a5](https://github.com/PolusAI/sophios/commit/221b4a5ee0aec3f4734a677e270deed64ee1616c))
* **compiler:** keep the spelling notes within pylint's locals limit and state the when: caveat exactly ([aa7c2e4](https://github.com/PolusAI/sophios/commit/aa7c2e4c49ceb07f33a01d0b1de051dbdddebf93))
* **compiler:** know a positional outputSource by its spelling ([7b32021](https://github.com/PolusAI/sophios/commit/7b3202109534be4c453521943df694107e345d17))
* **compiler:** read a when: input name as a word, not across a hyphen ([5bd9068](https://github.com/PolusAI/sophios/commit/5bd90683668b9f791a338868241dae2cbaf7acf7))
* **compiler:** say when an outputSource step id names more than one step ([faf219e](https://github.com/PolusAI/sophios/commit/faf219e93298f5147dcf69fc388a7d6b379cef00))
* **compiler:** wic011 leaves the file name to the span ([e0aad45](https://github.com/PolusAI/sophios/commit/e0aad45fd2c11ffb131a825d71d940b6e03832bb))
* **compiler:** write a run: path's tool under the name its step runs ([42b45fe](https://github.com/PolusAI/sophios/commit/42b45fe7db4a80ff49e91caf4899cad78fb7bd67))
* **compute:** Service refuses Windows itself: the client needs macOS, Linux or WSL ([4472009](https://github.com/PolusAI/sophios/commit/4472009090ce4c3a930eafe73a323b438f029556))
* **compute:** the forward's directory goes whatever fails; an ssh that cannot run is cmp002 ([7e7b10e](https://github.com/PolusAI/sophios/commit/7e7b10ea332aad2bd165cb307fedac17d4e73cc2))
* **compute:** wait() prints the run's end after its steps' lines, so the end is its last line ([f1f5568](https://github.com/PolusAI/sophios/commit/f1f5568f9246481c675f2b2a07fc47bbd394de0b))
* **compute:** workflowOutDir is relative to basepath, and a run's output folders are all names or all paths ([19be187](https://github.com/PolusAI/sophios/commit/19be18767252f1fad0e1b2d42dbc07c768481425))
* **config:** a missing --config_file is an error, not a write to ~/wic ([369b07e](https://github.com/PolusAI/sophios/commit/369b07e0b752825efccc2a85be1026790a7adfca))
* **config:** a search path that is not a list is a TypeError ([ff366dd](https://github.com/PolusAI/sophios/commit/ff366dd452c5604505dec96096949346c4bf04c1))
* **config:** provision resources under the selected home ([3d58677](https://github.com/PolusAI/sophios/commit/3d586777099e4d00c64483b38d3f401e4e2d3c90))
* configuration is a value, parsed once at the boundary ([aa8344e](https://github.com/PolusAI/sophios/commit/aa8344eabae91cdbb94da2bfb54729890b141b9b))
* copied tools keep their relative files ([cb5d117](https://github.com/PolusAI/sophios/commit/cb5d117530695f69f3caa7aabdebf3bf35f2bf7d))
* create generated CWL output directory ([92eb2ed](https://github.com/PolusAI/sophios/commit/92eb2edecf5f1586f5557637be159b4554f22e03))
* CWL depends only on its authors' files ([684e5fe](https://github.com/PolusAI/sophios/commit/684e5fec1dddb6084a7090c737db336aecc9f5e4))
* deduplicate unlabeled Graphviz edges ([9984fb4](https://github.com/PolusAI/sophios/commit/9984fb4c02f47ecf6b92cf1b1c157a2704e486a4))
* detach inline implementation bodies into the registry ([52801ba](https://github.com/PolusAI/sophios/commit/52801bacc7cd44a0f8278be9ebd1a237f63be066))
* diagnose only provably disjoint references ([4ee992d](https://github.com/PolusAI/sophios/commit/4ee992d88fbd1f28053a25fb83cf0c1f86e110d6))
* **docker:** repair the REST image so it can build and run ([2cac4db](https://github.com/PolusAI/sophios/commit/2cac4db37635eda9ee9ac2f64f5daaf73fe250c7))
* **docs:** build on ubuntu-24.04 with a Sphinx that knows :no-index: ([ee40cbb](https://github.com/PolusAI/sophios/commit/ee40cbb97df20a617eb2f4ed6372e332e5b63e5c))
* **docs:** keep ruamel's ScalarNode out of the input_output API page ([e88974e](https://github.com/PolusAI/sophios/commit/e88974ec9e24590be504ec1dbf2f7ae24f1b3096))
* **docs:** keep ruamel's YAML class out of the input_output API page ([616f506](https://github.com/PolusAI/sophios/commit/616f5069f0618b6e4d731d9e46b4724d279976ae))
* docstrings that describe code that is not there ([300d158](https://github.com/PolusAI/sophios/commit/300d15844af8b7881e43894b12c8d546552dd0ab))
* **drawing:** name a subworkflow's cluster after the step that calls it ([5bfca45](https://github.com/PolusAI/sophios/commit/5bfca45b5f54e084131f37f6a69d4b289de22c34))
* edges belongs to the graph that owns them ([e3fe8cf](https://github.com/PolusAI/sophios/commit/e3fe8cfc2801fc2a7d9c70421fa2927b3c42b872))
* emission no longer depends on the hash seed ([0e588fc](https://github.com/PolusAI/sophios/commit/0e588fc23c7214c41a38eebc6e3b318f1805db64))
* Emit spells scatter from its resolved ports ([be00af2](https://github.com/PolusAI/sophios/commit/be00af24f7760948d4d44dcaf3e38118e1287305))
* **emit:** keep an authored requirement body when the compiler needs the same class ([a947ddb](https://github.com/PolusAI/sophios/commit/a947ddb12b83484a477eadd8cb03591ec7374748))
* **emit:** write a step's scatter as a list ([84ecfc2](https://github.com/PolusAI/sophios/commit/84ecfc2d81b15f957da2a900adbeb506805d0cee))
* enforce Python API diagnostic contracts ([cb3a04d](https://github.com/PolusAI/sophios/commit/cb3a04dee13b8eb89293f7b0c48c05a5fd5a2cdf))
* enforce the language's invariants at the parse boundary ([750fd86](https://github.com/PolusAI/sophios/commit/750fd86b643dbd5c48bb65ef9264f7dd5b66c1e4))
* **examples:** the ichnaea examples run vjaganat90/ichnaea-segmentation-plugin:devn ([3f14788](https://github.com/PolusAI/sophios/commit/3f14788b2cec4eb0531fb813002a682a2526b3bd))
* **examples:** the integrated ichnaea example writes in place with absolute paths and no outputs folder ([693779d](https://github.com/PolusAI/sophios/commit/693779db3d19b8f6ee9897950e705b58148e5b6d))
* file a workflow under every namespace that calls it ([f404a6a](https://github.com/PolusAI/sophios/commit/f404a6ad5f79b03cec04273ee04993cfcf475b85))
* find the scattered boundary in the mapping, not by its name ([c3d3a54](https://github.com/PolusAI/sophios/commit/c3d3a54024732df0a52081e6e1d226700560e602))
* **frontdoor:** an inline run: body is written as the one CWL version Sophios emits ([3d04b89](https://github.com/PolusAI/sophios/commit/3d04b8920bc694ccad07efaf6d8f3f2120aa52fa))
* **frontdoor:** each run: body or path is its own registered process ([fc2e7a0](https://github.com/PolusAI/sophios/commit/fc2e7a03cff0c7dc5ff98a3192a20f0e4985c349))
* **frontdoor:** key a run: path by the file it resolves to ([4267365](https://github.com/PolusAI/sophios/commit/4267365fdac2bdcf5d99a7de17a3ae5f37ca8c82))
* **frontdoor:** name a run: path by its absolute path when it shares no drive with the root ([20a8a89](https://github.com/PolusAI/sophios/commit/20a8a8999a8565de0321f62b093f37b0dcb84a61))
* give the CWL substrate one declared version ([1463e85](https://github.com/PolusAI/sophios/commit/1463e851720e4b44178bd0898ce370d6dadef2a9))
* **graph:** draw inferred edges in the theme colour and explicit ones in blue ([00c008a](https://github.com/PolusAI/sophios/commit/00c008a375c70083db60916797baeddd927c1650))
* **graphviz:** draw the wic: graphviz: label, style and ranksame keys again ([1553433](https://github.com/PolusAI/sophios/commit/1553433ebdf6f2dc42b8afb548aa4ce98bf8aecf))
* handle empty requirements and loop index shadowing ([e1f9beb](https://github.com/PolusAI/sophios/commit/e1f9bebf616228e21884a116fd95906bb3f8aa82))
* Infer's wic032 spells a scattered name as scatter: does ([1cd435d](https://github.com/PolusAI/sophios/commit/1cd435d9c2b18688019de7e0721f5f83e1f3d95b))
* **infer:** an inference note names each step by position ([8de5b5a](https://github.com/PolusAI/sophios/commit/8de5b5af5c516750ef38abe3b7674bb2ebe0510b))
* **infer:** an inference note names the inner step a pin goes on ([f1da215](https://github.com/PolusAI/sophios/commit/f1da215245408c0999d4614cb3626b88cc4c6c56))
* insert steps in the id: form get_steps_keys can read ([11efdd2](https://github.com/PolusAI/sophios/commit/11efdd28c296787205c97826ed6e740a5d1071bc))
* **install:** Node on the 26.x line, newest patch ([3911ada](https://github.com/PolusAI/sophios/commit/3911ada80d5b4551f44df322bdec4212eb1330af))
* **ir:** a boolean on a string port is true or false ([3e83480](https://github.com/PolusAI/sophios/commit/3e8348057f1bff589d0a880f561b15549f9603a8))
* **ir:** a literal must already have its port's type; nothing is converted ([f0836c3](https://github.com/PolusAI/sophios/commit/f0836c37beed45c54e00510002ce2a3ef9cbdd38))
* **ir:** a promoted null secondaryFiles states no entries ([34e4716](https://github.com/PolusAI/sophios/commit/34e47165d5c98c619c3e06143c47c2e8381ff73a))
* **ir:** a promoted port keeps the boundary fields the CWL schema allows ([03d316a](https://github.com/PolusAI/sophios/commit/03d316a3fc20462a02ee7bb7ddfa9e461356e51f))
* **ir:** a promoted port writes a bare secondaryFiles pattern as a list ([f277dc5](https://github.com/PolusAI/sophios/commit/f277dc537576541bff90ea773e8d0cad6c5fc86e))
* **ir:** a scalar literal on a scattered input is wic020 ([b2edbf9](https://github.com/PolusAI/sophios/commit/b2edbf9e60799bcbfa21c85afd97710f7b971382))
* **ir:** a stdout or stderr output is promoted to the boundary as a File ([7db1837](https://github.com/PolusAI/sophios/commit/7db18375315d5d8170426a4b047d62ada3ac99d1))
* **ir:** an untyped authored output takes its producer's type, or is wic036 ([13f6e95](https://github.com/PolusAI/sophios/commit/13f6e95538d19f40ddec91f16542f09f908ab62d))
* **ir:** declare the $namespaces a workflow's promoted formats use ([d5d8059](https://github.com/PolusAI/sophios/commit/d5d8059ab6505ff5474f9d1a49cc607a082ecff6))
* **ir:** promote an unbound stdin input as File ([642acb8](https://github.com/PolusAI/sophios/commit/642acb8c53eae460f56aa61bd3ffe3b121d93637))
* **ir:** wic011, wic012, wic020 and wic031 name their position and step ([5f40b3b](https://github.com/PolusAI/sophios/commit/5f40b3b4e65bcebc7c1de6290a15285aa59c7c4e))
* judge a cross-scope edge in the scope that owns it ([d888d1e](https://github.com/PolusAI/sophios/commit/d888d1ebb68c3eaa6f495cf873c8fbab0861d06e))
* judge a local edge in its own scope, not the whole tree ([b5ebbbb](https://github.com/PolusAI/sophios/commit/b5ebbbb782b19099efa871fb63aec82c9397ff5f))
* keep semantic gates on pull requests ([ad2f4f8](https://github.com/PolusAI/sophios/commit/ad2f4f8874cdf85fb5835f2bb682433b3d18ecde))
* keep tool ids absolute and record fields a collection ([207a4af](https://github.com/PolusAI/sophios/commit/207a4af59265a645f9d1d040396d54e96727cf82))
* **lang:** a !cwl record that contains itself is an alias cycle ([9645d8d](https://github.com/PolusAI/sophios/commit/9645d8dc938aca3bcf5dcffba414e6171a643ae7))
* **lang:** a desugared record is checked wherever it is written ([34bf365](https://github.com/PolusAI/sophios/commit/34bf365d5cf07d498ea0c28b9f08eda88b0782da))
* **lang:** a record that gives its input no value is wic038 ([8e693d3](https://github.com/PolusAI/sophios/commit/8e693d3bae61b9c57b100507fa25dd5cb76b5008))
* **lang:** a YAML tag the loader cannot read is a diagnostic ([9af061d](https://github.com/PolusAI/sophios/commit/9af061de3071b4ffaa1f5e9e7d7311ada749a6d6))
* **lang:** align manufactured-document diagnostics and claims ([02fae4f](https://github.com/PolusAI/sophios/commit/02fae4f73c9d83a53177e208f7c0f23b2a62340f))
* **lang:** an !ii scalar YAML cannot read stays text ([7d6071e](https://github.com/PolusAI/sophios/commit/7d6071ef4715ab2220c98f6008bca533e5a864f4))
* **lang:** an error does not call itself a warning ([deef9fe](https://github.com/PolusAI/sophios/commit/deef9fed70ada9a87aae9be099f47793f0216884))
* **lang:** an untagged step-input record in in: is wic038 ([1d3cf52](https://github.com/PolusAI/sophios/commit/1d3cf5273aadaefb2a294475c143e92fcc695e01))
* **lang:** keep the position in the wic036 hint and lint the positional outputSource ([77cd837](https://github.com/PolusAI/sophios/commit/77cd837d31a9d54b901fb1e2b2acdce1cc92f808))
* **lang:** key a list-form port by the name after the # of its id ([67bf709](https://github.com/PolusAI/sophios/commit/67bf70998e339cbb29ee665cf9d9ffbe6ff33c7f))
* **lang:** read a quoted !ii scalar as its text ([28c662e](https://github.com/PolusAI/sophios/commit/28c662ef098acf2938f985174e532887d0c76b6f))
* **lang:** read CWL's list form of inputs, outputs, requirements and hints ([72a2b6d](https://github.com/PolusAI/sophios/commit/72a2b6d2b5cbae5013c9738837d879a3c1308cfa))
* **lang:** the parser accepts YAML merge keys the loader builds ([14b683a](https://github.com/PolusAI/sophios/commit/14b683a75e7c76e2314207de1f3faa78a879aaca))
* **lang:** the parser builds YAML with yaml.SafeLoader, not wic_loader ([391c49d](https://github.com/PolusAI/sophios/commit/391c49da58d0303b09c1a3ca88d60f00879171d8))
* **lang:** the parser copies only collections and names an undefined core tag ([b495f08](https://github.com/PolusAI/sophios/commit/b495f08fe3f723747a75cabf764cc0420c9c03f1))
* **lang:** the parser rejects every core tag the loader cannot build ([8539313](https://github.com/PolusAI/sophios/commit/853931378c5dd795e76990d8d5f305fa19b382b5))
* **lower:** a rejected document never reaches the graph's own invariants ([efe5939](https://github.com/PolusAI/sophios/commit/efe5939857a45b6be83dea2d8154c33475f51505))
* **lower:** reject linkMerge, pickValue and list outputSource on outputs ([7488cb9](https://github.com/PolusAI/sophios/commit/7488cb97c62b85aab9511f317d839faba38a11e0))
* name a Python clt_path in wic013 and give the Python fix ([a0b6b87](https://github.com/PolusAI/sophios/commit/a0b6b87b640b329454215092783cf1eabf3e5481))
* name the input, type, and literal when a scalar won't coerce ([de52f38](https://github.com/PolusAI/sophios/commit/de52f38f9f352ea1b76586e27d92163569543e9e))
* name the undeclared-port mistake for what it is ([7339d00](https://github.com/PolusAI/sophios/commit/7339d00ec29b904ac3a8ae7febc154b5bcf57a80))
* **names:** count authored steps in the names map and flag inserted ones ([c5bebc4](https://github.com/PolusAI/sophios/commit/c5bebc4473b806273709e1961bd1d1ad5a15a8f0))
* never specify more than the loader accepts ([b462068](https://github.com/PolusAI/sophios/commit/b4620680c7b6d136415bb208e61ea3ff345d020c))
* **nextflow:** .path only on a File input, and messages that name the real cause ([e8a7304](https://github.com/PolusAI/sophios/commit/e8a7304dfe1f15edbe0d0608e0bafe87817226e6))
* **nextflow:** a $ not followed by ( or { is plain text ([b8355ce](https://github.com/PolusAI/sophios/commit/b8355ce9f155f14c99a86b60e4c9067cca7deed9))
* **nextflow:** a basename reference to an entry names the entry again ([474411b](https://github.com/PolusAI/sophios/commit/474411bc629222610136d99aceb763f0f3ddf946))
* **nextflow:** a containerized command of conditional tokens runs the entrypoint alone ([79f321d](https://github.com/PolusAI/sophios/commit/79f321d8a3fdaed5a00a50bc62acb4c7dee4f9f4))
* **nextflow:** a default is held as a value written on a step is, so an int default of a float is the float ([1bba584](https://github.com/PolusAI/sophios/commit/1bba58423611c2c365a5f62bfd9d9bff5a184dbc))
* **nextflow:** a glob or a stream target takes a File or Directory reference only as CWL does, never at the input's own staged name, and the import writes it with .path ([5564651](https://github.com/PolusAI/sophios/commit/5564651911233c17864db04b349eb7bc1ea37931))
* **nextflow:** a long or double input's default adds no finding of its own ([2222884](https://github.com/PolusAI/sophios/commit/222288488ed92a1e72c7eae1657fb71a65eb0a25))
* **nextflow:** a null check tests for the absent-optional sentinel ([bb37665](https://github.com/PolusAI/sophios/commit/bb37665369c942244658ccf489c35deab3497133))
* **nextflow:** a source read alone reads an entry's input as a path ([e6ea845](https://github.com/PolusAI/sophios/commit/e6ea84506b665b5997c91d21f330c782e426c0ec))
* **nextflow:** accept the InlineJavascriptRequirement Sophios adds for when ([9965a55](https://github.com/PolusAI/sophios/commit/9965a555905243456a5c7a2b7f753923bd056631))
* **nextflow:** adapt scatter to the array item-type signature ([95823b5](https://github.com/PolusAI/sophios/commit/95823b5cca2a1deedf1c08ae952cc414d958ac60))
* **nextflow:** an absent optional input whose own binding has a valueFrom is rejected ([136c31e](https://github.com/PolusAI/sophios/commit/136c31ed0f63bb1ef2edea3ffa16e67e55ffb06f))
* **nextflow:** an entry whose name the task directory holds stops the task ([4390f89](https://github.com/PolusAI/sophios/commit/4390f898d40cba80f66dc28e24defdc68c1e2e2b))
* **nextflow:** an import stops at the line holding the value it refuses ([fbc7db6](https://github.com/PolusAI/sophios/commit/fbc7db65fee3567e09dd747abccf5265c6832a83))
* **nextflow:** an optional File or Directory output is null when nothing matches ([cc01304](https://github.com/PolusAI/sophios/commit/cc0130453d5a7ef8b7f5e19c15a52b17a95930e1))
* **nextflow:** classify Dirent and array-schema fields in the support table ([b4a12e0](https://github.com/PolusAI/sophios/commit/b4a12e0d1b4f6ff31d00b8f9d2883e7745a19fa3))
* **nextflow:** CLI runtime fixture step name must resolve its adapter ([ce6547a](https://github.com/PolusAI/sophios/commit/ce6547a55dc08095d7f1d260028d702d83eb09b7))
* **nextflow:** close five holes in the array and absence contracts ([984182f](https://github.com/PolusAI/sophios/commit/984182f4bf7b9e4eef9f1756fa1eebf19109597c))
* **nextflow:** close Phase 1 semantic gaps ([4b3fced](https://github.com/PolusAI/sophios/commit/4b3fcedd153a7957b75ab8506682ee3308cbc3ec))
* **nextflow:** close scatter composition review gaps ([265a3a5](https://github.com/PolusAI/sophios/commit/265a3a59067d1dd88ae56094bd8b768b6db54e20))
* **nextflow:** close shell and staging review gaps ([4065a8d](https://github.com/PolusAI/sophios/commit/4065a8dbbe0267474dd51c70aa62c38c15631bf9))
* **nextflow:** close three review gaps in flag/prefix/source handling ([a566bed](https://github.com/PolusAI/sophios/commit/a566beda125b3b4a7529d35f44a9af50bf2ff9e1))
* **nextflow:** close two accepted-but-wrong binding corners ([be2042f](https://github.com/PolusAI/sophios/commit/be2042f437ff4a99089912ca4da480396a3e02b7))
* **nextflow:** CWL interpolation rules, sidecars by name, one Groovy escaper proven ([a2a9862](https://github.com/PolusAI/sophios/commit/a2a986237d03e0d287a1383e44b27f769e15891f))
* **nextflow:** deliver an array of paths to the script as a list ([3ff6c14](https://github.com/PolusAI/sophios/commit/3ff6c14562abab3b5e7e2bcfdaec593d250df251))
* **nextflow:** drop redundant docker/nextflow availability probing ([6724512](https://github.com/PolusAI/sophios/commit/672451214e3d51585e73ecb5e6f280873cb4c240))
* **nextflow:** enforce both sides of the array and cardinality contracts ([646cf9d](https://github.com/PolusAI/sophios/commit/646cf9d2b7b791437e32560a5dd338d9b094cc1a))
* **nextflow:** enforce executable boundary invariants ([96e0903](https://github.com/PolusAI/sophios/commit/96e09030f8504b315f1c144c83c208882a69431c))
* **nextflow:** every rejection is a finding with its path, aggregated with the rest ([75cf0e5](https://github.com/PolusAI/sophios/commit/75cf0e5357c840f8c04cb747fb997d6079d96ecf))
* **nextflow:** export NfFlag/NfCommandToken from the public API ([15cea9b](https://github.com/PolusAI/sophios/commit/15cea9b64a919a05852cf744e2173c5fd898a704))
* **nextflow:** fail closed on both flag lowering holes ([7f8787f](https://github.com/PolusAI/sophios/commit/7f8787f97a2f4843127890c0d05906eeb64e04c7))
* **nextflow:** fail closed on post-merge review gaps ([c97466c](https://github.com/PolusAI/sophios/commit/c97466c531120f005bf27a217e971cb3dc40306f))
* **nextflow:** fail Docker regressions in CI instead of skipping them ([b44bde3](https://github.com/PolusAI/sophios/commit/b44bde3b42ede60f1fcafb9f878b954eaab764ef))
* **nextflow:** finish propagating the touch/cat image swap ([46166dc](https://github.com/PolusAI/sophios/commit/46166dcba1fbca0be7f5cbb432b1ec5dfc497447))
* **nextflow:** five places a run differed from CWL or left stale state ([dd15f11](https://github.com/PolusAI/sophios/commit/dd15f1199a6f0fb1f07da1593e229e41154ea03d))
* **nextflow:** five places the backend silently differed from its source ([9769e4e](https://github.com/PolusAI/sophios/commit/9769e4e17efccf8f148a4b5d41600a71a898d8ea))
* **nextflow:** four places a run differed from cwltool on valid CWL ([a1ad40b](https://github.com/PolusAI/sophios/commit/a1ad40bd4425630ac09741ea4257138115086a7a))
* **nextflow:** ignore inert CWL metadata ([d1ae4dc](https://github.com/PolusAI/sophios/commit/d1ae4dc95c61500be5ff782fa3563a81f9298ce0))
* **nextflow:** index single-input scatter so its outputs keep input order ([ec7a9ab](https://github.com/PolusAI/sophios/commit/ec7a9abc7e1f9da149b19db9c626bfd0aaf87f57))
* **nextflow:** judge a scattered parameter against the adapter ([dec7daf](https://github.com/PolusAI/sophios/commit/dec7dafb28ecd07d0aeb5364d88cf311e7450756))
* **nextflow:** keep projection templates, and close the subset on hydration ([3851cb9](https://github.com/PolusAI/sophios/commit/3851cb9c82d24d0034e6c952b8a6bdc5abd07c3f))
* **nextflow:** layout before a script's opening quotes and after its closing ones is free, as is a def's indentation ([7c49c7f](https://github.com/PolusAI/sophios/commit/7c49c7fb17d733a4c829c106303283a968a21412))
* **nextflow:** let a glob collect an entry, and reject writes inside a link ([0da680e](https://github.com/PolusAI/sophios/commit/0da680e88d8ff809bceba740344923636d7ba996))
* **nextflow:** list unread parameters in sorted order ([c5a2606](https://github.com/PolusAI/sophios/commit/c5a2606ed942a4690e1a48fa1d7dc4950781f773))
* **nextflow:** loadContents on a binding has a note of its own ([7ea02be](https://github.com/PolusAI/sophios/commit/7ea02be6225fb5a6dde28c3cd449750ce283ef94))
* **nextflow:** lower from the step's run and the emitted workflow steps ([75f5a37](https://github.com/PolusAI/sophios/commit/75f5a377ddba5187af11932335ae9d594eaa7dda))
* **nextflow:** lower tools in canonical form ([31122c3](https://github.com/PolusAI/sophios/commit/31122c318ff95e3d165ddf56b0640435ef8e4021))
* **nextflow:** merge conditional step inputs two channels at a time ([3e3b13a](https://github.com/PolusAI/sophios/commit/3e3b13abdf1c903d88e55954a2dbcc2a8d61c8e6))
* **nextflow:** name a list of step-input sources and a step-only input ([2a2c15a](https://github.com/PolusAI/sophios/commit/2a2c15a8d5365197877cd90a0775694e2692e0d1))
* **nextflow:** name long and double where a type declares them ([33969fb](https://github.com/PolusAI/sophios/commit/33969fba82d1bcb1addb80b3ea6c0acd30e7e3b8))
* **nextflow:** name processes and parameters from master's step ids ([0a7deb6](https://github.com/PolusAI/sophios/commit/0a7deb611c714778dc2dd030d32e505da687914e))
* **nextflow:** name the rejection of a nested workflow input's default ([379702a](https://github.com/PolusAI/sophios/commit/379702a1ad501691579a5a81a82d224c3a442b66))
* **nextflow:** nest the output of a nested_crossproduct over gathered arrays ([ec81e87](https://github.com/PolusAI/sophios/commit/ec81e87146e1dbaf6c62957008e1e8f8b98e8498))
* **nextflow:** no parameter holds an absent File? or Directory? ([857ee2f](https://github.com/PolusAI/sophios/commit/857ee2ff7a47f96315c12cf9f942162513a03b41))
* **nextflow:** note warnings point at the caller of to_nextflow ([9ea464c](https://github.com/PolusAI/sophios/commit/9ea464c4c8cbe89de2a247939cd6118fa7c466b8))
* **nextflow:** pass a hand-written 0.0 or long for a float, and claim Python's str() only ([88a56bd](https://github.com/PolusAI/sophios/commit/88a56bd07fc606837639970d833c66ef9a7d21e9))
* **nextflow:** pin touch/cat adapters to debian:bookworm-slim, not ubuntu ([e8fc322](https://github.com/PolusAI/sophios/commit/e8fc3226a413e69689ced91022762be062746d7b))
* **nextflow:** preserve aggregated capability findings ([389b61f](https://github.com/PolusAI/sophios/commit/389b61fe77f23529c47f9f8a1409dfbd538ce179))
* **nextflow:** preserve output port type narrowing ([0d771d8](https://github.com/PolusAI/sophios/commit/0d771d8d418b5f1262617d248df39719cafa10e9))
* **nextflow:** print every float as Python's str() does ([2f7a562](https://github.com/PolusAI/sophios/commit/2f7a562eeee107c7c72056f7f539246e5370f44f))
* **nextflow:** print the non-finite diagnostic of a when on the console ([2c04a11](https://github.com/PolusAI/sophios/commit/2c04a11ddc025f6f0a098d46164d37d4b10c973f))
* **nextflow:** process and workflow names avoid the names a script declares ([ef720ec](https://github.com/PolusAI/sophios/commit/ef720ecfdeb60b3ff0ed07bda9b9257f35e207c1))
* **nextflow:** read a float parameter's text back as a float without the IR ([7805e6a](https://github.com/PolusAI/sophios/commit/7805e6ad77357165d3264c97602fbe3b3ad3c823))
* **nextflow:** read model-derived memory with the renderer's number formatting ([d573f72](https://github.com/PolusAI/sophios/commit/d573f72cf3e638f6c32350d282e2204f406511c3))
* **nextflow:** reject ++ and -- in the safe subset ([f48580f](https://github.com/PolusAI/sophios/commit/f48580fcefbf5f0ca77a048a55b23a38aeb4b690))
* **nextflow:** reject a $ in a shellQuote: false literal ([6efd893](https://github.com/PolusAI/sophios/commit/6efd893aae105f7b057b35c07ff41e3dca31719c))
* **nextflow:** reject a binding on an array of optional booleans ([e1a7dc3](https://github.com/PolusAI/sophios/commit/e1a7dc3744ded6967fababedec480d6fec348ad4))
* **nextflow:** reject a boolean array's binding ([7514932](https://github.com/PolusAI/sophios/commit/751493257e0d38f9d3d829910779cd58f8d7ef9f))
* **nextflow:** reject a conditional process with dotproduct inputs in the IR ([9c46bc4](https://github.com/PolusAI/sophios/commit/9c46bc4c4f576a42b12b0d870161092ea8f94077))
* **nextflow:** reject a conditional step with no inputs at lowering ([5f13cf9](https://github.com/PolusAI/sophios/commit/5f13cf926c9c4cd348f173d5d3cfeee088cad796))
* **nextflow:** reject a containerized command that can render empty ([a36743a](https://github.com/PolusAI/sophios/commit/a36743ae66ab584d7c6c5c7f050704e019142e92))
* **nextflow:** reject a process connection out of a dotproduct-scattered process ([60e822f](https://github.com/PolusAI/sophios/commit/60e822f45bae1d797f37a4939e030efd24ee097d))
* **nextflow:** reject a step-input default of a File or Directory input ([fe87ce5](https://github.com/PolusAI/sophios/commit/fe87ce51d7d12aba8d38dbd50ca136f8822bd103))
* **nextflow:** reject an output named like a link its task stages ([d977d98](https://github.com/PolusAI/sophios/commit/d977d9868fe94e6dfac2ae94e9d6d174f2e3443f))
* **nextflow:** reject array-backed flag references ([c415523](https://github.com/PolusAI/sophios/commit/c415523a1ba59be2d30edef36ba2bd00c5b26b13))
* **nextflow:** reject unconsumed source fields ([6fb0988](https://github.com/PolusAI/sophios/commit/6fb0988b05f8114cc54bb6ed4ab9f120ca4c6df6))
* **nextflow:** reject unsafe source semantics ([61ddca7](https://github.com/PolusAI/sophios/commit/61ddca77d2d6ac4f140ba1727acb8de390b3f496))
* **nextflow:** render an assembled output name literally ([32c4a0b](https://github.com/PolusAI/sophios/commit/32c4a0b9cf783d204c0013c6329893ceb93d1c2c))
* **nextflow:** render capture globs literally ([7858ed8](https://github.com/PolusAI/sophios/commit/7858ed85e15b0441d07cfc514cc9457aae792962))
* **nextflow:** repoint touch/cat adapters off the broken bash:4.4 image ([0ea0ab8](https://github.com/PolusAI/sophios/commit/0ea0ab86824156f0c3929ab3bfb1b0d5186c9add))
* **nextflow:** report basename source errors from capability analysis ([2b2b805](https://github.com/PolusAI/sophios/commit/2b2b805c4351cb48f2f7f2712119078789e12f25))
* **nextflow:** script variables avoid names the script resolves, and embedded File references are rejected ([c90c853](https://github.com/PolusAI/sophios/commit/c90c853628b44fd0dd9e7ebb8179feba2c795b0c))
* **nextflow:** stage each file a step input joins in its own directory ([85caa39](https://github.com/PolusAI/sophios/commit/85caa390e5a65c60e09286ae22e5d57c1bb4fb43))
* **nextflow:** stage every array-typed path input in numbered directories ([21af810](https://github.com/PolusAI/sophios/commit/21af810e1213d5d1a8b41bd8b83c03c710764d2e))
* **nextflow:** stage gathered files in numbered directories on a scattered consumer ([fb22cff](https://github.com/PolusAI/sophios/commit/fb22cff936493bd1714fd4e3db3ad0fe5b0419c8))
* **nextflow:** stop mislabeling absent required inputs as deferred-optional ([cedf323](https://github.com/PolusAI/sophios/commit/cedf323bf89c8bdeb769d62db011080c553d7893))
* **nextflow:** structural import writes no cwltool:CUDARequirement ([62ea51b](https://github.com/PolusAI/sophios/commit/62ea51b82219209f9f9b142b234fa28e00a462b0))
* **nextflow:** the entry check names a path that holds nothing of the declared kind ([29474d6](https://github.com/PolusAI/sophios/commit/29474d6cf5502f9c7550d48d5ab65794341c9008))
* **nextflow:** the entry check stops a non-finite float, another class and an empty path ([54169ad](https://github.com/PolusAI/sophios/commit/54169adc5f6fbee897c46b07d2b4b0a8f8d37053))
* **nextflow:** the generated pipeline stops under the v1 script parser ([362de3c](https://github.com/PolusAI/sophios/commit/362de3c62b207811aa43e421dcb110285424db5d))
* **nextflow:** the import reads each hole of a form once, in time linear in the line ([51b3631](https://github.com/PolusAI/sophios/commit/51b36315dd71880d7f01692dceab18b04a5ddaf4))
* **nextflow:** the key of a map-form envDef entry names the variable ([55b2375](https://github.com/PolusAI/sophios/commit/55b23752a125adae23ea9109e9ead076c4b33e11))
* **nextflow:** validate before serializing in write_nextflow_artifacts ([dcfb801](https://github.com/PolusAI/sophios/commit/dcfb80196b38d8deadf1dc9927c21fba4d76f147))
* **nextflow:** validate hydrated conditions and ports, and keep a glob's braces literal ([837470b](https://github.com/PolusAI/sophios/commit/837470bb98606d81b288a7f503484f5f24dc317b))
* **nextflow:** whatever bytes the three files hold, the import returns a model or stops with a code ([b844d8a](https://github.com/PolusAI/sophios/commit/b844d8a11ba70dda4032fb5c92cb236262ef2b62))
* **nextflow:** widen the flag-source guard's exemption to any mapping ([e64ae1b](https://github.com/PolusAI/sophios/commit/e64ae1b9c6067117a6d19cd9ea93a42279aa5cf4))
* normalise a converter's format to a list before flattening ([9431b35](https://github.com/PolusAI/sophios/commit/9431b35fd2504d6e9d7472f8d7ea22d8069b189e))
* normalise every format at its point of entry, not just the whitelist's ([130de95](https://github.com/PolusAI/sophios/commit/130de95af34f5b91afcde5cd874c43f2c73b5223))
* normalize inferred boundary formats ([ece5181](https://github.com/PolusAI/sophios/commit/ece518183182c78256819d046204b45e67076082))
* one .wic document, whichever door it comes out of ([47f5874](https://github.com/PolusAI/sophios/commit/47f58749c484e0efd09c9350b514bbf0b5ee41bb))
* one rule for what a workflow boundary may declare ([1892d3d](https://github.com/PolusAI/sophios/commit/1892d3d9f028ad3e198599c87635472693d02c7e))
* **plugins:** a stem defined twice warns, naming the file used and why ([e9805e6](https://github.com/PolusAI/sophios/commit/e9805e6b1e2a8f2e7701de2fcf83dd1b40a3e3f0))
* **plugins:** check only the part of a path below its search root for autogenerated/ ([375dd4e](https://github.com/PolusAI/sophios/commit/375dd4e4d4158cacfe1ad0a7baee674f92bf90f7))
* **plugins:** discover in sorted order and never under autogenerated/ ([e4c8871](https://github.com/PolusAI/sophios/commit/e4c8871c09185d5a6f5e6a86147c3bb11e96142c))
* **plugins:** leave emitted ids in paths alone and keep the job suffix ([c280452](https://github.com/PolusAI/sophios/commit/c2804522aec3bdabe67ce4ec87d6ff6d13ccd17c))
* **plugins:** read DockerRequirement from list-form hints and requirements ([b82a579](https://github.com/PolusAI/sophios/commit/b82a579ea808e2c8c26bfbe04f29da9fc7fa8e11))
* **post_compile:** an embedded process writes a single secondaryFiles pattern as a list ([6ba6ae9](https://github.com/PolusAI/sophios/commit/6ba6ae950628a17416705dd3a56e880f8d758ad9))
* **post_compile:** an embedded tool keeps the network and listing defaults of its cwlVersion ([afd0f81](https://github.com/PolusAI/sophios/commit/afd0f819629fa5d712418cba3a7b2da06a92226d))
* **post_compile:** an embedded v1.0 tool keeps its cwltool-namespaced declarations ([b8f13f7](https://github.com/PolusAI/sophios/commit/b8f13f7174177d7ffeabb667451d0adc41581693))
* **post_compile:** follow a list of sources when a call is dissolved ([c153043](https://github.com/PolusAI/sophios/commit/c153043b1072237ef56084ad521ab68f36c955df))
* **post_compile:** keep a call nested when partial failure gave it a when ([9a07262](https://github.com/PolusAI/sophios/commit/9a072625912202ef5ef06241b78cb91aaca0eae3))
* **post_compile:** say so when an embedded tool declares no cwlVersion ([c1de644](https://github.com/PolusAI/sophios/commit/c1de64459d4c9027704801a4bd8d193d39699f55))
* **pre-commit:** mypy runs as CI runs it ([691b9df](https://github.com/PolusAI/sophios/commit/691b9df74ce263ecd3e1efa15c5b07bf7f661220))
* **preflight:** a stopped engine is not blamed on socket permissions ([94786cd](https://github.com/PolusAI/sophios/commit/94786cdb29eee971d7fb3f501cf79de06c9f4397))
* **preflight:** an output-target directory is not a missing input ([ca1ecd5](https://github.com/PolusAI/sophios/commit/ca1ecd58b4d167cc104de1d5cd0da3ce7e316cae))
* **preflight:** complete the upfront image fetch, and report a failed one as wic037 ([fc47d86](https://github.com/PolusAI/sophios/commit/fc47d867612ba6cc37352779331c0451a96fff72))
* **preflight:** leave the root logger alone so cwltool can run in-process ([361e7f1](https://github.com/PolusAI/sophios/commit/361e7f1187120b6ae51ff2d591d89873cc3c7bcc))
* **preflight:** load each CWL document from its file URI on every platform ([8bd44e3](https://github.com/PolusAI/sophios/commit/8bd44e35414cd9851e70b781bebe64e71f487e6f))
* **preflight:** read a file: URI as the platform's path ([ccf8c5f](https://github.com/PolusAI/sophios/commit/ccf8c5fe811bcabba6e8ecf5fc231d6686253734))
* **preflight:** wic037 quotes the engine's error line, not the extractor's traceback ([5042bb7](https://github.com/PolusAI/sophios/commit/5042bb743a038dee1c7504f9401ba1871cd5f745))
* preserve inference and merged inline overrides ([266c19b](https://github.com/PolusAI/sophios/commit/266c19ba3fad3f2f0d6e92affde76a2ad28a8027))
* preserve legacy inference search boundaries ([935b30f](https://github.com/PolusAI/sophios/commit/935b30f204fa90b2b0461dfc17c25449c228cba2))
* preserve workflow call boundaries while inlining ([b06d3a9](https://github.com/PolusAI/sophios/commit/b06d3a93b3cc875ba6d944cc84568e1ee84a0a1a))
* refuse a packed CWL Workflow used as a step ([496eaa1](https://github.com/PolusAI/sophios/commit/496eaa1022abce7707cb69b0aaed1c44c6482f42))
* report a CWL Workflow used as a step instead of emitting broken CWL ([c6ce11a](https://github.com/PolusAI/sophios/commit/c6ce11ab68b9dda5e37d0ad9ab4dbee1f4a753db))
* report failures instead of terminating the caller's process ([2e2ef72](https://github.com/PolusAI/sophios/commit/2e2ef7294672acd30ae480b6e17242045f89b657))
* **resolve:** merge a bare-id sidecar key with the other spelling of the same step ([d7655ce](https://github.com/PolusAI/sophios/commit/d7655ce679ab50644c52e5cd00d40182793daa2b))
* **resolve:** merge a parent's bare-id contribution into the child's positional entry ([8359a5d](https://github.com/PolusAI/sophios/commit/8359a5d35e8452c2f4ab0b0afa16a107479f94a0))
* **resolve:** pair sidecar step keys only when both address a step ([5068bbc](https://github.com/PolusAI/sophios/commit/5068bbc30116ee1d7e973248b17d92ab1ef33dd5))
* restore the complete codespell allowlist ([1d100c5](https://github.com/PolusAI/sophios/commit/1d100c51bf23de3610725bb2c8a5faa9cdb2f732))
* **rest:** say that /compile leaves real-time analysis out ([9d50c50](https://github.com/PolusAI/sophios/commit/9d50c50e041cff9253630f0b65872024fbc20cc0))
* rewrite an authored outputSource to the step emission renames it to ([7aaaad6](https://github.com/PolusAI/sophios/commit/7aaaad685a432709513c91948baeaaa38d6d5895))
* **run_local:** an interrupted cwltool run terminates the runner's children ([fe67e33](https://github.com/PolusAI/sophios/commit/fe67e339142d5067cbd3fcb1b16c35958beeef59))
* **run_local:** delete nothing after a run ([4e584f1](https://github.com/PolusAI/sophios/commit/4e584f1693e45007ca270f8c7968349df62314a2))
* **run_local:** the failure summary covers a runner that raised ([2f38c72](https://github.com/PolusAI/sophios/commit/2f38c7283f3bf7f9fdcccfcb225b78a8345e0dee))
* **run:** --cachedir reaches cwltool ([9b534c6](https://github.com/PolusAI/sophios/commit/9b534c640c274256ad74f7016ae57babd5d766cb))
* **run:** a Nextflow --check writes into a temporary directory and leaves the base path as it found it ([fdce796](https://github.com/PolusAI/sophios/commit/fdce79699587d9339320fd4e9da41170d19371f3))
* **run:** a Nextflow run writes and runs its pipeline in nextflow_&lt;workflow&gt;/ ([ebcbeba](https://github.com/PolusAI/sophios/commit/ebcbebac4d2056d956290f7c624eee3db71f9580))
* **run:** a Python Nextflow run lowers the declared outputs and raises ValueError ([e6a8ba1](https://github.com/PolusAI/sophios/commit/e6a8ba1a480900e0cf24fc18ad24533813419ab8))
* **run:** check the container engine only when a step runs in a container ([53f3faf](https://github.com/PolusAI/sophios/commit/53f3faf8fe22ee41b449018defd09e38e647992e))
* **run:** name a missing or unreadable input path before anything is pulled ([4d86064](https://github.com/PolusAI/sophios/commit/4d86064a7621c49b31e0f0fc1a554a4bbf1e977f))
* **run:** name the runner's default output directory after the workflow ([f907cd7](https://github.com/PolusAI/sophios/commit/f907cd739db2a03e0119f7a4ba383951cb290887))
* **run:** pass user env var values to the runner unchanged ([14f6768](https://github.com/PolusAI/sophios/commit/14f6768e59a66147b54f0bd997099f32f4174b14))
* **run:** quote and normalise the run script ([6c7502e](https://github.com/PolusAI/sophios/commit/6c7502ea9be54dda42ebd974aa44edd95401beb2))
* **run:** say why an in-process run failed, with or without a cachedir ([2616e7d](https://github.com/PolusAI/sophios/commit/2616e7d5317f29f22d7ec9e1d5fa56d5876e5e06))
* **run:** stage Directory inputs beside the workflow ([d652c5f](https://github.com/PolusAI/sophios/commit/d652c5f2e9adebd49eabbd5e2d2595456c02df96))
* **run:** the Nextflow check reads the inputs and names only Nextflow's remedies ([4d86d29](https://github.com/PolusAI/sophios/commit/4d86d291f7c080e2bb2164b697cc3bdde2869ebc))
* say where a call is marked wic: inlineable: false ([71e1df5](https://github.com/PolusAI/sophios/commit/71e1df503f42643752ece9d766408af536d6f205))
* say which types were compared, not which were declared ([6f6a086](https://github.com/PolusAI/sophios/commit/6f6a08685bc82f426a3dc6ea4cac199f6fb6886f))
* scan string constants, and convert the copies that scan finds ([1c28d74](https://github.com/PolusAI/sophios/commit/1c28d744a8e77b8f7307058789cce99c0d54d0f8))
* schema generation accepts a workflow input declared by its type alone ([e469e8d](https://github.com/PolusAI/sophios/commit/e469e8ddd88d79711acb16f107569ead9e577d25))
* stop the runner however Sophios ends ([5326821](https://github.com/PolusAI/sophios/commit/5326821eb70bc9ca579d0a195c2cde07072e3217))
* supply the subinterpreter's locations as a binding, not an edit ([00e477c](https://github.com/PolusAI/sophios/commit/00e477c25d4ab56cc4a55c31b2ee54c11b6bfc95))
* **tests:** make validate_cwl independent of cwltool's annotations ([32f05cb](https://github.com/PolusAI/sophios/commit/32f05cb70421d0e8b0461985a62886091b3ee071))
* **tests:** REST bbbc runs use the rebuilt image ([22c95fe](https://github.com/PolusAI/sophios/commit/22c95fe0c969d0f5878ad458a1ad71bbbd1762c0))
* **tests:** the REST runs pull their images with cwl-docker-extract first ([c64dbe4](https://github.com/PolusAI/sophios/commit/c64dbe47c1b2ffd858cf16197a876f62c880acb6))
* the edge-definition rule is positional, and both spellings obey it ([b3b3535](https://github.com/PolusAI/sophios/commit/b3b3535f9a4f146603e6c2908709464baa13db69))
* the emitted `in:` starts empty, not as the source spelling ([4b1f5e0](https://github.com/PolusAI/sophios/commit/4b1f5e096526852a4359dc50c1a22e9d4e40def8))
* the last producer of the removed step form, and tags on structural nodes ([26ddb56](https://github.com/PolusAI/sophios/commit/26ddb561199913173f8a0fa3556e90825de4580a))
* the namespaced run: target is a function of identity, not of itself ([1ce2ccc](https://github.com/PolusAI/sophios/commit/1ce2ccc701061420b5cefa9db62af4f6be905313))
* the pins walk survives the recursive trees the loader builds ([ccb8063](https://github.com/PolusAI/sophios/commit/ccb806393e08c497553b410521a8e984010bc844))
* the poisoned subprocess needs tests/ on its own PYTHONPATH ([00120bb](https://github.com/PolusAI/sophios/commit/00120bb111de1bd97ea8af53b8a380da3e64fd7d))
* the sidecar case in the cycle contract was not a cycle ([9d752c0](https://github.com/PolusAI/sophios/commit/9d752c0feb8d5d19bec4225c903a3a3921cf54a6))
* the tool builder writes CWL through the one YAML writer ([169f7ac](https://github.com/PolusAI/sophios/commit/169f7ac35de1a8e3c22899f9c2c69a3400735434))
* the typed path drops a requirements: shape it does not model ([f30a7a4](https://github.com/PolusAI/sophios/commit/f30a7a4172c8adcfbd65b1ebbb18550b09e2d190))
* the unknown-tag rule belongs to one function, not to each position ([d995c4d](https://github.com/PolusAI/sophios/commit/d995c4ddec1f51dcee48bad9cae575afd87b1bb4))
* the weekly tests the ref it was dispatched on ([6c4ca08](https://github.com/PolusAI/sophios/commit/6c4ca08b66c91979f5493b0bf9f10ca4da0cfe7b))
* the writer transcribes, recurses, and is total over the union ([09a5cc0](https://github.com/PolusAI/sophios/commit/09a5cc0b1389385c1c2784003b9c654dc54df623))
* type errors the repo mypy catches and the hook does not ([4ff43ec](https://github.com/PolusAI/sophios/commit/4ff43ec47117d5ce61f8288dfa1b5f065969f022))
* unwrap a literal once at the job boundary, not per branch ([ace421b](https://github.com/PolusAI/sophios/commit/ace421b69fc3c51308fa762c65ef1523fd5f96e8))
* wic011 stops suggesting --allow_raw_cwl ([3d3e87c](https://github.com/PolusAI/sophios/commit/3d3e87c283e9aa680867d8bf653cdeec29db6635))
* wic021 names both readings of a single-key step entry ([af84f1e](https://github.com/PolusAI/sophios/commit/af84f1ed8782583ce733941f786f3a8a48d4e6d8))
* write out the return type so the match is checked ([16b44b5](https://github.com/PolusAI/sophios/commit/16b44b5108b28430b5e5162a284e746a2c53ef8c))


### Performance Improvements

* **ir:** cache the hash of a step id and a derived name ([a5b760a](https://github.com/PolusAI/sophios/commit/a5b760a198985e74cea2efa825d92d496387b2ad))
* **ir:** spell a reference only when it is reported ([a5a1749](https://github.com/PolusAI/sophios/commit/a5a1749e493095b1d089232c208f9f83a8ffcb89))


### Documentation

* --basepath chooses where files go ([e26257c](https://github.com/PolusAI/sophios/commit/e26257c87469000c935c7674e8d9d5dc76b6d604))
* !cwl cannot name a step that emission renames ([08b4234](https://github.com/PolusAI/sophios/commit/08b42343b404297a6881deae1637ebedb0e51141))
* a Nextflow run's pipeline, work directory and log are in nextflow_&lt;workflow&gt;/ ([5caf651](https://github.com/PolusAI/sophios/commit/5caf65194846be2be70066281128e0a061b67acf))
* a tool's input may not be named like an attribute of a step's inputs, such as _store ([7d80c1f](https://github.com/PolusAI/sophios/commit/7d80c1f64e9b39b353cc22cacd56f0b6959af838))
* a value bound on a workflow input is in the job compile() returns, and the .wic write_wic writes leaves it open ([0a359a9](https://github.com/PolusAI/sophios/commit/0a359a94853d684b041b71f9f2ef6d1d15d0d5f2))
* a whole-number default of a float reaches the command as the float ([1cfeeaa](https://github.com/PolusAI/sophios/commit/1cfeeaadf7dedff20612fb325db4ce37d09f0b61))
* add the approved core refactor design ([c071b76](https://github.com/PolusAI/sophios/commit/c071b76c56f78cb2f5c7eca4a4a945bc185ddc74))
* **advanced:** how inference chooses, and how to pin it ([71c942f](https://github.com/PolusAI/sophios/commit/71c942f05bfa1f5386be4c0f580a0be7f5622b90))
* **advanced:** pinning an inferred edge across subworkflow calls ([3272fd7](https://github.com/PolusAI/sophios/commit/3272fd777c1ec334089dd3656519c5bde5b94dfa))
* an error-codes page with the fix for every code ([b7a310c](https://github.com/PolusAI/sophios/commit/b7a310ce1bbb7b91a95a5d5a4d27c5ae361a9d4a))
* **api:** list the ValueError Workflow.run raises for a bad env var name ([6e53008](https://github.com/PolusAI/sophios/commit/6e530082c658391f849cd3c822f22e13bb7c32ad))
* **api:** say validate() neither loads nor checks $schemas ([3215c88](https://github.com/PolusAI/sophios/commit/3215c88a3960979bef7fa52c2b916f2c45bc6f1a))
* benchmark rationales that the compiler fixes made false ([d79a707](https://github.com/PolusAI/sophios/commit/d79a707002db2f87ee07d11c52980c709e48331b))
* benchmarks fail on a raising case, not timings ([af4956c](https://github.com/PolusAI/sophios/commit/af4956cfa0be1a4aa764cad8e74b51659f87756f))
* **changelog:** a missing --yaml or --inputs_file, and an unexpected failure, are one line ([834f901](https://github.com/PolusAI/sophios/commit/834f901692d14021156564e6713fe11466004b40))
* **changelog:** every break since 0.6.0 ([45fce55](https://github.com/PolusAI/sophios/commit/45fce5581a44ad8d57b9b1bcd87002df22501363))
* **changelog:** note that a CWL Workflow as a step is refused with wic013 ([e9cea3f](https://github.com/PolusAI/sophios/commit/e9cea3fb7ab184512c354a02ff7e83c06acd51ae))
* **changelog:** note the parser's agreement with the YAML loader ([3405f59](https://github.com/PolusAI/sophios/commit/3405f596c2e7049186869d25aea327d92ea9e493))
* **changelog:** note the user env var changes ([2786cb4](https://github.com/PolusAI/sophios/commit/2786cb4e6fcd53f01acff2ede943bc2c2ab2073b))
* **changelog:** the command line reports an unwritable autogenerated/ as wic021 ([38adf58](https://github.com/PolusAI/sophios/commit/38adf584bd739dd7977ebf25ecd19a8d678ae208))
* **changelog:** the pre-flight checks the directories a run writes to (wic021) ([369a5f3](https://github.com/PolusAI/sophios/commit/369a5f369c4802db545f1dae31051ae8473c0720))
* **changelog:** upgrade notes from 0.6.0 to 0.7.0 ([d24e0bc](https://github.com/PolusAI/sophios/commit/d24e0bc594a518dfa6947ac0b2f2319f1c9095f7))
* **cli:** --basepath help names the Nextflow files ([7b6eafa](https://github.com/PolusAI/sophios/commit/7b6eafa3a9ee44e51e62ed1253919aa04bcd18a1))
* **cli:** --cwl_inline_runtag embeds every process ([813626b](https://github.com/PolusAI/sophios/commit/813626b2a0b33b2eb6a362422204070e61a7c92f))
* compiled CWL names and one-directory tree ([859c738](https://github.com/PolusAI/sophios/commit/859c738680270b4d69d673ba3368f086d9c0986b))
* **compute:** a run written in place returns its output folders alone, and names no outputs folder ([32752c7](https://github.com/PolusAI/sophios/commit/32752c759c227e3a5d235d17883e50638b2bc4d9))
* **compute:** the docs' examples print no None ([07f4c69](https://github.com/PolusAI/sophios/commit/07f4c6901776134c591917ec3845f060b3ef7c16))
* **compute:** where each output lands, and which folders are refused ([b096e7b](https://github.com/PolusAI/sophios/commit/b096e7b5e3cf4c6146f943cd3de69f2e9a4e6a66))
* **cookbook:** declare the conditional input as boolean ([dbae278](https://github.com/PolusAI/sophios/commit/dbae27891a665fd94861ca33303a759451279bfa))
* **cookbook:** named outputs hide step outputs only at the top level ([97f11bb](https://github.com/PolusAI/sophios/commit/97f11bb71c850dece349712e5c8e80dfd2135317))
* **cookbook:** one runnable example per construct, on both surfaces ([2e37e8d](https://github.com/PolusAI/sophios/commit/2e37e8d59fcc8264eb8f597993ee9044075d318b))
* **cookbook:** the inference note points at the step's line ([f52f050](https://github.com/PolusAI/sophios/commit/f52f05042702c37db2b3a436b6056ecb24edd6ce))
* define the .wic language and reconcile both front-ends ([fd4cce5](https://github.com/PolusAI/sophios/commit/fd4cce5a4d17ce6dbf34e490274656e233976688))
* **design:** record pass-through as a second meaning of --allow_raw_cwl ([980c038](https://github.com/PolusAI/sophios/commit/980c038c9983c0ee67b1017c51c57db4e6c13e28))
* **dev:** an empty wic: block renders as {}, never as a null ([c4b0f3e](https://github.com/PolusAI/sophios/commit/c4b0f3e72118a3d43aa586af8a6c6a131b7da231))
* **dev:** the language spec for implementers, with the support matrix ([965502f](https://github.com/PolusAI/sophios/commit/965502fc9f5998d607a3c7646f81af10de9f293c))
* drop the module the retirement deleted ([ccddc17](https://github.com/PolusAI/sophios/commit/ccddc179930eb128072c7298e900a031cb042feb))
* drop the per-PR review scratch from the repository root ([fd75ed3](https://github.com/PolusAI/sophios/commit/fd75ed3e8f9612562b21fe06d97a5fd2ca624b99))
* EMPTY_NAME's comment has no colon for napoleon to split ([8fd9b74](https://github.com/PolusAI/sophios/commit/8fd9b7429105db86bce32606a2843cd2c4dd2419))
* **errors:** wic021 describes the one check that reports it ([3874738](https://github.com/PolusAI/sophios/commit/387473830b8ba805045d8696a0901c5ab978f2d1))
* **errors:** wic021 names every directory the writable check covers ([98f5834](https://github.com/PolusAI/sophios/commit/98f583485b5f7410d27d6c415dbb15c019aa8b17))
* **examples:** secrets_echo declares its secret as a workflow input ([3f8cf0f](https://github.com/PolusAI/sophios/commit/3f8cf0f5cd065c0bc7bb0d91f1f7c67397b30689))
* **guide:** point the intro at the error-codes page ([b89e82f](https://github.com/PolusAI/sophios/commit/b89e82f8768788c880b33448ce6322294027385e))
* how to run the Nextflow target, and how options reach Nextflow ([dc19afe](https://github.com/PolusAI/sophios/commit/dc19afeda332a860d793f4ca53989c2308328ef0))
* identifiers that dereference, claims that live once ([fedf8d1](https://github.com/PolusAI/sophios/commit/fedf8d14d7545326cbc75d5358b59046c508db39))
* importing a pipeline Sophios wrote, what stops it, and what it does not bring back ([1065d15](https://github.com/PolusAI/sophios/commit/1065d1579abc2325e907fd8e206a61057386c7d7))
* **install:** the wic037 sample names the image touch and cat pull ([8a1d1f9](https://github.com/PolusAI/sophios/commit/8a1d1f9a2be3013d4150834de3c9851c0537b981))
* ir docstrings state contracts, not history ([97dfdab](https://github.com/PolusAI/sophios/commit/97dfdab11d217648d438b802483af6ebc5c0e2c1))
* lang and compiler docstrings state contracts, not history ([cc82fd4](https://github.com/PolusAI/sophios/commit/cc82fd46a74281b67f132fd364e7bf805a1480fc))
* **lang:** clarify manufactured construct coverage ([ca27a48](https://github.com/PolusAI/sophios/commit/ca27a483e484758baa2d611ebe8387bfd28e9644))
* **lang:** say where a rejected field is positioned ([ccae007](https://github.com/PolusAI/sophios/commit/ccae00745d999c1f5c66b160ad090106ba476857))
* **language:** a positional outputSource holds beside inferred edges ([8c0bc40](https://github.com/PolusAI/sophios/commit/8c0bc40e2c578abf42dda2ae8ef30b73430e9056))
* **language:** a record with several sources takes each from its own document ([4f8e7d5](https://github.com/PolusAI/sophios/commit/4f8e7d50acb3287c468ee12b4a3af64132a089b2))
* **language:** a self-containing alias is wic030 unless its key reports first ([739994b](https://github.com/PolusAI/sophios/commit/739994b176e25b412f604b29ffdfb5b2a0c187dc))
* **language:** a user guide for the language, with every error code ([0ec0a11](https://github.com/PolusAI/sophios/commit/0ec0a118f81aaa1b83402a14fe661d955bc48cfc))
* **language:** an inline run body is written at the workflow's CWL version ([8d4ea94](https://github.com/PolusAI/sophios/commit/8d4ea949146fdbcb2f0f51ffbaf460ae9d9604c6))
* name the language Sophios in the renderer and schema ([0be6d5d](https://github.com/PolusAI/sophios/commit/0be6d5d670b066a363c7524bf05f786540f16369))
* name the language Sophios, and its version tag lang_version ([b3b68af](https://github.com/PolusAI/sophios/commit/b3b68af51b6dc89aad52b1cb8efb8a9b885e2d3c))
* **nextflow:** --check leaves the base path as it found it ([9dfe5c1](https://github.com/PolusAI/sophios/commit/9dfe5c18f83b916461ac544d79197c17fe12b78b))
* **nextflow:** a writable output directory must exist when the pipeline starts ([885d583](https://github.com/PolusAI/sophios/commit/885d583961858deab26bb0aba836c43979b4ddac))
* **nextflow:** arbitrary JavaScript is what is ruled out ([aac1144](https://github.com/PolusAI/sophios/commit/aac1144bdf6fc58822e2b20ecda4ace979d4e6e5))
* **nextflow:** Args/Returns docstrings on every public callable ([d189598](https://github.com/PolusAI/sophios/commit/d1895989c26d5b2ec5b88e3f748d57620f576692))
* **nextflow:** conditional workflows round-trip as opaque structure ([47e88cb](https://github.com/PolusAI/sophios/commit/47e88cb4354574867a674a05dc1c6163c85dd5a5))
* **nextflow:** correct public entry points ([e71c273](https://github.com/PolusAI/sophios/commit/e71c2739adc7c3f1ee188dff8c1dbc4143964e8c))
* **nextflow:** define Lowered as nf_support.py does, and name the document keys ([ffd12a1](https://github.com/PolusAI/sophios/commit/ffd12a19ef247653d3e74514368e8c1dc1e9454d))
* **nextflow:** describe recursive nested projection ([da528e4](https://github.com/PolusAI/sophios/commit/da528e432090e14ac38e5bf1c40b605f9380d38c))
* **nextflow:** drop the stale non-optional qualifier on boolean flags ([92a991b](https://github.com/PolusAI/sophios/commit/92a991baf5f897188093ff4069ef93b78c48fab4))
* **nextflow:** name what the corpus lane runs and how to dispatch it ([dec0615](https://github.com/PolusAI/sophios/commit/dec06151f50889d8f1b74b6f966a99a120d5fdae))
* **nextflow:** render the support table into docs/nextflow.md ([e12e8bf](https://github.com/PolusAI/sophios/commit/e12e8bf5135ef1f90b19f1204e73e5e2fdde6395))
* **nextflow:** say when the broadcast source is a queue ([a8c99c1](https://github.com/PolusAI/sophios/commit/a8c99c14990565ac820436f8fa0ef24b7d1dc0dd))
* **nextflow:** say which input option the reader takes from text ([161feff](https://github.com/PolusAI/sophios/commit/161feffb346cedced9cbf58e7794baa3b8dc0856))
* **nextflow:** state the length check as dotproduct-only ([ba2d428](https://github.com/PolusAI/sophios/commit/ba2d4288aa1af7c91b850086acb7abe5c79f0574))
* **nextflow:** the corpus runs in Run Workflows on a push ([8a87b7e](https://github.com/PolusAI/sophios/commit/8a87b7eca1baaddceeff1d0a7ad47744567a523b))
* **nextflow:** what a glob and a stream target take of a File or Directory input, never its own staged name, and the import's .path ([ac566d9](https://github.com/PolusAI/sophios/commit/ac566d94c3393823b482030b2a57a13d535e4b8b))
* only a code that shipped in a release is never reused ([ee2f0a2](https://github.com/PolusAI/sophios/commit/ee2f0a27d278f26ebe9cf47a4036bcffca16ca9f))
* podman pulls with podman, and shipped images are loaded before the run ([ceb5f1f](https://github.com/PolusAI/sophios/commit/ceb5f1f07a8d4b4cea0800268ab80e3ce2f04b70))
* private helpers do not carry generated-documentation blocks ([5ae1272](https://github.com/PolusAI/sophios/commit/5ae12722e0e62f6432cee83e751b09a017828e90))
* quote the cwltool step id as the run prints it ([47c6af2](https://github.com/PolusAI/sophios/commit/47c6af2052c067acf35ddada1da0ff4f23bafda4))
* quote the wic043 note as the compiler prints it ([66df4a0](https://github.com/PolusAI/sophios/commit/66df4a091c0bf49a65f5f29204c54e336599b693))
* record the [#403](https://github.com/PolusAI/sophios/issues/403) adversarial review and its mutation table ([b9e4ace](https://github.com/PolusAI/sophios/commit/b9e4acee0a346cf0d96210e38c7b45ca22c061c3))
* **reference:** §1 is the generated support matrix ([063afdc](https://github.com/PolusAI/sophios/commit/063afdce26fb2486c5aec65abd4893fc131a8052))
* **reference:** sidecar step keys ([72c95ba](https://github.com/PolusAI/sophios/commit/72c95bac63620bf13ae30fa1daf79ea8805e8088))
* **reference:** the generated spelling is named on stderr ([a68dc83](https://github.com/PolusAI/sophios/commit/a68dc83127da15e953ee347c20bb1e33ab53ce71))
* **reference:** the positional outputSource spelling ([9e53573](https://github.com/PolusAI/sophios/commit/9e5357360b3de3dbc5d59f9a7aebc1df65f801ea))
* **reference:** the step-input record is the fifth input form ([1c1ec6d](https://github.com/PolusAI/sophios/commit/1c1ec6d9e2b03af583a3e5e768e62849b97c2445))
* retire sophios_language_reference.md and relink ([39b60c3](https://github.com/PolusAI/sophios/commit/39b60c3a3100852c29a19b1d6f6008d42dd721a2))
* rewrite the documentation around the two forms ([fe9dfe9](https://github.com/PolusAI/sophios/commit/fe9dfe9a25db39e82d5ef34557acafaf3ed5fe35))
* run Sophios on a lead compute node ([6d039fd](https://github.com/PolusAI/sophios/commit/6d039fd06c929e3b5c57cc0ced139673fda6c6e6))
* say what a .wic step id runs, and make multistep1_pyapi.py the tutorial's twin ([af259e3](https://github.com/PolusAI/sophios/commit/af259e323917894ca4fabe4e83e610fcfea91ec5))
* say what Sophios is up front ([c39449d](https://github.com/PolusAI/sophios/commit/c39449d0476275eceb6d3db458bbc5346d37e85e))
* Sophios checks the machine itself; --check is the check without a run ([8667751](https://github.com/PolusAI/sophios/commit/86677511af07109331affc9ae403c7e6a4c6d304))
* start(), the run handle and run_async ([f94aa75](https://github.com/PolusAI/sophios/commit/f94aa75638f225b0aecb8c94cc6c67f6d35c7f52))
* the API reference builds again ([6fe88ea](https://github.com/PolusAI/sophios/commit/6fe88ea3d010116825a55a34879a78bc087e9971))
* the CWL you still need, with the builder call for each concept ([d32baea](https://github.com/PolusAI/sophios/commit/d32baeac6f0f6a4ac0a390ae577fd4cc78bce298))
* the editor schema is syntax, generated from the AST ([a2e4317](https://github.com/PolusAI/sophios/commit/a2e431724c5e28a9229bd485a906b7a4120bd97b))
* the first-workflow guide names the image touch and cat run in ([9c079d8](https://github.com/PolusAI/sophios/commit/9c079d8257abe143fb546e5df7c504c0b391ae4c))
* the guide follows one growing pipeline ([752bd92](https://github.com/PolusAI/sophios/commit/752bd92d172c253e6024597cf8d3739ff6fcced5))
* the inserted-step note cites a form the reference no longer lists ([924e52d](https://github.com/PolusAI/sophios/commit/924e52ddb9323ea3de39d99656b2b153bbcc45bd))
* the JSON diagnostic format ([8d0961c](https://github.com/PolusAI/sophios/commit/8d0961cf94a2ff75a2eeca2e741ade5934ebf2c8))
* the last private field block, and an example with no referent ([1c87a0a](https://github.com/PolusAI/sophios/commit/1c87a0a974ab1e6c9f09539492f9fab3106060cf))
* the parse obligation covers what the Python API writes as files ([fefdbd7](https://github.com/PolusAI/sophios/commit/fefdbd7d1731c2acb13b1a50085cf7e9a292c7c8))
* the run path, its log and Toil's outputs ([08b6921](https://github.com/PolusAI/sophios/commit/08b6921d41f4a9e3e629f24d9a8aae0b4e2f0a34))
* the sequence-of-single-key-mappings form worked until 9758e81 ([09ee57a](https://github.com/PolusAI/sophios/commit/09ee57a90cee32d2d0821c08aef3fb61381f6a2d))
* **tool_builder:** a docstring on every public method, naming the CWL it writes ([79ea31e](https://github.com/PolusAI/sophios/commit/79ea31e6e8e0e186aef5abb4fae9f06e6c1e4fc7))
* **tool_builder:** Input.binding_extra also writes inputBinding ([545a626](https://github.com/PolusAI/sophios/commit/545a626ccd48fe50574a413f9992bf5390a2f1b8))
* tutorials and the advanced guide use the step form that parses ([129a4c3](https://github.com/PolusAI/sophios/commit/129a4c32eab16f47c6e6275a7d3d49809ecadb71))
* two Raises blocks that restate the signature above them ([09e04f4](https://github.com/PolusAI/sophios/commit/09e04f4d06bbe061e8c0b6713936a34d812d3010))
* **userguide:** chained scatter from Python ([0aa62c6](https://github.com/PolusAI/sophios/commit/0aa62c69bb5f4c4c144dfcd584456c8bc8592010))
* **userguide:** load a .wic into Python ([15eb643](https://github.com/PolusAI/sophios/commit/15eb643f2f541d55ca51e6ed0be8c87940813e7d))
* **userguide:** scatter a producer before the step that scatters over it ([b3bbaf5](https://github.com/PolusAI/sophios/commit/b3bbaf5e136e5e94a613c141282b726a7c6fecb0))
* **userguide:** what write_wic writes ([969b79c](https://github.com/PolusAI/sophios/commit/969b79cd044bcf63bc5272d3055dd9ecac2785b0))
* what Sophios says when it fails unexpectedly ([4c1377e](https://github.com/PolusAI/sophios/commit/4c1377eff8f3f478881d78de4ee37ea1c83f291f))
* what the Nextflow run checks, the options it passes, and that CWL is one of two backends ([5004d54](https://github.com/PolusAI/sophios/commit/5004d549e563335e5021bb5c9aa4ae1a0bd00d8f))
* wic025 covers references inside included documents ([eba0ff1](https://github.com/PolusAI/sophios/commit/eba0ff1918cd879754e37fac3550226bb01fd831))
* wrap the two link docstrings over 120 columns ([01bb31f](https://github.com/PolusAI/sophios/commit/01bb31f6ad8514e5fe585cffdd00ece913b69405))
* write wic: as a literal in the new error-code comments ([ba1862e](https://github.com/PolusAI/sophios/commit/ba1862ed94a5cf973255f36033243e79b2df5f90))


### Code Refactoring

* a step is described once; Emit derives in: and out: ([a7b205f](https://github.com/PolusAI/sophios/commit/a7b205faa228ebb8b0abb46514927b62790b2851))
* **api:** a run handle waits for a status and collects a result; cancel says whether it cancelled ([eb9f32b](https://github.com/PolusAI/sophios/commit/eb9f32b739d17d1fe8abf2443d07a60fcab4ebaa))
* **api:** binding a Path reads nothing from the disk ([e1c396e](https://github.com/PolusAI/sophios/commit/e1c396e782e74dc5011a7bbeb344cdfe2d5d81b0))
* **api:** never load $schemas when validating a tool ([28b8c6d](https://github.com/PolusAI/sophios/commit/28b8c6d9d61b5645175dc28c5755646891d1e4b1))
* **api:** remove Workflow.add_input and the string-keyed binding methods ([bcc1eff](https://github.com/PolusAI/sophios/commit/bcc1effb173bab77615d823fa31a81bd7c05d45d))
* **api:** write_wic is the only way to write a .wic ([cb74a00](https://github.com/PolusAI/sophios/commit/cb74a009c7f93672e1c6817e1cafe2819c0f0367))
* **api:** write_wic writes a self-contained bundle with authored names ([df9316f](https://github.com/PolusAI/sophios/commit/df9316f0b0588a1af6329706f6373c69c1a9993a))
* canonical emission; no IR type carries a field order ([c69005b](https://github.com/PolusAI/sophios/commit/c69005bd1067a8fb6eea3fd2cef67c31bd50a329))
* **compiler:** accept a positional outputSource beside an inferred edge ([51f29f0](https://github.com/PolusAI/sophios/commit/51f29f08b02f6be2d67927745a77261e10d98c35))
* Complete runs twice, and says each boundary rule once ([f069b7c](https://github.com/PolusAI/sophios/commit/f069b7c473891de40f528749478a4d28aee666a2))
* Complete states facts, Emit spells the document surface ([6efec51](https://github.com/PolusAI/sophios/commit/6efec51fd5b4eb90cbeeccae9d77374f608affe4))
* **compute:** a compute run is followed as a local one is ([30143e1](https://github.com/PolusAI/sophios/commit/30143e17fb8f12c572743bbb1f958f62831b1312))
* **compute:** one RunConfig holds everything of a request but the workflow and its inputs ([830e0bd](https://github.com/PolusAI/sophios/commit/830e0bdf16bf811ac435c84809cf42e667ba3279))
* **compute:** RunConfig is a builder, made with where the outputs go ([4041808](https://github.com/PolusAI/sophios/commit/404180840e5cc11bec688b1e21fee96b32ecd6b0))
* **compute:** Service takes a URL; the tunnel is the user's ([6c4a567](https://github.com/PolusAI/sophios/commit/6c4a5675ef335df073f6767f117ea08b6c01e15a))
* **compute:** the compute error codes stay inside sophios.compute ([1de247a](https://github.com/PolusAI/sophios/commit/1de247a4c3500dfbb2c4e672bf679768c7ed49d0))
* **compute:** the cutover to the compute service's contract 2.0.0 ([8d3d51a](https://github.com/PolusAI/sophios/commit/8d3d51a3fb3a2e1bcbb41c52deaf0358df8eccce))
* **contrib:** the converter writes the desugared mappings itself ([226fe48](https://github.com/PolusAI/sophios/commit/226fe482254a2b3ab89e3f491cb348b1c79d4191))
* delete production code only tests reach, inline one-caller wrappers ([e8c68a0](https://github.com/PolusAI/sophios/commit/e8c68a0bdf88289c8e795d4610218c7c3cea8b76))
* delete the graph and namespace helpers nothing calls ([4edd616](https://github.com/PolusAI/sophios/commit/4edd616cf721d0e147a37c1ec01d0ba7c247d3da))
* delete wic_loader ([6bc1467](https://github.com/PolusAI/sophios/commit/6bc14671e26e5dad772a9af142df89507131fde7))
* draw a compiled workflow after compiling, not inside it ([e4c6f9b](https://github.com/PolusAI/sophios/commit/e4c6f9b30fc0808a8028776df14f13a56971ed8b))
* **drawing:** one reader for a sidecar's graphviz entry ([5ee9287](https://github.com/PolusAI/sophios/commit/5ee92873aaa16a9bcabb1962026502e4b27a1188))
* fold wic021 into wic006, one code for a step with no id: ([948364c](https://github.com/PolusAI/sophios/commit/948364c981da679922fcd6e97005ad5a245bbe70))
* generate the JSON Schema from the AST, not beside it ([11d0faf](https://github.com/PolusAI/sophios/commit/11d0faf984368ec553fe720af743c7d7e5cefa21))
* hand-authored CWL specs keep identity equality ([469d99d](https://github.com/PolusAI/sophios/commit/469d99ddd66eaf10a78e077c27ee110a272b1738))
* identities stay structural until Emit spells them ([c65c714](https://github.com/PolusAI/sophios/commit/c65c7140739e7bbcacbae92e69e2ff1e4202428e))
* Infer names the step it is iterating; EdgeOrigin names its reader ([72bbdc9](https://github.com/PolusAI/sophios/commit/72bbdc984794fb4487e59315db073b074f33f387))
* Infer's lifted input keeps the tool's format as declared ([e4c5e62](https://github.com/PolusAI/sophios/commit/e4c5e620c8141ed1667771b358a49fd2602d839a))
* inline the Python API's one-expression wrappers ([46b429e](https://github.com/PolusAI/sophios/commit/46b429ee7a08a8de1835277080348423aed5b510))
* **ir:** give the record handling its own helpers ([be97814](https://github.com/PolusAI/sophios/commit/be97814bd5936dbd72c0d4725a9d8dce0e24ee24))
* **ir:** record derived port origins instead of re-splitting names ([2746d72](https://github.com/PolusAI/sophios/commit/2746d725067f1a6856e010e24100ae556477046e))
* **lang:** the parser asks the loader instead of mirroring it ([f622bc5](https://github.com/PolusAI/sophios/commit/f622bc59eca293d2bdce2da086fce84da466347d))
* link no longer imports PortName ([2b53b24](https://github.com/PolusAI/sophios/commit/2b53b24c637962eb8b5801f399b613a54e0938bf))
* **lower:** one reading of the step an outputSource names ([a692d2a](https://github.com/PolusAI/sophios/commit/a692d2a33d2c46ce8b5f6d6318aef2bc2ea8cc3c))
* match a record's fields as a Mapping, without importing Fields ([89befa1](https://github.com/PolusAI/sophios/commit/89befa1e8ddb09650cdee72de2dadda401a0ea70))
* modernize types and supporting code ([3af39f6](https://github.com/PolusAI/sophios/commit/3af39f6af722cc3b383be47dec7730d011dae1f0))
* modularize core workflow processing ([6570369](https://github.com/PolusAI/sophios/commit/657036902c84175b55603e8a5659b13c38455324))
* name the run-args merge privately ([6f2ad36](https://github.com/PolusAI/sophios/commit/6f2ad36b88a4f7657b997d89e216351252650e00))
* name the runtime checks inlining made hard to read ([68e8080](https://github.com/PolusAI/sophios/commit/68e8080a559ef1cd25e222854e4a55cbdcc261bc))
* namespace the language layer's shared state ([2f64d8a](https://github.com/PolusAI/sophios/commit/2f64d8aacc00e43593ff8d2f54e0367a4b6ac6b6))
* **nextflow:** an output record computes its own array depth ([dfc3943](https://github.com/PolusAI/sophios/commit/dfc3943d01fd932d11c2be99752ed669ead3993e))
* **nextflow:** build a workflow input's sink channel and a broadcast in place ([b1c6cb9](https://github.com/PolusAI/sophios/commit/b1c6cb9f044e34ac8bb842567b4db962667f4883))
* **nextflow:** command and glob segments render one reference value ([25b4e10](https://github.com/PolusAI/sophios/commit/25b4e103b5ca7441a55416063b299d7a185165e3))
* **nextflow:** delete the structural reader, promotion and CWL reconstruction ([ca07b21](https://github.com/PolusAI/sophios/commit/ca07b21581bc4af2c1d3ba7de0f87bb4e4b3184b))
* **nextflow:** drop an avoidable local import and a shadowed name ([ee8af93](https://github.com/PolusAI/sophios/commit/ee8af93c2a01e0cbefa9c6c0d22d5820b773ec2f))
* **nextflow:** drop the always-true clause from the scattered-edge check ([2f63931](https://github.com/PolusAI/sophios/commit/2f63931bf9f14d3083b75475c1332b77ec8a7240))
* **nextflow:** drop the unused MULTI_INPUT_ADAPTERS import ([50271be](https://github.com/PolusAI/sophios/commit/50271be45ae1cdf50bc0b833777ccad8c8ac2239))
* **nextflow:** establish executable IR boundary ([baf2569](https://github.com/PolusAI/sophios/commit/baf25696816efaf861cd037f522bed9a2db631c6))
* **nextflow:** hydrate the current schema version only ([1a0473b](https://github.com/PolusAI/sophios/commit/1a0473b38afa5aae48a9706e2e4ad6d2ecaf4535))
* **nextflow:** key a staged step output by its step id alone ([fb3b124](https://github.com/PolusAI/sophios/commit/fb3b1249667e333c367df592736d1adcae07c6dd))
* **nextflow:** lower from the core semantic graph ([93c68f4](https://github.com/PolusAI/sophios/commit/93c68f4e90654db9300c67f4c7e71cbd231d9665))
* **nextflow:** move public exposure to follow-up ([7e7f44f](https://github.com/PolusAI/sophios/commit/7e7f44facf39216af1741ba11033b6cee204bcae))
* **nextflow:** name the multi-input scatter helpers for both methods ([f1db967](https://github.com/PolusAI/sophios/commit/f1db9672b41fa1bd83c3f7a092c9d305be14c71d))
* **nextflow:** one field check, one gate table and one rule table in the model ([c91069b](https://github.com/PolusAI/sophios/commit/c91069b4a0b52eace851635843939d4626f2c6d3))
* **nextflow:** one idiom table for the forms with holes, filled by the renderer ([2fb4d8f](https://github.com/PolusAI/sophios/commit/2fb4d8f67a7d15c23849133c1c49ff2baa8d0e46))
* **nextflow:** one multi-input method map and one conditional branch renderer ([bff0d17](https://github.com/PolusAI/sophios/commit/bff0d1755a4cef8ef3c0a2c285921ecea1e73954))
* **nextflow:** one number-literal and one arity check in the safe subset ([f04223e](https://github.com/PolusAI/sophios/commit/f04223e174288d0c85c808d15f767633bbea0cdd))
* **nextflow:** one place stages a path input's files under a directory, each under its own name ([43cdd76](https://github.com/PolusAI/sophios/commit/43cdd762b14e6a6aa657b242082aace786e1a1ba))
* **nextflow:** one positive-integer check for cpus and accelerator ([e6f589c](https://github.com/PolusAI/sophios/commit/e6f589cee15a03ddf9d77c0e838cb609df7dbbe4))
* **nextflow:** one walk for each repeated capability pass ([16aecb8](https://github.com/PolusAI/sophios/commit/16aecb810869b58a7760a37157915288ba0a2e23))
* **nextflow:** rebase the backend onto core semantics (Spec 3) ([c5464e7](https://github.com/PolusAI/sophios/commit/c5464e73693832ad7e3d8cf09f01cb1534fad50d))
* **nextflow:** share one topological sort and trim dead surface ([8ff608f](https://github.com/PolusAI/sophios/commit/8ff608f3a866d433258463679eae7afb6e8e62af))
* **nextflow:** the step-input check walks wired inputs and looks a producer up by its id ([7fcf6bc](https://github.com/PolusAI/sophios/commit/7fcf6bcd8bd8a2b60a680e18518d3aeea158abff))
* one child-attachment helper, no dead graph fields ([147254c](https://github.com/PolusAI/sophios/commit/147254c41f6392f5f7786eb06592297d54dde848))
* one edge store; an edge says whether it was composed or inferred ([ddc6f82](https://github.com/PolusAI/sophios/commit/ddc6f825a2cbe9ab502666207c855ee94b46fdfe))
* one home for the wic: vocabulary, one home for the generators ([07121da](https://github.com/PolusAI/sophios/commit/07121da195c062d9af6dcff6bca7446a6fbe0f0e))
* one place spells a derived name ([88cd526](https://github.com/PolusAI/sophios/commit/88cd5261aad16cf0a6437e0a0a3a17f3f527078d))
* **plugins:** AuthoredNamesFilter spells a text on request ([c006925](https://github.com/PolusAI/sophios/commit/c0069254ecaa876a0906a6083fb01a92b55d5dd2))
* PortType parses its declaration once, into a canonical type ([58ae205](https://github.com/PolusAI/sophios/commit/58ae205b4dab304c69542e0fb9abd4f2e00b9002))
* remove diagnostics error-code shim ([5442ddd](https://github.com/PolusAI/sophios/commit/5442ddd37e49270f83d7a8e29af6f9557a236c83))
* **resolve:** resolve a called workflow in its own function ([b2c1b34](https://github.com/PolusAI/sophios/commit/b2c1b34028aebc9075ed43545a4454b552f62117))
* run every runner as a child process ([0adbb51](https://github.com/PolusAI/sophios/commit/0adbb51a31042489136f04eb52bbbfa583d59008))
* **run_local:** take the documents the runner gets as a parameter ([9e8f905](https://github.com/PolusAI/sophios/commit/9e8f905d850dd4951110518deac10606d6e1f441))
* scatter rank and a port's boundary declaration each have one owner ([3c38adb](https://github.com/PolusAI/sophios/commit/3c38adb64b0887a48b494254da6e390be94155cb))
* separate the core and contrib zones ([ceef080](https://github.com/PolusAI/sophios/commit/ceef0802781cd573ec076bd5bf981dc90581d84e))
* Step and Workflow assign their fields normally ([5a70b0e](https://github.com/PolusAI/sophios/commit/5a70b0ebeac0a691bbeaae1d257cba8544de900f))
* stepin says what it derives, once ([8e2e66e](https://github.com/PolusAI/sophios/commit/8e2e66eac1949b8757bf8ae634e3afab2dc79cb2))
* **tests:** the flattening model leaves a call nested when its boundary is not proved removable ([6e356a9](https://github.com/PolusAI/sophios/commit/6e356a9fa988b78a505f3e2f66cef7d3bc841dc1))
* the AST keeps no field order either ([a791ed4](https://github.com/PolusAI/sophios/commit/a791ed45831b82cbdfe2254cbdd5b33c82ab0caf))
* the AST schema is the only schema Sophios produces ([cd8267b](https://github.com/PolusAI/sophios/commit/cd8267b41d925779f97a08f711115594bbd343b9))
* the compiler has one door, and it takes a parsed root ([dbbc195](https://github.com/PolusAI/sophios/commit/dbbc195a0000160800a7738c276b2973a03c78bc))
* the concrete outputSource spelling is the only one ([40f09a3](https://github.com/PolusAI/sophios/commit/40f09a381ac1875cbaf7ee0f31b06b18211990d2))
* the Python API builds a language Document ([0690967](https://github.com/PolusAI/sophios/commit/069096737e426884dee29cc3b877e56d055c6759))
* the registry holds parsed documents, and Resolve parses nothing ([ea3a965](https://github.com/PolusAI/sophios/commit/ea3a9652454bb074511deefd59f136821ac55b8d))
* the tool builder renders CWL objects one way ([99b7234](https://github.com/PolusAI/sophios/commit/99b7234d0ac45fc277921c44e5c3e3e5bdb94254))
* the tool builder renders through cwl_utils ([c223cc9](https://github.com/PolusAI/sophios/commit/c223cc9879c5d2ab05b48bfe30dcb6dc7d35773c))
* **tool_builder:** remove Output(from_input=) ([86bea23](https://github.com/PolusAI/sophios/commit/86bea238bd4ad964c43194e1c8321a4535bfa98c))
* whether a port is required is decided by declarations.required alone ([49e335b](https://github.com/PolusAI/sophios/commit/49e335bb7a82bb9c30c94f727ae43d9105548583))


### Build System

* a build with no release tag in reach is a post-0.6.0 dev build ([2928d34](https://github.com/PolusAI/sophios/commit/2928d34bc68dd39b99cdc9fe993e252afad2fc04))
* drop the runners-src extra, a direct Git dependency PyPI rejects ([1ce5ae8](https://github.com/PolusAI/sophios/commit/1ce5ae8d1ddc152dd802cf3a45f0fd67cc620ff9))
* hold cwl-utils below 0.43 ([15e835a](https://github.com/PolusAI/sophios/commit/15e835a828eed58be491f8f3d7b45d55158bf2f9))
* leave cwl-utils unbounded ([2e016f3](https://github.com/PolusAI/sophios/commit/2e016f35be0afbbff79b8c76fea71a9ab74a7594))
* networkx is a test dependency only ([e7ef4ee](https://github.com/PolusAI/sophios/commit/e7ef4eed1f6568804e9a8630a90bc7788fece5a8))
* require cwltool&gt;=3.1.20241217163858 for singularity image names ([c3e39a0](https://github.com/PolusAI/sophios/commit/c3e39a01b8bc454a5239cd0dd6288030de662f3d))
* the core/contrib boundary is an import-linter contract ([ce05b18](https://github.com/PolusAI/sophios/commit/ce05b1839067bdcccb116aefbd56032cba5c6b31))

## 0.7.0: upgrading from 0.6.0

Codes in parentheses are the codes Sophios prints with each diagnostic.

### `.wic` files

- A literal must already have its input's type; nothing is converted any more
  (wic020). Write `!ii 2` rather than `!ii 2.9` on an `int` input, `!ii true`
  rather than `!ii yes` on a `boolean` input, and `!ii 1.0e-5` rather than
  `!ii 1e-5` (YAML 1.1 reads the latter as text). A `string` input still takes
  any scalar, and an `int` still feeds a `float`.
- A quoted `!ii` scalar is its text, as it is in plain YAML: `!ii '007'` is the
  text `007`, where it used to be the number 7. Quote a literal to keep it
  text; leave it unquoted for a number, so `!ii 7` on an `int` input. A
  `string` input takes either spelling.
- A boolean on a `string` input reaches the tool as `true` or `false`, where it
  was `True` or `False`. A tool that compares against `True` must compare
  against `true`.
- A scalar literal on an input listed in `scatter:` is an error (wic020); it
  used to be wrapped into a one-element list, so the scatter ran once. Write
  a list, or take the input out of `scatter:`.
- A mapping in `in:` made only of CWL step-input fields (`source`, `default`,
  `valueFrom`, `linkMerge`, ...) is reported (wic038); it used to be sent to
  the tool as a literal object. Tag it `!ii` if the tool should receive that
  mapping, or write it as a `!cwl` step-input record if it binds the input.
- An output with `linkMerge`, `pickValue` or a list `outputSource` is reported
  (wic038). Point `outputSource` at one `step/port`.
- `!&` belongs on outputs only (wic019). It never compiled on an input; move it
  to the producing step's `out:` and consume it with `!*`.
- A step written as a single-key entry in a sequence (`- touch: {...}`) is
  reported (wic006); it never compiled. Write `- id: touch`, or use the
  mapping form `steps: {touch: {...}}`.
- A document the YAML loader cannot load (`!!int abc`, `!!str [a]`, an
  impossible date such as `2020-13-45`, an unknown `!!` tag such as `!!foo: 1`,
  `k: !!foo 1` or a verbatim `!<tag:...>`, a `<<` that merges a scalar) is
  reported (wic009) at the loader's position, with the loader's own message,
  as an unknown `!` tag is; it used to pass the parser or crash it. Correct the
  value at that position, or quote the text if it was meant literally.
- An `!ii` scalar is read as YAML reads the same text written plain, so
  `!ii 2020-13-45` is reported (wic009) as `2020-13-45` is; it used to become
  the text `2020-13-45`. Quote the text if it was meant literally.
- A Sophios tag used as a mapping key (`!ii a: b`) is reported (wic005): a key
  is a name. Put the tag on the value: `a: !ii b`.
- A step id, a workflow input or output, and the workflow's own file name are
  letters, digits, `_`, `.` and `-`, and do not start with `.`; anything else
  is reported (wic045). They become file names and ids of the compiled CWL,
  where `/`, `#`, a space or `~` broke the run or a name. Rename the step or
  port.
- `<<` merge keys apply everywhere, as YAML defines them: in `!ii` values, in
  `in:` and in plain CWL passed through, which used to carry a literal `<<` key
  or make an input named `<<`. A merged mapping keeps the key order the YAML
  loader gives it, so steps merged into `steps:` run in that order. To keep a
  key named `<<`, quote it: `"<<": value`.
- A step whose run is a CWL Workflow (a top-level `class: Workflow`, or a packed
  `$graph` whose `main` is a Workflow) is refused (wic013), whether it is a
  stem on `search_paths_cwl`, a `run:` path or an inline `run:` body. It used
  to compile with exit 0 and then fail in cwltool, or fail with wic028. Write
  it as a `.wic` subworkflow and call that, or run a CWL Workflow file on its
  own with `--allow_raw_cwl`. In Python, a `Step` whose `clt_path` is a CWL
  Workflow is refused the same way: build it as a nested `Workflow` of `Step`s.
- A packed `.cwl` file (a `$graph`) whose `main` is a CommandLineTool is that
  tool, on `search_paths_cwl`, as a `run:` path or as a `Step`'s `clt_path`:
  the step has `main`'s ports and runs `main` on its own, as a CWL runner
  would. It used to compile as a tool with no ports (wic028, or a failure in
  cwltool). A packed file with no `main` is refused (wic013), naming the
  processes it holds.
- A `cwl_subinterpreter` step declares a real-time analysis, which Sophios
  runs beside the workflow under `--run_local` (see Real-time Analysis in
  `docs/advanced.md`); it is no longer emitted as a CWL step. Every input is a
  literal, and a malformed declaration is reported (wic044). The adapter's
  `cachedir_path`, `root_workflow_yml_path` and `homedir` inputs and its
  `output_log_path` output are gone: remove a binding of those inputs and any
  edge from `output_log_path`. `max_times` is an `int`; a string integer such
  as `'20'` is still accepted. From Python, a `.wic` analysis is found through
  `Workflow.run(workflow_paths=...)`, shaped like `Workflow.from_wic`'s.

- The REST converter keeps each WFB setting as sent: a string setting is text
  and is no longer read as YAML a second time. That second read crashed on
  `*.tif`, `@x` or `%p`, turned `{row:c}` into a mapping, cut `x # y` to `x`,
  folded line breaks, dropped surrounding quotes and spaces, and wrote a null,
  alone or inside a list, as the text `None`. A typed value sent as text now
  stays text: send `5`, `0.5`, `true`, `null` or `[1, 2]` as that JSON value,
  not as the string `"5"`, `"0.5"`, `"true"`, `"null"` or `"[1, 2]"`, which a
  typed input reports (wic020). `"2020-01-02"`, `"yes"`, `"0x1F"` and `"12:30"`
  stay text as well.

### Compiled CWL

- A step is called by its id: `touch`, where it was
  `greeting__step__1__touch`, and a call to `make_greeting.wic` is the step
  `make_greeting`. An id more than one step of a workflow has is numbered on
  every step that has it, the first included, with `@` and the step's
  position: `append@2`, `append@3`; so is a step whose id is also the name of
  one of the workflow's inputs or outputs. A name the compiler makes joins
  step and port with `~`: the input `touch~filename` where it was
  `greeting__step__1__touch___filename`, and a nested one
  `make_greeting~touch~filename`. Rename the keys of an inputs file written
  for an earlier compile, and the keys you read from `output_<workflow>.json`;
  `--cwl_inline_subworkflows` names a dissolved call's steps `<call>~<step>`.
  In Markdown on GitHub, quote such a name in a code span: two `~` render as
  strikethrough.
- A `when:` on a subworkflow call that reads one of the call's made names
  writes it in brackets, `$(inputs["touch~filename"])`: `~` is not allowed in
  `inputs.<name>`.
- An `outputSource:` that names its step as `<workflow>__step__<n>__<id>` names
  no step any more: with no `type:` it is reported (wic036), and with one the
  runner rejects it. Write the step's id, or `(n, id)` when the id repeats. A
  step whose id you wrote that way is still that step.
- Every file of the tree sits beside the workflow in the base path:
  `<workflow>.cwl`, `<workflow>_inputs.yml`, and `<workflow>~<step>.cwl` for
  the process each step runs, where it was `<generated step id>/<tool>.cwl`;
  a step inside a subworkflow is `<workflow>~<call>~<step>.cwl`. A tool three
  steps run is written three times, and two workflows compiled into one base
  path never share a file. The empty `<tool>_inputs.yml` beside each tool is
  no longer written. A real-time analysis is laid out the same way under
  `realtime/<workflow>/`: `<analysis>.cwl` and `<analysis>~<step>.cwl`. A
  script that read a tool file by its old path reads it by its step's.
- `autogenerated/<workflow>.names.json` is no longer written, and a run no
  longer rewrites the runner's messages: they name each step as it is called
  in the compiled CWL.
- A tool that refers to a file relative to itself (an `$include`, an
  `$import`, a relative `location` or `path` such as a `default:`) is packed
  by cwltool when it is written: the include becomes its text, the import its
  content, a location an absolute `file://` URI. The files stay where they
  are, and a copied tool no longer loses them. A file cwltool cannot read is
  reported at the step that runs the tool (wic013), where the run used to
  fail. A `dockerFile` given by `$include` is packed the same way; its path
  is no longer made absolute.
- The compiled workflow no longer lists the EDAM ontology under `$schemas`;
  `$schemas` holds what you wrote.
- The compiled workflow no longer carries Sophios's own `sophios:lang_version`
  key and `sophios` prefix, which no runner reads; the compile prints the
  version, and Python has `CompiledWorkflow.lang_version`. `edam` is declared
  in `$namespaces` only where a format says `edam:`.

### Command line

- The runner's default output directory is named after the workflow too,
  `outdir_<runner>_<workflow>_<timestamp>`, like the provenance, summary and
  job-store paths beside it. Two different workflows started in the same
  second from one directory no longer share it, where cwltool could fail with
  `File exists` or merge their outputs. Two runs of the same workflow started
  in the same second from one directory still share every path the run writes.
- Diagnostics are printed on stderr, each with its `file:line:col` and code, and
  so are the compile-failure banners. A script that read them from stdout must
  read stderr.
- A working directory where `autogenerated/` cannot be created or written is
  reported as `wic021`, naming the directory, before anything is compiled; it
  used to be a `PermissionError` traceback.
- `--basepath DIR` names the directory the command writes its compiled workflow,
  job file, drawings, schema and run files into, as `Workflow.run(basepath=)`
  does; it was always `autogenerated/`. Nothing under it is read as a tool or
  workflow: pass the same directory to `plugins.get_tools_cwl(config, basepath=)`
  and `get_yml_paths(config, basepath=)` from Python.
- A `--yaml` or `--inputs_file` that names no file is a usage error: one line
  and exit code 2, as a missing `--config_file` already was. It used to be a
  `FileNotFoundError` traceback.
- An unexpected failure is one line: what stopped, and the
  `error_<workflow>.txt` that now holds the whole traceback (it used to be
  written without its stack frames). If the line does not say what to change,
  report it with that file.
- An unrecognised argument exits with code 2. Pass `--passthrough_flags yes` to
  hand unknown flags to the runner; `--passthrough_flags` accepts only `yes` or
  `no`.
- A flag is never read as an abbreviation of a longer one: write each flag in
  full (`--inputs_file`, not `--inputs`). A runner flag such as `--validate`
  now reaches the runner instead of being taken for `--validate_plugins`.
- `--run_local` exits with the runner's exit code when the run fails; it used
  to exit 0. A script that checked the output instead of the exit code can
  check the exit code.
- `--quiet` is passed to cwltool only when you give it. It no longer changes
  the compiled CWL: a tool on a search path used to get `stdout: <tool>.out`
  and `stderr: <tool>.err` at compile, written to files the run deleted. A
  tool's own messages now print as the run goes.
- `--no_provenance` drops the runner's `--provenance`; it used to be accepted
  and ignored.
- `--cachedir` is passed to cwltool when you give it, so cwltool caches each
  step there and reuses a step whose tool and inputs are unchanged. It used to
  be accepted and ignored, and its default, `cachedir`, is now unset: a run
  caches only when you ask, or in `cachedir/` when it runs a real-time
  analysis. Give `--cachedir cachedir` to keep the old directory.
- A relative `location` or `path` of a File or Directory in a `.wic`
  `default:`, under `inputs:` or in a step-input record, is read from the
  directory of the `.wic` and written absolute into the compiled CWL; it used
  to be written as it was, and read from wherever the runner read the
  compiled workflow. A default that relied on that reads from beside the
  `.wic`, or is written absolute.
- A relative `location` or `path` of a File or Directory in `--inputs_file`
  is read from the directory of the inputs file, at any depth, as CWL v1.2
  section 5.1.5 says. Only a top-level `location` was rewritten before, and it
  was read from the working directory, so a job file that sat elsewhere
  worked. If you keep an inputs file outside the directory you run from and
  wrote its paths relative to that directory, make them relative to the file,
  or absolute.
- Before a run, Sophios checks that each File and Directory a job names
  exists, is the right kind and is readable (wic016). An inputs file that is
  not a mapping of input names to values is reported (wic002). It also checks
  that it can create and write the directories the run writes to:
  `autogenerated/` or `Workflow.run()`'s `basepath`, and `--outdir` or
  `run_args_dict={"outdir": ...}` (wic021).
- A `!ii` File or Directory read from beside the workflow is run where it is:
  the job names it by its absolute path, and nothing is copied. A File used to
  be copied into `autogenerated/`, however large, and a Directory was not, so a
  tool that read it failed.
- The container engine is checked only when a step runs in a container, with
  `<engine> info` rather than a hello-world container. A workflow with no
  container runs without an engine.
- Before a local run, a podman run pulls its images with podman; it used to pull
  them with docker. A tool whose image comes from `dockerLoad` or `dockerImport`
  now has it loaded before the run; the run never loaded it before. An image
  that cannot be pulled, loaded or imported is reported (wic037) instead of
  ending in a traceback.
- `--write_intermediate_wic` is gone.
- `--ignore_validation_errors` is still accepted but does nothing beyond a
  warning: there is no separate validation pass left to ignore. Remove it.
- `--generate_schemas` writes only the language schema,
  `autogenerated/schemas/wic.json`. Per-tool and per-workflow schemas, and
  `schema_store.json`, are no longer written: point an editor at `wic.json`.
- `--cwl_inline_subworkflows` flattens subworkflow calls into the root again,
  and `--cwl_inline_runtag` embeds each tool; pass both for one self-contained
  file. A call that carries anything besides `in` and `out` (such as
  `scatter:` or `when:`), or whose workflow says `wic: {inlineable: false}`,
  stays nested and is named on stderr.
- The `cwl_subinterpreter` command is gone; `--run_local` runs a declared
  real-time analysis itself.
- `--graphviz` draws each step as a box with no fill, its outline and label
  in black, and an inferred edge in black; an explicit edge is still blue.
  The boxes used to be filled light blue and the inferred edges white, which
  vanished on a white page. `--graph_dark_theme` now draws the text, the box
  outlines and the inferred edges in white, for a dark background; it used to
  turn the text and the inferred edges black. The labels `--graph_label_edges`
  writes take the same colour as the text. Drop the flag where you passed it
  to get black, and pass it where you relied on white. With the flag an
  explicit edge is a lighter blue (`lightskyblue`), which reads on a dark
  background where `blue` does not.
- `--docker_remove_entrypoints` (and `docker_remove_entrypoints` in
  `Workflow.run()`) runs the `-noentrypoint` images it builds. The run used to
  read the CWL written before the images were renamed, so it ran the original
  images; and a `DockerRequirement` under `hints`, or in a tool embedded in an
  inlined document, was never renamed. An image from a registry with a port
  (`localhost:5000/img:tag`) no longer fails the run.
- cwltool and Toil run as a separate program, a child of Sophios, from the
  command line and `Workflow.run()` alike, never inside the Sophios process.
  Ctrl-C sends the runner SIGTERM, on which cwltool stops the containers it
  started, and kills it if it has not stopped within 30 seconds; however else
  Sophios ends (`kill`, a closed terminal, `kill -9`), the runner is stopped the
  same way. `error_<workflow>.txt` is written only when the runner could not be
  started, and the `via cwltool.main.main python API` line is gone.
- `sophios.run_local.run_local`, `build_cmd`, `create_safe_env` and
  `RUNNER_UNAVAILABLE`, and `sophios.plugins.FailedJobs`, are removed: run with
  the command line or `Workflow.run()`.
- A local run, from the command line or `Workflow.run()`, keeps everything the
  runner printed, both streams as they came, in `<basepath>/run_<workflow>.log`.
- A run under `--cwl_runner toil-cwl-runner` writes the output object Toil
  prints to `<basepath>/output_<workflow>.json`, as a cwltool run does.

### Python API and embedding

- `Workflow.run()` raises `WorkflowRunError` (api005, importable from
  `sophios.api.python.workflow`) when the run fails, instead of returning.
  Catch it where you used to inspect the result.
- `Workflow.run()` returns a `RunResult` (`state`, `outputs`, `outdir`,
  `log_file`, `exit_code`) for a completed run instead of `None`, and `None`
  with `generate_run_script`. A script that ignores the return value runs as
  before. `Workflow.start()` takes `run()`'s arguments and returns a
  `RunHandle` once the runner has started: `wait(timeout=None)`,
  `collect(timeout=None)`, `status()`, `logs()`, `outputs()` and `cancel()`,
  with the states `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED` and
  `CANCELLED`. `wait()` returns the run's `RunStatus` however it ended;
  `collect()` returns its `RunResult` or raises `WorkflowRunError`, and
  `run()` is `start().collect()`; `cancel()` returns whether the run is
  cancelled. The run lives as long as the Python process: when the
  interpreter exits with the run still going, it is cancelled.
- `sophios.run_local_async` is gone: its `run_cwl_workflow` and
  `run_cwl_serialized` ran a runner without Sophios's checks, image pulls or
  failure report. Use `await flow.run_async(...)`, a coroutine with `run()`'s
  arguments and result, which runs the compile and the checks in a worker
  thread, and stops the runner when its task is cancelled. It refuses a
  workflow that declares real-time analyses with `api008`; use `run()` or
  `start()` for one. `sophios.runtime_inputs.normalize_job_inputs` and
  `normalize_cwl_document`, which only it used, are gone with it.
- `Workflow.run()` runs exactly the document `Workflow.compile()` returns,
  written as one `<workflow>.cwl` with its job file; it no longer writes a
  file for each tool beside it. A workflow that names outputs keeps only
  those, in the run's outputs as in `compile()`: read a step's output by
  naming it as a workflow output.
- `run_args_dict={'no_provenance': 'yes'}` runs without the runner's
  record of the run.
- A `Step(step_name=...)` or `Workflow` name with a character other than
  letters, digits, `_`, `.` and `-`, or starting with `.`, raises
  `ValueError`. A relative File or Directory in a `StepInput(default=...)` is
  read from the working directory and written absolute, as a bound value is.
- `Workflow.compile()` and `Workflow.run()` take `inference=Inference(...)`
  (`sophios.api.python.workflow`), the Python spelling of
  `--inference_disable`, `--inference_strict`,
  `--inference_use_naming_conventions`, `--insert_steps_automatically` and the
  configuration file's `renaming_conventions` and `inference_rules`. An
  invalid one is `api007`. Without it, inference runs with the defaults, as before.
- Library code raises `SophiosError` (`sophios.lang.diagnostics`) instead of
  calling `sys.exit(1)`, so a bad workflow no longer ends the calling process.
  Catch `SophiosError` where you caught `SystemExit`. The CLI's exit codes are
  unchanged.
- A numpy `int`, `float32` or `bool` passed as a literal is no longer
  converted; pass `int(...)`, `float(...)` or `bool(...)`.
- Contrib modules moved under `sophios.contrib`:
  `sophios.api.utils.converter` is `sophios.contrib.converter`,
  `sophios.api.utils.wfb_util` is `sophios.contrib.wfb_util`,
  `sophios.api.utils.ict.*` is `sophios.contrib.ict.*`, and
  `sophios.api.rest.api` is `sophios.contrib.rest.api`. The REST API's HTTP
  behaviour is unchanged.
- The modules `sophios.ast`, `sophios.inference`, `sophios.inlineing` and
  `sophios.schemas` are gone with the compiler they belonged to. Compile
  through the CLI or `sophios.api.python`.
- Ports are bound and read through their objects. `Workflow.add_input()` is
  gone; declare an input by binding it: `workflow.inputs.name = ...` or
  `step.inputs.x = workflow.inputs.name`.
- `Step.bind_input()`, `Step.get_output()`, `Workflow.bind_input()`,
  `Workflow.bind_output()` and `Workflow.add_output()` are private. Use
  `step.inputs.x = ...`, `step.outputs.x` and `workflow.outputs.x = ...`.
- `Output(from_input=...)` is gone; write the glob as a CWL expression, e.g.
  `glob="$(inputs.output.basename)"` for a File or Directory input,
  `glob="$(inputs.output)"` for a string one.
- `Fields.to_list()` is gone and `SecondaryFile.to_dict()` returns a mapping:
  the tool builder renders through cwl_utils.
- `Workflow.write_wic()` writes a self-contained bundle (the root `.wic`, each
  nested workflow's `.wic` and each tool's `.cwl`) and no longer takes
  `inline_subworkflows`. The inline `subtree:` form is gone. Drop the
  argument and read the nested workflows from their own files.
- `Workflow.to_wic_yaml()` and the `Workflow.yaml` property are gone:
  `Workflow.write_wic()` is the one way to write `.wic` from Python. Call it
  and read the file it writes.
- A written workflow output names its authored step (`step/port`), and every
  step carries `run: <stem>.cwl`. Two different tools sharing one file stem in
  a workflow are rejected; rename one tool's file. So is a different
  `<stem>.cwl` already in the target directory, which `write_wic` used to
  overwrite silently; a file holding the same tool is left untouched. A tool's
  relative imports such as `$import` are not copied.
- Several sources, `linkMerge`, `pickValue`, `valueFrom`, `default`,
  `loadContents` and `loadListing` are bound with `StepInput(source=,
  link_merge=, pick_value=, value_from=, default=, load_contents=,
  load_listing=)`, whose sources are port objects. A list of ports, or a port
  anywhere inside a bound list or mapping, raises `InvalidInputValueError`
  where it is bound; it used to reach the compiler as a literal and fail with
  wic020. Wrap the ports in `StepInput(source=[...])`.
- `Step(path, config_path=...)` is `Step(path, step_inputs_file=...)`. Its
  values are bound as written: a File or Directory in it is no longer checked
  when the step is built (api001), and a local run reports a missing one
  before it starts (wic016), reading a relative path from the working
  directory. `Step.from_cwl_document(config=...)` and
  `CommandLineTool.to_step(config=...)` are `step_inputs=...`, bound the same
  way.
- `Workflow.run(user_env_vars=...)` passes each value to the runner exactly as
  given; `$`, `!`, `&`, quotes, parentheses and newlines used to be stripped
  from it. Write a value as the runner should see it. A key that is not an
  environment variable name raises `ValueError` before the run starts, where
  it used to be dropped with a warning; rename or remove it.
- `compile_source` takes the bundle and a `sophios.compiler.CompileSettings`
  only: drop `yaml_tag_paths`, the graph settings and `graph_target`.
  `CompileSettings()` holds the command line's defaults, and
  `sophios.cli.settings_from_args(args)` returns `(settings, graph_settings)`.
  The compiler no longer draws: `CompilationArtifact.graph_view` and
  `sophios.utils_graphs` are gone, and `sophios.drawing.draw(result, bundle,
  graph_settings, name, *, background)` draws a compiled workflow. The
  `sophios.cwl_subinterpreter` module is gone.
- `sophios.utils_yaml.wic_loader()` and its `WicLoader` are gone. Read a
  `.wic` with `sophios.lang.parse` or `Workflow.from_wic`.
- `CommandLineTool.validate()`, `write_cwl(validate=True)` and
  `validate_cwl_document()` never load the ontologies under `$schemas`, as a
  local run does not, and no longer take `skip_schemas`. Drop the argument.
- `Workflow.draw()` returns the workflow's drawing as a `graphviz.Digraph`,
  which a Jupyter notebook shows inline when a cell ends with it. It compiles
  as `compile()` does, so `inference=` changes the edges it draws. Its
  background is opaque, white or with `dark=True` near-black, so it reads in
  a light or a dark notebook, in exported HTML and on GitHub; `--graphviz`
  keeps its transparent background.

<!-- Breaking changes merged before 0.7.0 is released add their upgrade steps
     to the section above that they belong to. -->

### Packaging

- `aiofiles` is no longer a dependency; `sophios.run_local_async` was its only user.
- The `runners-src` extra is gone; PyPI does not accept a dependency on a Git
  URL. Install `cwl-utils` from Git yourself if you need an unreleased
  version.
- cwltool 3.1.20241217163858 or newer is required. From that release cwltool
  names a singularity image with no tag as `cwl-docker-extract` pulls it
  (`<name>_latest`); with an older cwltool, a singularity run of such an
  image cannot find it. `pip install -U cwltool` if pip reports a conflict.
- Before a release is uploaded to PyPI, its built wheel and sdist are each
  installed into a fresh environment and used from outside the checkout: the
  CLI, the Python API, and a tutorial compile with the adapters the package
  ships. The same check runs on every pull request.

### New diagnostics that do not fail a compile

These print on stderr and change no output; nothing needs to change.

- When inference picks between equally good producers it says so: wic042 when
  one producer offered several matching outputs, wic043 when an earlier
  producer also matched. `--inference_strict` makes both errors; pin the edge
  with `!&`/`!*` to choose explicitly.
- A `when:` that reads an undeclared input (with the bracket form to write
  when it reads `inputs.<call>` for a made name), a `wic: steps:` key or a
  `wic: graphviz: ranksame` entry that addresses no step, and a call left nested by `--cwl_inline_subworkflows`
  are each named on stderr.
