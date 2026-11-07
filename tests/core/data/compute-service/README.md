Each case is a conversation with the compute service: `do` is `submit` (test_compute.py's `_workflow()`) or `attach`
(run r1), then the run's own methods in turn; the test's docstring says what each other key means. The expectations
are the hand-written assertions they replace, each as strong or stronger, which follow the service's contract and
docs/compute.md; never filled from a run. Quote a time, which YAML reads as a datetime. In `pauses`, the seconds
slept, 5 is `compute._RETRY` and 10 `compute.POLL`; `patience` sets `compute.PATIENCE`.
