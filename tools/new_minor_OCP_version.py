import argparse
import functools
import json
import logging
import os
import re
import subprocess
import time

import github
import gitlab
import jenkins
import jira
import requests
import ruamel.yaml
import yaml

script_dir = os.path.dirname(os.path.realpath(__file__))

##############################################
# Updates OCP version by:
#  * check if new version is available
#  * open OCP version JIRA ticket
#  * create a update branch
#  * open a GitHub PR for the changes
#  * test test-infra
#  * open an app-sre GitLab PR for the changes (integration environment only, for now)
###############################################

logging.basicConfig(level=logging.INFO, format='%(levelname)-10s %(filename)s:%(lineno)d %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("__main__").setLevel(logging.INFO)

# Users / branch names / messages
BRANCH_NAME = "update_ocp_version_to_{version}"
DEFAULT_ASSIGN = "lgamliel"
DEFAULT_WATCHERS = ["ronniela", "romfreiman", "lgamliel", "oscohen"]
PR_MENTION = ["romfreiman", "ronniel1", "gamli75", "oshercc"]
PR_MESSAGE = "{task}, Bump OCP version from {current_version} to {target_version} (auto created)"
# The script scans the following message to look for previous tickets, changing this may break things!
TICKET_DESCRIPTION = "OCP version update required, Latest version {latest_version}, Current version {current_version}"

# Minor version detection
OCP_LATEST_RELEASE_URL = "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/latest/release.txt"

# app-interface PR related constants
APP_INTERFACE_CLONE_DIR = "app-interface"
APP_INTERFACE_GITLAB_PROJECT = "app-interface"
APP_INTERFACE_GITLAB_REPO = f"service/{APP_INTERFACE_GITLAB_PROJECT}"
APP_INTERFACE_GITLAB = "gitlab.cee.redhat.com"
APP_INTERFACE_GITLAB_API = f'https://{APP_INTERFACE_GITLAB}'
APP_INTERFACE_SAAS_YAML = f"{APP_INTERFACE_CLONE_DIR}/data/services/assisted-installer/cicd/saas.yaml"
CUSTOM_OPENSHIFT_IMAGES = os.path.join(script_dir, "custom_openshift_images.json")
EXCLUDED_ENVIRONMENTS = {"integration-v3", "staging", "production"}  # Don't update OPENSHIFT_VERSIONS in these envs

# assisted-service PR related constants
ASSISTED_SERVICE_CLONE_DIR = "assisted-service"
ASSISTED_SERVICE_GITHUB_REPO = "openshift/assisted-service"
ASSISTED_SERVICE_CLONE_URL = f"https://{{user_password}}@github.com/{ASSISTED_SERVICE_GITHUB_REPO}.git"
ASSISTED_SERVICE_UPSTREAM_URL = f"https://github.com/{ASSISTED_SERVICE_GITHUB_REPO}.git"
ASSISTED_SERVICE_MASTER_DEFAULT_OCP_VERSIONS_JSON_URL = \
    f"https://raw.githubusercontent.com/{ASSISTED_SERVICE_GITHUB_REPO}/master/default_ocp_versions.json"
UPDATED_FILES = ["default_ocp_versions.json", "config/onprem-iso-fcc.yaml", "onprem-environment"]
HOLD_LABEL = "do-not-merge/hold"
REPLACE_CONTEXT = ['"{ocp_version}"', "ocp-release:{ocp_version}"]
OCP_VERSION_REGEX = re.compile(r"quay\.io/openshift-release-dev/ocp-release:(.*)-x86_64")
OPENSHIFT_TEMPLATE_YAML = f"{ASSISTED_SERVICE_CLONE_DIR}/openshift/template.yaml"

# GitLab SSL
REDHAT_CERT_URL = 'https://password.corp.redhat.com/RH-IT-Root-CA.crt'
REDHAT_CERT_LOCATION = "/tmp/redhat.cert"
GIT_SSH_COMMAND_WITH_KEY = "ssh -o StrictHostKeyChecking=accept-new -i '{key}'"

# Jenkins
JENKINS_URL = "http://assisted-jenkins.usersys.redhat.com"
TEST_INFRA_JOB = "assisted-test-infra/master"

# Jira
JIRA_SERVER = "https://issues.redhat.com"
JIRA_BROWSE_TICKET = f"{JIRA_SERVER}/browse/{{ticket_id}}"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jira-user-password", help="JIRA Username and password in the format of user:pass",
                        required=True)
    parser.add_argument("--github-user-password",
                        help="GITHUB Username and password in the format of user:pass", required=True)
    parser.add_argument("--gitlab-key-file", help="GITLAB key file", required=True)
    parser.add_argument("--gitlab-token", help="GITLAB user token", required=True)
    parser.add_argument("--jenkins-user-password",
                        help="JENKINS Username and password in the format of user:pass", required=True)
    return parser.parse_args()


