A commit within a single repository is an atomic transaction of changes.
To define an atomic "transaction" of commits across repositories,
we can simply create branches in all of the repos with the exact same name.
Then, on push or pull request to any individual repo, we query all of the
repos for a branch with that exact same name, and run all of the individual CIs
using those particular branches. If a branch isn't found in a given repo,
we fallback to the default branch on the upstream repo (i.e. PolusAI & master/main)
(So you should always use unique branch names; do not use 'testing', etc across repos.)

We link the CIs using asynchronous API calls, so they will show up in
your actions histories (plural!) as separate runs. Moreover, actions initiated
by [`repository_dispatch`](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#repository_dispatch) or [`workflow_dispatch`](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#workflow_dispatch) are **NOT** affiliated with any
branch (How could they be? Anyone or anything can call the API...)
so the github website will NOT display the dispatching branch name in blue.
In particular, for PRs,

<span style="color:red"> A GREEN CHECK MARK FOR THE INITIAL DISPATCH WORKFLOW DOES NOT MEAN ANY OF THE OTHER ACTIONS PASSED! </span>

<span style="color:red"> YOU MUST MANUALLY EXAMINE THE ACTIONS HISTORIES OF *ALL* LINKED REPOS! </span>

We have very carefully passed along the commit message, repository, and branch
names so users can easily see which actions correspond to a given
"transaction" / coordinated set of branches across repositories.
Note that since the API calls are asynchronous, there are unavoidable race conditions.
In particular, when merging PRs in different repos as part of a "transaction",
you have about 10 seconds to simultaneously merge all PRs into master.
Otherwise, you may simply need to manually re-run the actions.

The purpose of the `branch_dispatch_*.yml` files is to contain (almost) all of the logic necessary
for linking CIs across repos. Once the appropriate forks and branches are
determined, you can just pass them as inputs into your existing github
actions workflows, and then add a few steps at the end to return the result.

First note that we can only call the default branch (master/main) through [`repository_dispatch`](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#repository_dispatch).
Note that is is not sufficient to checkout master/main and then `git switch`
to another branch. The Actions workflow files themselves may be different
between branches! So after we find the forks and branches, we use
`workflow_dispatch` to directly checkout the correct code and run the CIs on
the correct forks/branches. See [this discussion](https://github.com/orgs/community/discussions/24657#discussioncomment-3244904) and [this example in the GitHub doc](https://docs.github.com/en/rest/actions/workflows?apiVersion=2022-11-28#create-a-workflow-dispatch-event).

Each type of webhook events has its own set of body parameters.
- `repository_dispatch` requires `event_type` (even if it is not used in the receiving workflow file)
and uses `client_payload` to pass user data. See [here](https://docs.github.com/en/webhooks-and-events/webhooks/webhook-events-and-payloads#repository_dispatch).
- `workflow_dispatch` requires `ref` and uses `inputs` to pass user data. See [here](https://docs.github.com/en/webhooks-and-events/webhooks/webhook-events-and-payloads#workflow_dispatch).

Secondly, in general rather than using `$GITHUB_*` inside workflow files, prefer the equivalent `${{ github.* }}`. See GitHub docs at [here](https://docs.github.com/en/actions/learn-github-actions/variables#default-environment-variables)
and [here] (https://docs.github.com/en/actions/learn-github-actions/contexts#github-context).

Thirdly, the mechanism of variable expansions in GitHub Actions [are potentially subject to injection
attack](https://securitylab.github.com/research/github-actions-untrusted-input/)
where user input data could be mis-interpreted as code and be executed without notice.
A safer way to evaluate variables in Actions is to use [custom actions](https://docs.github.com/en/actions/creating-actions),
where variables will be passed into as input arguments and will not be expanded inside the workflow
to generate commands (e.g. defined after `run:`) to be run inside shells.

Finally, the dispatches and other GitHub API calls requires authentication. Even if they don't,
it is a good idea to [use authentication to increase the rate limit of calling API in GitHub Actions](https://docs.github.com/en/rest/overview/resources-in-the-rest-api?apiVersion=2022-11-28#rate-limiting).
See [`token.md`](https://github.com/PolusAI/sophios/blob/master/.github/workflows/token.md)
in this folder for a detailed discussion.


## Additional Notes and References:
1. Since each people creates his/her own GitHub App to authenticate dispatches
   in CIs, the cross-repo CIs are run among forks on the person's GitHub account.
   When working on a feature, even if changes are only made to one repository,
   user should fork the other repositories that the cross-repo CIs will send
   dispatch to and install on them the GitHub App for authentication.
   Otherwise, the CIs would fail since the repositories (i.e. forks) that
   dispatches are sent to don't exist and the new feature will not be properly tested.
2. If a branch with the same name doesn't exist on one of the forks in the cross-repo CIs,
   the code of the default branch of the upstream repo (PolusAI master/main) will be checked out,
   but the workflow is still run on the fork, not the upstream PolusAI repo. (We explicitly
   set the value of the repository to checkout in `lint_and_test.yml`).
3. Blog from the GitHub Security Lab on using `pull_request_target`:
[Part 1](https://securitylab.github.com/research/github-actions-preventing-pwn-requests/),
[Part 2](https://securitylab.github.com/research/github-actions-untrusted-input/),
[Part 3](https://securitylab.github.com/research/github-actions-building-blocks/).