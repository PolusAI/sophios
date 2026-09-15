# Changelog

## [0.6.0](https://github.com/PolusAI/sophios/compare/sophios-v0.5.0...sophios-v0.6.0) (2026-09-15)


### ⚠ BREAKING CHANGES

* fold wic021 into wic006, one code for a step with no id:
* a sequence step carries its name in an id: key
* an edge is defined at its source, so !& belongs on outputs only
* library code raises SophiosError instead of calling sys.exit(1). CLI exit codes are unchanged.
* contrib Python import paths move.   sophios.api.utils.converter   -> sophios.contrib.converter   sophios.api.utils.wfb_util    -> sophios.contrib.wfb_util   sophios.api.utils.ict.*       -> sophios.contrib.ict.*   sophios.api.rest.api          -> sophios.contrib.rest.api REST HTTP behaviour is unchanged; HTTP clients are unaffected. The design document approves this exception explicitly (§3, exception 2).

### Features

* add a typed AST and total parser for the .wic syntax layer ([e5cd173](https://github.com/PolusAI/sophios/commit/e5cd173b4c32cba5c34f7d7619f1b18e3e0790be))
* added Python API ([#102](https://github.com/PolusAI/sophios/issues/102)) ([38fbd19](https://github.com/PolusAI/sophios/commit/38fbd19bc1a3f51181a7b5f2a22b204c209abd6d))
* close the opaque type and preserve literal spellings ([7b3eb06](https://github.com/PolusAI/sophios/commit/7b3eb06d510b5a4d0fbafe64e5d77722bc530ff4))
* code to build pdf copy of the docs ([0f01875](https://github.com/PolusAI/sophios/commit/0f0187506ac3c07e08997f6bc60b48e7821a5ddb))
* correctly resolve edam format inputs ([df66d0a](https://github.com/PolusAI/sophios/commit/df66d0aba96e478519efe9973e706653517ee425))
* export a JSON Schema derived from the language definition ([6d07b99](https://github.com/PolusAI/sophios/commit/6d07b995c6ad582d56e33058f7c2baa8ba448ccd))
* fix wordings on install docs and refactor clt builder api names ([8d15a9f](https://github.com/PolusAI/sophios/commit/8d15a9fe82a50e8d43b3829fcab9c572398b21be))
* further simplify API surface and improve names ([4f82bfe](https://github.com/PolusAI/sophios/commit/4f82bfef13547637e19079530aa0b5640e1491d0))
* give the library a failure type an embedder can catch ([38840d6](https://github.com/PolusAI/sophios/commit/38840d6819197e94f2d391a6b7f01b1d1e8aebfb))
* **pythonapi:** add Pydantic V2 support ([#152](https://github.com/PolusAI/sophios/issues/152)) ([16be637](https://github.com/PolusAI/sophios/commit/16be6376b1814fe6a289880a8ae80f6829badbd5))
* reconcile dev and user RTD docs ([0fb3f4f](https://github.com/PolusAI/sophios/commit/0fb3f4f9b1612159929000b4a2cd7cde804ad0de))
* regularize and simplify API surface ([1268551](https://github.com/PolusAI/sophios/commit/12685513fb347952cf5c6bc1a61b9030df5de343))
* resolve the language version by rule, not by accident ([ee568c4](https://github.com/PolusAI/sophios/commit/ee568c46425729c25bbd1e9425598574f25c4db4))
* revamp dev docs and add edge inference to python workflows API ([550849e](https://github.com/PolusAI/sophios/commit/550849effcc3d32890b65165f44a72f73184ca0d))
* revamp dev docs and add edge inference to python workflows API ([9c1b7c1](https://github.com/PolusAI/sophios/commit/9c1b7c16445a615cde7ed7e589772cb20175a597))
* surface the resolved language version everywhere it matters ([71a6eea](https://github.com/PolusAI/sophios/commit/71a6eea081fb309a097d1de70e6b87034d8d54e3))
* write the .wic AST back out in both surface spellings ([7b3528e](https://github.com/PolusAI/sophios/commit/7b3528ed1875bdcd7544fd34af1717d04870e849))


### Bug Fixes

* a falsy default is not the absence of a default ([7c8648a](https://github.com/PolusAI/sophios/commit/7c8648a6c8e72c53e0c733901b2b80352702857f))
* a null default satisfies nothing, and the predicate gates inference too ([847914a](https://github.com/PolusAI/sophios/commit/847914a1caf040581306af906b80d87269ea780f))
* a sequence step carries its name in an id: key ([d35f998](https://github.com/PolusAI/sophios/commit/d35f998aa76a6886d0010f8201edab3c1968e6d1))
* a sequence step with no id: still reports the contents of its body ([ba69b2b](https://github.com/PolusAI/sophios/commit/ba69b2ba6aa58f3293d9189b59f398ca2e7fda98))
* a sequence step's identity and body are read the way the mapping form reads them ([40e95c7](https://github.com/PolusAI/sophios/commit/40e95c71e683b83a2c1f14119e84c6ac052ec669))
* a wrong-shaped literal is a TypeError, and it was escaping undiagnosed ([0d99647](https://github.com/PolusAI/sophios/commit/0d99647206487d0f6da5522151b84126204eda6f))
* address the review comments about version management ([03bde5b](https://github.com/PolusAI/sophios/commit/03bde5bcc0f4241db20b7b5d39e8b2f7c8e7514a))
* an edge is defined at its source, so !& belongs on outputs only ([79d5c69](https://github.com/PolusAI/sophios/commit/79d5c6905f8e606e596dd984ae05eb7e37539878))
* API names and client side code ([dac8ef6](https://github.com/PolusAI/sophios/commit/dac8ef6615b86f728cf9ad914d8cdf7de3dcb111))
* binding an input does not edit the documentation the user wrote ([0b2010d](https://github.com/PolusAI/sophios/commit/0b2010d41c30d5564b2b87a83326ead3833d443b))
* check the tag before the position, and keep the name either way ([cea6e31](https://github.com/PolusAI/sophios/commit/cea6e31848d61d257aa4d1194887b33ca3793d82))
* cleanup stale shims and unused _future_ imports ([7575f57](https://github.com/PolusAI/sophios/commit/7575f578075a6605353a02e45e3e54a31a9a95be))
* close the review's open findings on the syntax layer ([4e4be8d](https://github.com/PolusAI/sophios/commit/4e4be8d6600cddda56341432bba23f6da8972dbd))
* collecting tests provisions nothing ([4313214](https://github.com/PolusAI/sophios/commit/4313214a3d90ce26c887e0a9fe3836436655b7a5))
* configuration is a value, parsed once at the boundary ([aa8344e](https://github.com/PolusAI/sophios/commit/aa8344eabae91cdbb94da2bfb54729890b141b9b))
* dead code removal, stale comment deletion and test streamlining ([d6335c1](https://github.com/PolusAI/sophios/commit/d6335c1fef59dc0f3418dce17b1bdf6753552bce))
* dead code removal, stale comment deletion and test streamlining ([9c0208c](https://github.com/PolusAI/sophios/commit/9c0208cf6c13a08022ba864ad7075eab7d1eae2e))
* deduplicate unlabeled Graphviz edges ([9984fb4](https://github.com/PolusAI/sophios/commit/9984fb4c02f47ecf6b92cf1b1c157a2704e486a4))
* diagnose only provably disjoint references ([4ee992d](https://github.com/PolusAI/sophios/commit/4ee992d88fbd1f28053a25fb83cf0c1f86e110d6))
* **docker:** repair the REST image so it can build and run ([2cac4db](https://github.com/PolusAI/sophios/commit/2cac4db37635eda9ee9ac2f64f5daaf73fe250c7))
* emission no longer depends on the hash seed ([0e588fc](https://github.com/PolusAI/sophios/commit/0e588fc23c7214c41a38eebc6e3b318f1805db64))
* enforce the language's invariants at the parse boundary ([750fd86](https://github.com/PolusAI/sophios/commit/750fd86b643dbd5c48bb65ef9264f7dd5b66c1e4))
* fixed mypy errors ([a7e85e2](https://github.com/PolusAI/sophios/commit/a7e85e2c5a58c9dadd8520da7b09ebc2c9326aca))
* fixed mypy issues ([039bd62](https://github.com/PolusAI/sophios/commit/039bd62c96b99612dbdfb3b4d5f7f4c55fe1978b))
* give the CWL substrate one declared version ([1463e85](https://github.com/PolusAI/sophios/commit/1463e851720e4b44178bd0898ce370d6dadef2a9))
* handle empty requirements and loop index shadowing ([e1f9beb](https://github.com/PolusAI/sophios/commit/e1f9bebf616228e21884a116fd95906bb3f8aa82))
* insert steps in the id: form get_steps_keys can read ([11efdd2](https://github.com/PolusAI/sophios/commit/11efdd28c296787205c97826ed6e740a5d1071bc))
* move away versioning from versioneer and simplify release prep ([626919e](https://github.com/PolusAI/sophios/commit/626919e4249c97ea60d303f64399c2395d33349b))
* name the input, type, and literal when a scalar won't coerce ([de52f38](https://github.com/PolusAI/sophios/commit/de52f38f9f352ea1b76586e27d92163569543e9e))
* never specify more than the loader accepts ([b462068](https://github.com/PolusAI/sophios/commit/b4620680c7b6d136415bb208e61ea3ff345d020c))
* normalise a converter's format to a list before flattening ([9431b35](https://github.com/PolusAI/sophios/commit/9431b35fd2504d6e9d7472f8d7ea22d8069b189e))
* normalise every format at its point of entry, not just the whitelist's ([130de95](https://github.com/PolusAI/sophios/commit/130de95af34f5b91afcde5cd874c43f2c73b5223))
* one .wic document, whichever door it comes out of ([47f5874](https://github.com/PolusAI/sophios/commit/47f58749c484e0efd09c9350b514bbf0b5ee41bb))
* **pythonapi:** fix int, float in yaml ([#170](https://github.com/PolusAI/sophios/issues/170)) ([4a0ccb7](https://github.com/PolusAI/sophios/commit/4a0ccb7bd456dfe2c2772f6e003a361d5c6e4995))
* **pythonapi:** fix Workflow object ([#135](https://github.com/PolusAI/sophios/issues/135)) ([cfe1495](https://github.com/PolusAI/sophios/commit/cfe14959b557b3470c212022ef38b9ea01ecca74))
* remove complicated API surface of submit and update client code ([6ac0e6d](https://github.com/PolusAI/sophios/commit/6ac0e6d6ca018c95e3c5c79a2a05d1557088814e))
* remove directory creation before run and fix up run_async surface ([2fc6055](https://github.com/PolusAI/sophios/commit/2fc6055e5aa205837dad5a9e10bfaa335359739c))
* report failures instead of terminating the caller's process ([2e2ef72](https://github.com/PolusAI/sophios/commit/2e2ef7294672acd30ae480b6e17242045f89b657))
* simplify compute request and lint fix ([6a75c36](https://github.com/PolusAI/sophios/commit/6a75c368d58ae3bd1a15927a9eb73e0afa0c8c13))
* the edge-definition rule is positional, and both spellings obey it ([b3b3535](https://github.com/PolusAI/sophios/commit/b3b3535f9a4f146603e6c2908709464baa13db69))
* the last producer of the removed step form, and tags on structural nodes ([26ddb56](https://github.com/PolusAI/sophios/commit/26ddb561199913173f8a0fa3556e90825de4580a))
* the pins walk survives the recursive trees the loader builds ([ccb8063](https://github.com/PolusAI/sophios/commit/ccb806393e08c497553b410521a8e984010bc844))
* the poisoned subprocess needs tests/ on its own PYTHONPATH ([00120bb](https://github.com/PolusAI/sophios/commit/00120bb111de1bd97ea8af53b8a380da3e64fd7d))
* the sidecar case in the cycle contract was not a cycle ([9d752c0](https://github.com/PolusAI/sophios/commit/9d752c0feb8d5d19bec4225c903a3a3921cf54a6))
* the unknown-tag rule belongs to one function, not to each position ([d995c4d](https://github.com/PolusAI/sophios/commit/d995c4ddec1f51dcee48bad9cae575afd87b1bb4))
* the writer transcribes, recurses, and is total over the union ([09a5cc0](https://github.com/PolusAI/sophios/commit/09a5cc0b1389385c1c2784003b9c654dc54df623))
* wic021 names both readings of a single-key step entry ([af84f1e](https://github.com/PolusAI/sophios/commit/af84f1ed8782583ce733941f786f3a8a48d4e6d8))


### Documentation

* add the approved core refactor design ([c071b76](https://github.com/PolusAI/sophios/commit/c071b76c56f78cb2f5c7eca4a4a945bc185ddc74))
* added inline comment for cwl-utils ([28d9591](https://github.com/PolusAI/sophios/commit/28d959106647f4e9ed736f4720e2a01d22eb038b))
* added type ignore to CWLInputParameter ([aaa2710](https://github.com/PolusAI/sophios/commit/aaa27105c0b1f046216b7d97604133641f81c7b1))
* benchmark rationales that the compiler fixes made false ([d79a707](https://github.com/PolusAI/sophios/commit/d79a707002db2f87ee07d11c52980c709e48331b))
* define the .wic language and reconcile both front-ends ([fd4cce5](https://github.com/PolusAI/sophios/commit/fd4cce5a4d17ce6dbf34e490274656e233976688))
* drop the per-PR review scratch from the repository root ([fd75ed3](https://github.com/PolusAI/sophios/commit/fd75ed3e8f9612562b21fe06d97a5fd2ca624b99))
* identifiers that dereference, claims that live once ([fedf8d1](https://github.com/PolusAI/sophios/commit/fedf8d14d7545326cbc75d5358b59046c508db39))
* name the language Sophios in the renderer and schema ([0be6d5d](https://github.com/PolusAI/sophios/commit/0be6d5d670b066a363c7524bf05f786540f16369))
* name the language Sophios, and its version tag lang_version ([b3b68af](https://github.com/PolusAI/sophios/commit/b3b68af51b6dc89aad52b1cb8efb8a9b885e2d3c))
* record the [#403](https://github.com/PolusAI/sophios/issues/403) adversarial review and its mutation table ([b9e4ace](https://github.com/PolusAI/sophios/commit/b9e4acee0a346cf0d96210e38c7b45ca22c061c3))
* the inserted-step note cites a form the reference no longer lists ([924e52d](https://github.com/PolusAI/sophios/commit/924e52ddb9323ea3de39d99656b2b153bbcc45bd))
* the sequence-of-single-key-mappings form worked until 9758e81 ([09ee57a](https://github.com/PolusAI/sophios/commit/09ee57a90cee32d2d0821c08aef3fb61381f6a2d))
* tutorials and the advanced guide use the step form that parses ([129a4c3](https://github.com/PolusAI/sophios/commit/129a4c32eab16f47c6e6275a7d3d49809ecadb71))


### Code Refactoring

* fold wic021 into wic006, one code for a step with no id: ([948364c](https://github.com/PolusAI/sophios/commit/948364c981da679922fcd6e97005ad5a245bbe70))
* generate the JSON Schema from the AST, not beside it ([11d0faf](https://github.com/PolusAI/sophios/commit/11d0faf984368ec553fe720af743c7d7e5cefa21))
* improved typehints, addressed mypy ([54d4c3d](https://github.com/PolusAI/sophios/commit/54d4c3df6ea6d84010222cf6a85d2c315ba756ca))
* modernize types and supporting code ([3af39f6](https://github.com/PolusAI/sophios/commit/3af39f6af722cc3b383be47dec7730d011dae1f0))
* modularize core workflow processing ([6570369](https://github.com/PolusAI/sophios/commit/657036902c84175b55603e8a5659b13c38455324))
* namespace the language layer's shared state ([2f64d8a](https://github.com/PolusAI/sophios/commit/2f64d8aacc00e43593ff8d2f54e0367a4b6ac6b6))
* one home for the wic: vocabulary, one home for the generators ([07121da](https://github.com/PolusAI/sophios/commit/07121da195c062d9af6dcff6bca7446a6fbe0f0e))
* **pythonapi:** remove docker checks ([#148](https://github.com/PolusAI/sophios/issues/148)) ([1724d99](https://github.com/PolusAI/sophios/commit/1724d9911133809379e83b07dcb7403fc2681870))
* refactored Step init ([303ad35](https://github.com/PolusAI/sophios/commit/303ad3590805c40dabe8d5b92b8dad571d058f27))
* separate the core and contrib zones ([ceef080](https://github.com/PolusAI/sophios/commit/ceef0802781cd573ec076bd5bf981dc90581d84e))
* the concrete outputSource spelling is the only one ([40f09a3](https://github.com/PolusAI/sophios/commit/40f09a381ac1875cbaf7ee0f31b06b18211990d2))

## Changelog

All notable changes to Sophios will be documented in this file.