def cmd(command, env=None, **kwargs):
    logging.info(f"Running command {command} with env {env} kwargs {kwargs}")

    if env is None:
        env = os.environ
    else:
        env = {**os.environ, **env}

    popen = subprocess.Popen(command, env=env, **kwargs)
    stdout, stderr = popen.communicate()

    if popen.returncode != 0:
        raise subprocess.CalledProcessError(returncode=popen.returncode, cmd=command, output=stdout, stderr=stderr)

    return stdout, stderr


def cmd_with_git_ssh_key(key_file):
    return functools.partial(cmd, env={
        "GIT_SSH_COMMAND": GIT_SSH_COMMAND_WITH_KEY.format(key=key_file)
    })


def main(args):
    latest_ocp_version = get_latest_ocp_version()
    logging.info(f"Found latest version {latest_ocp_version}")
    current_assisted_service_ocp_version = get_default_ocp_version(latest_ocp_version)

    if latest_ocp_version == current_assisted_service_ocp_version:
        logging.info(f"OCP version {latest_ocp_version} is up to date")
        return

    logging.info(
        f"""OCP version mismatch,
    latest version: {latest_ocp_version}
    current version {current_assisted_service_ocp_version}""")

    jira_client, task = create_task(args, current_assisted_service_ocp_version, latest_ocp_version)

    if task is None:
        logging.info("Not creating PR because ticket already exists")
        return

    branch, openshift_versions_json = update_ai_repo_to_new_ocp_version(args,
                                                                        current_assisted_service_ocp_version,
                                                                        latest_ocp_version, task)
    logging.info(f"Using versions JSON {openshift_versions_json}")
    pr = open_pr(args, current_assisted_service_ocp_version, latest_ocp_version, task)
    test_success = test_changes(args, branch, pr)

    if test_success:
        fork = create_app_interface_fork(args)
        app_interface_branch = update_ai_app_sre_repo_to_new_ocp_version(fork, args, latest_ocp_version,
                                                                         openshift_versions_json, task)
        pr = open_app_interface_pr(fork, app_interface_branch, current_assisted_service_ocp_version,
                                   latest_ocp_version,
                                   task)

        jira_client.add_comment(task, f"Created a PR in app-interface GitLab {pr.web_url}")


def test_changes(args, branch, pr):
    logging.info("testing changes")
    succeeded, url = test_test_infra_passes(args, branch)
    if succeeded:
        logging.info("Test-infra test passed, removing hold branch")
        unhold_pr(pr)
        pr.create_issue_comment(f"test-infra test passed, HOLD label removed, see {url}")
    else:
        logging.warning("Test-infra test failed, not removing hold label")
        pr.create_issue_comment(f"test-infra test failed, HOLD label is set, see {url}")

    return succeeded


def test_test_infra_passes(args, branch):
    username, password = get_login(args.jenkins_user_password)
    jenkins_client = jenkins.Jenkins(JENKINS_URL, username=username, password=password)
    next_build_number = jenkins_client.get_job_info(TEST_INFRA_JOB)['nextBuildNumber']
    jenkins_client.build_job(TEST_INFRA_JOB,
                             parameters={"SERVICE_BRANCH": branch, "NOTIFY": False, "JOB_NAME": "Update_ocp_version"})
    time.sleep(10)
    while not jenkins_client.get_build_info(TEST_INFRA_JOB, next_build_number)["result"]:
        logging.info("waiting for job to finish")
        time.sleep(30)
    job_info = jenkins_client.get_build_info(TEST_INFRA_JOB, next_build_number)
    result = job_info["result"]
    url = job_info["url"]
    logging.info(f"Job finished with result {result}")
    return result == "SUCCESS", url


