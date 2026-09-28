// Test IDs for the deploy pipeline feature (SSH-driven remote training,
// testing and deployment wizard, rendered as a tab inside ModelDeploy).
// See ./index.js for the recipe to add a new feature file.

export const DEPLOY_PIPELINE = {
  startRunButton: 'deploy-pipeline-start-run-button',
  rollbackToggleButton: 'deploy-pipeline-rollback-toggle-button',
  rollbackSubmitButton: 'deploy-pipeline-rollback-submit-button',
  runRow: 'deploy-pipeline-run-row',
  hostInput: 'deploy-pipeline-host-input',
  portInput: 'deploy-pipeline-port-input',
  usernameInput: 'deploy-pipeline-username-input',
  passwordInput: 'deploy-pipeline-password-input',
  pemInput: 'deploy-pipeline-pem-input',
  remoteWorkdirInput: 'deploy-pipeline-remote-workdir-input',
  remoteDataYamlInput: 'deploy-pipeline-remote-data-yaml-input',
  remoteBaseModelInput: 'deploy-pipeline-remote-base-model-input',
  remoteProductionModelInput: 'deploy-pipeline-remote-production-model-input',
  epochsInput: 'deploy-pipeline-epochs-input',
  trainPctInput: 'deploy-pipeline-train-pct-input',
  validPctInput: 'deploy-pipeline-valid-pct-input',
  testPctInput: 'deploy-pipeline-test-pct-input',
  stageSubmitButton: 'deploy-pipeline-stage-submit-button',
  runTestsButton: 'deploy-pipeline-run-tests-button',
  approveButton: 'deploy-pipeline-approve-button',
  rejectButton: 'deploy-pipeline-reject-button',
  progressBar: 'deploy-pipeline-progress-bar',
};
