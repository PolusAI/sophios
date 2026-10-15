# Subworkflows e.g. "macros"

In the previous tutorial, we listed all of the workflow steps in a single file. Alternatively, we can extract some of the steps into another workflow.

<table>
<tr>
<td>
docs/tutorials/multistep3.wic

```yaml
steps:
- id: touch
  in:
    filename: !ii empty.txt
- id: append_twice.wic
- id: cat
```

docs/tutorials/append_twice.wic

```yaml
steps:
- id: append
  in:
    str: !ii Hello
- id: append
  in:
    str: !ii World!
```

</td>
<td>
docs/tutorials/multistep3.wic.gv.png

![Multistep](multistep3.wic.gv.png)

</td>
</tr>
</table>

We have moved the two append steps into `append_twice.wic`. The step `- id: append_twice.wic` runs that file as a subworkflow: a step named after a `.wic` file runs it (see [What a step runs](../language_guide.md#23-what-a-step-runs)). As you can see from the arrows in the graphical representation, the exact same edges have been inferred! The inference algorithm is guaranteed to work identically across subworkflow boundaries! You are completely free to abstract away minor details behind a subworkflow, and the main workflow graph will be identical.