def create_task(args, current_assisted_service_ocp_version, latest_ocp_version):
    jira_client = get_jira_client(*get_login(args.jira_user_password))
    task = create_jira_ticket(jira_client, latest_ocp_version, current_assisted_service_ocp_version)
    return jira_client, task


def get_latest_ocp_version():
    return "4.6.9999"
    res = requests.get(OCP_LATEST_RELEASE_URL)
    if not res.ok:
        raise RuntimeError(f"GET {OCP_LATEST_RELEASE_URL} failed status {res.status_code}")

    _intro_text, version_yaml, *_ = res.text.split("\n---\n")

    ocp_latest_release_data = yaml.load(version_yaml)
    return ocp_latest_release_data['Name']


def get_default_ocp_version(latest_ocp_version):
    ocp_version_major, *_ = latest_ocp_version.rsplit('.', 1)

    res = requests.get(ASSISTED_SERVICE_MASTER_DEFAULT_OCP_VERSIONS_JSON_URL)
    if not res.ok:
        raise RuntimeError(
            f"GET {ASSISTED_SERVICE_MASTER_DEFAULT_OCP_VERSIONS_JSON_URL} failed status {res.status_code}")

    release_image = json.loads(res.text)[ocp_version_major]['release_image']

    result = OCP_VERSION_REGEX.search(release_image)
    ai_latest_ocp_version = result.group(1)
    return ai_latest_ocp_version


def get_login(user_password):
    try:
        username, password = user_password.split(":", 1)
    except ValueError:
        logger.error("Failed to parse user:password")
        raise

    return username, password


def create_jira_ticket(jira_client, latest_version, current_version):
    ticket_text = TICKET_DESCRIPTION.format(latest_version=latest_version, current_version=current_version)

    summaries = get_all_version_ocp_update_tickets_summaries(jira_client)

    try:
        ticket_id = next(ticket_id for ticket_id, ticket_summary in summaries if ticket_summary == ticket_text)
        logger.info(f"Ticket already exists {JIRA_BROWSE_TICKET.format(ticket_id=ticket_id)}")
        return None
    except StopIteration:
        # No ticket found, we can create one
        pass

    new_task = jira_client.create_issue(project="MGMT",
                                        summary=ticket_text,
                                        priority={'name': 'Blocker'},
                                        components=[{'name': "Assisted-Installer CI"}],
                                        issuetype={'name': 'Task'},
                                        description=ticket_text)
    jira_client.assign_issue(new_task, DEFAULT_ASSIGN)
    logger.info(f"Task created: {new_task}")
    add_watchers(jira_client, new_task)
    return new_task


def add_watchers(jira_client, issue):
    for w in DEFAULT_WATCHERS:
        jira_client.add_watcher(issue.key, w)


def get_jira_client(username, password):
    logger.info("log-in with username: %s", username)
    return jira.JIRA(JIRA_SERVER, basic_auth=(username, password))


def get_all_version_ocp_update_tickets_summaries(jira_client):
    query = 'component = "Assisted-Installer CI"'
    all_issues = jira_client.search_issues(query, maxResults=False, fields=['summary', 'key'])
    issues = [(issue.key, issue.fields.summary) for issue in all_issues]

    return set(issues)


def clone_assisted_service(user_password):
    cmd(["rm", "-rf", ASSISTED_SERVICE_CLONE_DIR])

    cmd(["git", "clone", ASSISTED_SERVICE_CLONE_URL.format(user_password=user_password),
         "--depth=1", ASSISTED_SERVICE_CLONE_DIR])

    def git_cmd(*args: str):
        return cmd(("git", "-C", ASSISTED_SERVICE_CLONE_DIR) + args)

    git_cmd("remote", "add", "upstream", ASSISTED_SERVICE_UPSTREAM_URL)
    git_cmd("fetch", "upstream")
    git_cmd("reset", "upstream/master", "--hard")


