# Overview
Cross-repo CIs is necessary when multiple repositories are interdependent and we want
to ensure that changes in one repository do not adversely affect the other
repositories. It involves events on one repository (e.g. `push` of a regular commit)
triggering CIs on other repositories. The GitHub RESTFul API endpoint for requesting
a workflow run of one repository from another repository is [`repository_dispatch`](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#repository_dispatch). The workflow with
`repository_dispatch` as a trigger only runs on the default branch (`master/main`), to
use the correct workflow file and code from the targeting branch, a subsequent
[`workflow_dispatch`](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#workflow_dispatch)
is called. Both API calls requires authentication.

There are a couple of ways to perform [authentication in GitHub API calls](https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api?apiVersion=2022-11-28),
including the `GITHUB_TOKEN`, classic personal access tokens,
fine-grained personal access tokens, OAuth apps, and GitHub Apps.

`GITHUB_TOKEN` is the [default authentication method used in GitHub Actions workflow](https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api?apiVersion=2022-11-28#authenticating-in-a-github-actions-workflow).
It is [automatically generated at the beginning of a workflow run](https://docs.github.com/en/actions/security-guides/automatic-token-authentication)
and can authorize API calls in jobs of the workflow. However, it can be only used [in
the same workflow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow)
that generates it. In addition, `GITHUB_TOKEN` can [authorize the
`workflow_dispatch` and `repository_dispatch` API calls](https://github.blog/changelog/2022-09-08-github-actions-use-github_token-with-workflow_dispatch-and-repository_dispatch/),
but only for [dispatches to the same repository](https://github.com/marketplace/actions/repository-dispatch).
(Yes, you can use `repository_dispatch` to trigger workflows of the same repository.)
Therefore, a secure way is needed to authenticate API calls across repositories.

This doc discusses why using GitHub App to authenticate cross-repo GitHub API calls
(particularly `repository_dispatch`), and how to securely do it in various triggering
events (e.g. `pull_request` in which the sender could be an external actor).

# Why Using GitHub App
There are a couple of reasons and advantages of GitHub App over other authentication
methods. The primary ones include the following:

1. GitHub Apps use a granular permission control model on resources at multiple levels:
repository, organization, and user account, while OAuth Apps and classic personal
access tokens use the concept of scopes with excessive permissions. Only the minimum
necessary permissions are given. In the case of a leak, the damages are reduced to
the minimum.
2. GitHub Apps generate an one-time access token for authentication with an expiration
time. The leak of this one-time token will not cause a disaster. However, the private
keys of Apps need to be securely stored.
3. Authentication with tokens for API calls which don't need authentication enjoy an
elevated API rate limit. For GitHub Apps, the rate limit is 5000 per hour, while it is
60 per hour without an authentication.
4. GitHub Apps act independently of an user. Granting permissions to a GitHub App
doesn't consume a seat compared with adding a user as collaborators to a repo and
using his personal access token for authentication.

As a particular note, fine-grained personal access token (PAT) also uses the granular
permission control model and can authorize API calls on behalf of users. But GitHub
mandates an expiration of PATs. Admins of repositories need to remember when the PATs
are going to epxire and regenerate them before that. GitHub Apps, on the other hand,
only require a one-time setup and that's it.

### References and Additional Reads
1. [Authentication with a GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).
2. [GitHub App versus other options](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/deciding-when-to-build-a-github-app)
3. [Granular permission(s) required by GitHub Apps to authenticate GitHub API calls](https://docs.github.com/en/rest/overview/permissions-required-for-github-apps?apiVersion=2022-11-28)


# How to Use GitHub App for Authentication, Securely

The basic mechanism of using GitHub Apps for authentication works as follows.

1. Create a GitHub App with the minimum [necessary permissions](https://docs.github.com/en/rest/overview/permissions-required-for-github-apps?apiVersion=2022-11-28)
for desired API calls.
2. Install the App on the repositories you want it to
authenticate for. If the repositories belong to other users, they should initiate the
installation.
3. Generate a key-pair for the App and store the private key and the App ID (note:
not client ID) as secrets (e.g. `APP_ID` and `APP_PRIVATE_KEY`) on the repositories
that needs authentication tokens.
4. Add a step in workflow files to generate `installation token`
before the steps of the API calls requiring authentication. Pass the generated token
to the `Authentication` header of the API calls. For syntax of using outputs of
previous steps in a workflow file, see [here](https://docs.github.com/en/actions/using-jobs/defining-outputs-for-jobs).

To generate installation tokens, we can use either third-party actions or custom
actions. When using third-party actions, it is the best to specify the SHA code
or the tag of the commit you want to use, [preventing bad actors to alter behaviors
of the actions and create security loopholes](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions#using-third-party-actions).

Currently, we use the third-party action `tibdex/github-app-token@b62528385c34dbc9f38e5f4225ac829252d1ea92`, as demonstrated in [this
example](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow)
in GitHub docs.

## Security: Avoid Sharing Private Keys of GitHub Apps
GitHub Apps uses key-pairs to [generate one-time tokens for GitHub API authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps).
It is a good practice to not share private keys among accounts, even for members of
an organization, avoiding cascading risks from security flaws on other's repositories.
Therefore, each member who forks the repositoris with cross-repo CIs will need
to create their own GitHub Apps and stores their private keys and App IDs as
secrets on their own forks with names matching those used in the workflow files
(i.e. `APP_ID` and `APP_PRIVATE_KEY`). When opening a PR to the upstream PolusAI
repository, it is the GitHub App on the PolusAI account used to generate tokens
and authenticate API calls in the workflows. Forks will never need to access
private keys of the PolusAI GitHub App.

As a reminder, using this approach requires users to fork all repositories on which
workflows will be run in the cross-repo CIs, although changes might only be made on
one repository. For example, push on `mm-workflow` triggers workflow runs on
`sophios`. Even new features are only added to `mm-workflow` and
the testing only needs code form the default `master` branch of
`sophios`, `sophios` should still be forked,
because the user's GitHub App is not installed on PolusAI and can only anthenticate API
calls to repositories owned by the user.

## Security: Block Access to Secrets and Prevent Running Custom Actions from External Actors
Using either GitHub Apps or fine-grained PATs to authenticate API calls will need to
store either the private keys of the Apps or the PATs as [secrets](https://docs.github.com/en/actions/security-guides/encrypted-secrets)
on repositories. Access to secrets from external actors should be reduced to the
minimum, since their leak would give unauthorized actors write permissions to your
repositories. Apart from not sharing them with others, we should also only let
trusted accounts to use them in workflows.

GitHub, by default, forbids all accounts other than the owner to access secrets
on public repositoies, including external collaborators explicitly invited by
the repository owner. ([Private repositories have the option to grant access to
secrets.](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository#enabling-workflows-for-forks-of-private-repositories)). This creates a dilemma that we'd like PolusAI members to be able
to run workflows on PRs to the upstream PolusAI repo, and the workflows require
access to secrets in order to generate authentication token, which are not accessible
in workflows triggered by `pull_request`.

The workaround is to use `pull_request_target`, which gives access to secrets on
the upstream PolusAI repository, while the sender of the event is the fork owner.
For security, GitHub restrict `pull_request_target` to run [in the context of the
base branch of a PR](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#pull_request_target).
However, we deliberately checkout the code from the head of the PR to ensure the
changes in the PR pass the tests in CIs (which is the whole point of running CIs).
[Checking out the head of a PR violates the security recommendations of GitHub](https://securitylab.github.com/research/github-actions-preventing-pwn-requests/),
since it could allow random external actors to checkout their code with harmful
changes and run on our self-hosted runners. To eliminate the security loophole, a
step of checking membership against a whitelist is added to all workflows that could
be triggered by `pull_request_target` (directly or indirectly by cross-repo CIs).
They include the workflows with `repository_dispatch` as the triggering event since
the dispatches could be sent from PRs onto dependent repositories, e.g. PRs to `PolusAI/mm-workflows` would send dispatches to
`PolusAI/sophios`. In this step, we check whether the **sender** of the event is an external collaborator of the repository or a member of the PolusAI organization. If not, this step will fail and stop the workflow.

See `.github/my_actions/check_membership` and the `Check Membership` step in
`branch_dispatch_pull_request.yml`.

As an additional note, the workflows should always use the `check_membership` custom
action from the base branch of a PR. The action could be altered if the head of a PR
is accidentally checked out.

### References and Additional Reads
1. Blog from the GitHub Security Lab on using `pull_request_target`:
[Part 1](https://securitylab.github.com/research/github-actions-preventing-pwn-requests/),
[Part 2](https://securitylab.github.com/research/github-actions-untrusted-input/),
[Part 3](https://securitylab.github.com/research/github-actions-building-blocks/).
2. [Best practices of creating a GitHub App](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app)

# Additional Notes
1. A distinctive scenario uncommon to
regular push events in a single repository is that the user who triggered the event
(the `sender` property in webhook payloads) could be different from
the owner of the repository running the workflow (e.g. in `pull request`). In the
membership check step, extra care is required to check membership of the sender
of **the initial triggering event**, which might not necessarily be the
sender of the current event. Therefore, in `branch_dispatch_*.yml`, information of the sender of the very first event is passed explicitly by variables `sender_repo_*` to
provide tracebility.