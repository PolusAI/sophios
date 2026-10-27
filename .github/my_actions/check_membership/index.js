// See https://docs.github.com/en/actions/creating-actions/creating-a-javascript-action
// The runner runs this file as it is (runs.main), so it requires nothing but Node's built-ins.

// !!!!!! CRITICAL: PLEASE READ !!!!!!
// https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions- settings-for-a-repository#controlling-changes-from-forks-to-workflows-in-public-repositories
// "Note: Workflows triggered by pull_request_target events are run in the context of the base
// branch. Since the base branch is considered trusted, workflows triggered by these events will always run,
// REGARDLESS OF APPROVAL SETTINGS."
// Therefore, membership must be checked to authorize PRs to run workflows.

const fs = require('fs');

const getInput = (name) => (process.env['INPUT_' + name.toUpperCase()] || '').trim();

async function main() {
  const account_name = getInput('account_name');
  const access_token = getInput('access_token');

  if (!access_token) {
    console.log("Error! access_token is not defined! (or expired)");
  }

  const [owner, repo] = process.env.GITHUB_REPOSITORY.split('/');

  let verified = false;
  if (!verified) {
    // Step 1: Check whether the account to check is the owner of the repository
    verified = account_name === owner;
  }

  if (!verified) {
    // Step 2: Check whether the account is an external collaborator of the repository

    // GitHub API doc: https://docs.github.com/en/rest/collaborators/collaborators?apiVersion=2022-11-28#check-if-a-user-is-a-repository-collaborator
    // Note: the user is only checked against the collaborator list of the current repository.
    // So for cross repo CIs, the sender should be added as a collaborator to all repos.
    const url = `https://api.github.com/repos/${owner}/${repo}/collaborators/${account_name}`
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${access_token}`
      }
    });
    verified = response.status === 204;
    if (!verified) {
        console.log(`Collaborator check failed with status: ${response.status}`)
        console.log(`url: ${url}`);
        console.log(`response.json(): ${JSON.stringify(await response.json(), undefined, 2)}`);
    }
  }

  if (!verified && owner === 'PolusAI') {
    // Step 3: Check whether the account is a member of the PolusAI.
    // This step will only be run when the workflow is currently running on PolusAI, which is an
    // org account. The above endpoint repos/{owner}/{repo}/collaborators/{account} can also checks
    // membership but only when the member is a direct collaborator of the org-owned repo, or has
    // access through team memberships or default org permissions. To be safe, use the explicit
    // membership-check endpoint.

    // GitHub API doc: https://docs.github.com/en/rest/orgs/members?apiVersion=2022-11-28#check-organization-membership-for-a-user
    const url = `https://api.github.com/orgs/PolusAI/members/${account_name}`
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${access_token}`
      }
    });
    verified = response.status === 204;
    if (!verified) {
        console.log(`Membership check failed with status: ${response.status}`)
        console.log(`url: ${url}`);
        console.log(`response.json(): ${JSON.stringify(await response.json(), undefined, 2)}`);
    }
  }

  // GitHub is not very clear about type declaration in JS actions and payloads in API calls.
  // See the discussion: https://github.com/actions/runner/issues/1483#issuecomment-969295757
  // To avoid ambiguity, use string for output and string comparison in workflow steps.
  const delimiter = `ghadelimiter_${crypto.randomUUID()}`;
  fs.appendFileSync(process.env.GITHUB_OUTPUT, `verified<<${delimiter}\n${verified.toString()}\n${delimiter}\n`);

  // Get the JSON webhook payload for the event that triggered the workflow
  const event = process.env.GITHUB_EVENT_PATH ? JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8')) : {};
  const payload = JSON.stringify(event, undefined, 2)
  console.log(`The event payload: ${payload}`);

  if (!verified) {
    throw new Error(`Account ${account_name} is not authorized to run workflows.`);
  }
}

main().catch((error) => {
  console.log(`::error::${error.message}`);
  process.exitCode = 1;
});