def clone_app_interface(gitlab_key_file):
    cmd(["rm", "-rf", APP_INTERFACE_CLONE_DIR])

    cmd_with_git_ssh_key(gitlab_key_file)(
        ["git", "clone", f"git@{APP_INTERFACE_GITLAB}:{APP_INTERFACE_GITLAB_REPO}.git", "--depth=1",
         APP_INTERFACE_CLONE_DIR]
    )


def change_version_in_files(old_version, new_version):
    for file in UPDATED_FILES:
        fpath = os.path.join(ASSISTED_SERVICE_CLONE_DIR, file)

        with open(fpath) as f:
            content = f.read()

        for context in REPLACE_CONTEXT:
            old_version_context = context.format(ocp_version=old_version)
            new_version_context = context.format(ocp_version=new_version)
            logging.info(f"File {fpath} - replacing {old_version_context} with {new_version_context}")
            content = content.replace(old_version_context, new_version_context)

        with open(fpath, 'w') as f:
            f.write(content)


def add_single_node_fake_4_8_release_image(openshift_versions_json):
    with open(CUSTOM_OPENSHIFT_IMAGES) as f:
        custom_images = json.load(f)

    versions = json.loads(openshift_versions_json)
    versions["4.8"] = custom_images["single-node-alpha"]
    return json.dumps(versions)


def change_version_in_files_app_interface(openshift_versions_json):
    # Use a round trip loader to preserve the file as much as possible
    with open(APP_INTERFACE_SAAS_YAML) as f:
        saas = ruamel.yaml.round_trip_load(f, preserve_quotes=True)

    target_environments = {
        "integration": "/services/assisted-installer/namespaces/assisted-installer-integration.yml",
        "integration-v3": "/services/assisted-installer/namespaces/assisted-installer-integration-v3.yml",
        "staging": "/services/assisted-installer/namespaces/assisted-installer-stage.yml",
        "production": "/services/assisted-installer/namespaces/assisted-installer-production.yml",
    }

    # TODO: This line needs to be removed once 4.8 is actually released
    openshift_versions_json = add_single_node_fake_4_8_release_image(openshift_versions_json)

    # Ref is used to identify the environment inside the JSON
    for environment, ref in target_environments.items():
        if environment in EXCLUDED_ENVIRONMENTS:
            continue

        environment_conf = next(target for target in saas["resourceTemplates"][0]["targets"]
                                if target["namespace"] == {"$ref": ref})

        environment_conf["parameters"]["OPENSHIFT_VERSIONS"] = openshift_versions_json

    with open(APP_INTERFACE_SAAS_YAML, "w") as f:
        # Dump with a round-trip dumper to preserve the file as much as possible.
        # Use arbitrarily high width to prevent diff caused by line wrapping
        ruamel.yaml.round_trip_dump(saas, f, width=2 ** 16)


def commit_and_push_version_update_changes(new_version, message_prefix):
    def git_cmd(*args: str, stdout=None):
        return cmd(("git", "-C", ASSISTED_SERVICE_CLONE_DIR) + args, stdout=stdout)

    git_cmd("commit", "-a", "-m", f"{message_prefix} Updating OCP version to {new_version}")

    branch = BRANCH_NAME.format(version=new_version)

    verify_latest_onprem_config()
    status_stdout, _ = git_cmd("status", "--porcelain", stdout=subprocess.PIPE)
    if len(status_stdout) != 0:
        for line in status_stdout.splitlines():
            logging.info(f"Found on-prem change in file {line}")

        git_cmd("commit", "-a", "-m", f"{message_prefix} Updating OCP latest onprem-config to {new_version}")

    git_cmd("git", "push", "origin", f"HEAD:{branch}")
    return branch


