// See https://docs.github.com/en/actions/creating-actions/creating-a-javascript-action
// The runner runs this file as it is (runs.main), so it requires nothing but Node's built-ins.

const fs = require('fs');

const getInput = (name) => (process.env['INPUT_' + name.toUpperCase()] || '').trim();

async function main() {
  const repository = getInput('repository');
  const workflow_yml = getInput('workflow_yml');
  const sender_repo = getInput('sender_repo');
  const sender_repo_owner = getInput('sender_repo_owner');
  const dispatch_ref = getInput('dispatch_ref');
  const wic_owner = getInput('wic_owner');
  const wic_ref = getInput('wic_ref');
  const event_type = getInput('event_type');
  const commit_message = getInput('commit_message');
  const mm_workflows_owner = getInput('mm_workflows_owner');
  const mm_workflows_ref = getInput('mm_workflows_ref');
  const image_workflows_owner = getInput('image_workflows_owner');
  const image_workflows_ref = getInput('image_workflows_ref');
  const access_token = getInput('access_token');

  if (!access_token) {
    console.log("Error! access_token is not defined! (or expired)");
  }

  // Use base repository owner, otherwise permission errors: Resource not accessible by integration.
  const url_dispatches = "https://api.github.com/repos/" + process.env.GITHUB_REPOSITORY.split('/')[0] + "/" + repository + "/actions/workflows/" + workflow_yml + "/dispatches";
  console.log(`url_branches: ${url_dispatches}`);
  console.log(`access_token: ${access_token}`);

  // Note: In PRs, the branch to which the dispatch is sent to is not necessarily the same
  // as the branch to be checked out. Especially for the case of upstream being PolusAI, the
  // dispatch should be sent to the 'master' branch while the code to be checked out should be
  // the feature branch of the user's fork. When seeing error message: "message":"No ref found for: ..."
  // the problem is in fact that (with everyone using their own GitHub App for authentication),
  // the username is wrong (i.e. username in dispatch URL being the upstream, and different from the
  // sender).
  const response = await fetch(url_dispatches, {
    method: "POST",
    body: JSON.stringify({
      ref: dispatch_ref,
      inputs: {
        "event_type": event_type,
        "commit_message": commit_message,
        "sender_repo": sender_repo,
        "sender_repo_owner": sender_repo_owner,
        "wic_owner": wic_owner,
        "wic_ref": wic_ref,
        "mm-workflows_owner": mm_workflows_owner,
        "mm-workflows_ref": mm_workflows_ref,
        "image-workflows_owner": image_workflows_owner,
        "image-workflows_ref": image_workflows_ref,
      },
    }),
    headers: {
//      "Accept": "application/vnd.github+json",
//      "X-GitHub-Api-Version": "2022-11-28",
      'Authorization': `Bearer ${access_token}`
    }
  });
  const response_str = await response.text();
  console.log(`response: ${response_str}`);
  const delimiter = `ghadelimiter_${crypto.randomUUID()}`;
  fs.appendFileSync(process.env.GITHUB_OUTPUT, `response<<${delimiter}\n${response_str}\n${delimiter}\n`);

  // Get the JSON webhook payload for the event that triggered the workflow
  const event = process.env.GITHUB_EVENT_PATH ? JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8')) : {};
  const payload = JSON.stringify(event, undefined, 2)
  console.log(`The event payload: ${payload}`);
}

main().catch((error) => {
  console.log(`::error::${error.message}`);
  process.exitCode = 1;
});
