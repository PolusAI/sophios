// See https://docs.github.com/en/actions/creating-actions/creating-a-javascript-action
// The runner runs this file as it is (runs.main), so it requires nothing but Node's built-ins.

const fs = require('fs');

const getInput = (name) => (process.env['INPUT_' + name.toUpperCase()] || '').trim();

async function main() {
  const github_repository = getInput('github_repository');
  const event_type = getInput('event_type');
  const sender_repo = getInput('sender_repo');
  const operating_system = getInput('operating_system');
  const commit_message = getInput('commit_message');
  const mm_workflows_ref = getInput('mm_workflows_ref');
  const workflow_success = getInput('workflow_success');
  const access_token = getInput('access_token');

  if (!access_token) {
    console.log("Error! access_token is not defined! (or expired)");
  }

  const url_dispatches = "https://api.github.com/repos/" + process.env.GITHUB_REPOSITORY.split('/')[0] + "/" + sender_repo.split("/")[1] + "/dispatches";
  console.log(`url_branches: ${url_dispatches}`);
  console.log(`access_token: ${access_token}`);

  const run_name = commit_message + " - " + mm_workflows_ref + " - " + operating_system;

  if (event_type == "repository_dispatch") {
    console.log(`Replying to sender_repo: ${sender_repo}`);
    const response = await fetch(url_dispatches, {
      method: "POST",
      body: JSON.stringify({
        event_type: "ci_status",
        client_payload: {
          repository: github_repository,
          success: workflow_success,
          run_name: run_name,
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
  }

  // Get the JSON webhook payload for the event that triggered the workflow
  const event = process.env.GITHUB_EVENT_PATH ? JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8')) : {};
  const payload = JSON.stringify(event, undefined, 2)
  console.log(`The event payload: ${payload}`);
}

main().catch((error) => {
  console.log(`::error::${error.message}`);
  process.exitCode = 1;
});