def commit_and_push_version_update_changes_app_interface(key_file, fork, new_version, message_prefix):
    branch = BRANCH_NAME.format(version=new_version)

    def git_cmd(*args: str):
        cmd_with_git_ssh_key(key_file)(("git", "-C", APP_INTERFACE_CLONE_DIR) + args)

    fork_remote_name = "fork"

    git_cmd("fetch", "--unshallow", "origin")
    git_cmd("remote", "add", fork_remote_name, fork.ssh_url_to_repo)
    git_cmd("checkout", "-b", branch)
    git_cmd("commit", "-a", "-m", f'{message_prefix} Updating OCP version to {new_version}')
    git_cmd("push", "--set-upstream", fork_remote_name)

    return branch


def create_app_interface_fork(args):
    with open(REDHAT_CERT_LOCATION, "w") as f:
        f.write(requests.get(REDHAT_CERT_URL).text)

    os.environ["REQUESTS_CA_BUNDLE"] = REDHAT_CERT_LOCATION

    gl = gitlab.Gitlab(APP_INTERFACE_GITLAB_API, private_token=args.gitlab_token)
    gl.auth()

    forks = gl.projects.get(APP_INTERFACE_GITLAB_REPO).forks
    try:
        fork = forks.create({})
    except gitlab.GitlabCreateError as e:
        if e.error_message['name'] != "has already been taken":
            fork = gl.projects.get(f'{gl.user.username}/{APP_INTERFACE_GITLAB_PROJECT}')
        else:
            raise

    return fork


def verify_latest_onprem_config():
    try:
        cmd(["make", "verify-latest-onprem-config"], cwd=ASSISTED_SERVICE_CLONE_DIR)
    except subprocess.CalledProcessError as e:
        if e.returncode == 2:
            # We run the command just for its side-effects, we don't care if it fails
            return

        raise


def update_ai_repo_to_new_ocp_version(args, old_ocp_version, new_ocp_version, ticket_id):
    clone_assisted_service(args.github_user_password)
    change_version_in_files(old_ocp_version, new_ocp_version)
    cmd(["make", "update-ocp-version"], cwd=ASSISTED_SERVICE_CLONE_DIR)

    with open(OPENSHIFT_TEMPLATE_YAML) as f:
        openshift_versions_json = next(param["value"] for param in
                                       yaml.safe_load(f)["parameters"] if
                                       param["name"] == "OPENSHIFT_VERSIONS")

    branch = commit_and_push_version_update_changes(new_ocp_version, ticket_id)
    return branch, openshift_versions_json


def update_ai_app_sre_repo_to_new_ocp_version(fork, args, new_ocp_version, openshift_versions_json, ticket_id):
    clone_app_interface(args.gitlab_key_file)

    change_version_in_files_app_interface(openshift_versions_json)

    return commit_and_push_version_update_changes_app_interface(args.gitlab_key_file, fork, new_ocp_version, ticket_id)


def open_pr(args, current_version, new_version, task):
    branch = BRANCH_NAME.format(version=new_version)

    github_client = github.Github(*get_login(args.git_user_password))
    repo = github_client.get_repo(ASSISTED_SERVICE_GITHUB_REPO)
    body = " ".join([f"@{user}" for user in PR_MENTION])
    pr = repo.create_pull(
        title=PR_MESSAGE.format(current_version=current_version, target_version=new_version, task=task),
        body=body,
        head=f"{github_client.get_user().login}:{branch}",
        base="master"
    )
    hold_pr(pr)
    logging.info(f"new PR opened {pr.url}")
    return pr


def hold_pr(pr):
    pr.create_issue_comment('/hold')


def unhold_pr(pr):
    pr.create_issue_comment('/unhold')


def open_app_interface_pr(fork, branch, current_version, new_version, task):
    body = " ".join([f"@{user}" for user in PR_MENTION])
    body = f"{body}\nSee ticket {JIRA_BROWSE_TICKET.format(ticket_id=task)}"

    pr = fork.mergerequests.create({
        'title': PR_MESSAGE.format(current_version=current_version, target_version=new_version, ticket_id=task),
        'description': body,
        'source_branch': branch,
        'target_branch': 'master',
        'target_project_id': fork.forked_from_project["id"],
        'source_project_id': fork.id
    })

    hold_pr(pr)

    logging.info(f"New PR opened {pr.web_url}")

    return pr


if __name__ == "__main__":
    main(parse_args())
