import os
import re
import json
import yaml
import time
import copy
import logging
import argparse
import tempfile
import functools
import subprocess

from bs4 import BeautifulSoup
from distutils.version import LooseVersion

import jira
import github
import gitlab
import jenkins
import requests
import ruamel.yaml

logging.basicConfig(level=logging.INFO, format='%(levelname)-10s %(filename)s:%(lineno)d %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("__main__").setLevel(logging.INFO)

# Users / branch names / messages
BRANCH_NAME = "{prefix}_update_assisted_service_versions"
DEFAULT_ASSIGN = "oscohen"
DEFAULT_WATCHERS = ["ronniela", "romfreiman", "lgamliel", "oscohen"]
PR_MENTION = ["romfreiman", "ronniel1", "gamli75", "oshercc"]
PR_MESSAGE = "{task}, Bump OCP versions"

OCP_RELEASES = "https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/"
RHCOS_RELEASES = "https://mirror.openshift.com/pub/openshift-v4/dependencies/rhcos/{minor}"

# RCHOS version
RCHOS_LIVE_ISO_URL = "https://mirror.openshift.com/pub/openshift-v4/dependencies/rhcos/{minor}/{version}/rhcos-{version}-x86_64-live.x86_64.iso"

RCHOS_VERSION_FROM_ISO_REGEX = re.compile("coreos.liveiso=rhcos-(.*) ")
DOWNLOAD_LIVE_ISO_CMD = "curl {live_iso_url} -o {out_file}"

DEFAULT_VERSIONS_FILES = "default_ocp_versions.json"

# app-interface PR related constants
APP_INTERFACE_CLONE_DIR = "app-interface"
APP_INTERFACE_GITLAB_PROJECT = "app-interface"
APP_INTERFACE_GITLAB_REPO = f"service/{APP_INTERFACE_GITLAB_PROJECT}"
APP_INTERFACE_GITLAB = "gitlab.cee.redhat.com"
APP_INTERFACE_GITLAB_API = f'https://{APP_INTERFACE_GITLAB}'
APP_INTERFACE_SAAS_YAML = f"{APP_INTERFACE_CLONE_DIR}/data/services/assisted-installer/cicd/saas.yaml"
# For now, all environments are excluded because we don't perform any overrides
APP_INTERFACE_VERSIONS_OVERRIDE_EXCLUDED_ENVIRONMENTS = {"integration", "staging", "production"}

# assisted-service PR related constants
ASSISTED_SERVICE_CLONE_DIR = "assisted-service"
ASSISTED_SERVICE_GITHUB_REPO_ORGANIZATION = "openshift"
ASSISTED_SERVICE_GITHUB_REPO = f"{ASSISTED_SERVICE_GITHUB_REPO_ORGANIZATION}/assisted-service"
ASSISTED_SERVICE_GITHUB_FORK_REPO = "{github_user}/assisted-service"
ASSISTED_SERVICE_FORKED_URL = f"https://github.com/{ASSISTED_SERVICE_GITHUB_FORK_REPO}"
ASSISTED_SERVICE_CLONE_URL = f"https://github.com/{ASSISTED_SERVICE_GITHUB_FORK_REPO}.git"
ASSISTED_SERVICE_UPSTREAM_URL = f"https://github.com/{ASSISTED_SERVICE_GITHUB_REPO}.git"
ASSISTED_SERVICE_MASTER_DEFAULT_OCP_VERSIONS_JSON_URL = \
    f"https://raw.githubusercontent.com/{ASSISTED_SERVICE_GITHUB_REPO}/master/default_ocp_versions.json"
ASSISTED_SERVICE_OPENSHIFT_TEMPLATE_YAML = f"{ASSISTED_SERVICE_CLONE_DIR}/openshift/template.yaml"

OCP_REPLACE_CONTEXT = ['"{version}"', "ocp-release:{version}"]

RHCOS_LIVE_ISO_REGEX = re.compile("https://mirror.openshift.com/pub/openshift-v4/dependencies/rhcos/.*/(.*)/.*-x86_64-live.x86_64.iso")

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

script_dir = os.path.dirname(os.path.realpath(__file__))
CUSTOM_OPENSHIFT_IMAGES = os.path.join(script_dir, "custom_openshift_images.json")

TICKET_DESCRIPTION = "Default versions need to be updated"

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-jup",  "--jira-user-password",    help="JIRA Username and password in the format of user:pass", required=True)
    parser.add_argument("-gup",  "--github-user-password",  help="GITHUB Username and password in the format of user:pass", required=True)
    parser.add_argument("-gkf",  "--gitlab-key-file",       help="GITLAB key file", required=True)
    parser.add_argument("-gt",   "--gitlab-token",          help="GITLAB user token", required=True)
    parser.add_argument("-jkup", "--jenkins-user-password", help="JENKINS Username and password in the format of user:pass", required=True)
    parser.add_argument("--dry-run", action='store_true',   help="test run")
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

def get_rchos_version_from_iso(rhcos_latest_release, iso_url):
    live_iso_url = iso_url.format(version=rhcos_latest_release)
    with tempfile.NamedTemporaryFile() as tmp_live_iso_file:
        subprocess.check_output(
            DOWNLOAD_LIVE_ISO_CMD.format(live_iso_url=live_iso_url, out_file=tmp_live_iso_file.name), shell=True)
        try:
            os.remove("/tmp/zipl.prm")
        except FileNotFoundError:
            pass

        subprocess.check_output(f"7z x {tmp_live_iso_file.name} zipl.prm", shell=True, cwd="/tmp")
        with open("/tmp/zipl.prm", 'r') as f:
            zipl_info = f.read()
        result = RCHOS_VERSION_FROM_ISO_REGEX.search(zipl_info)
        rchos_version_from_iso = result.group(1)
        logger.info(f"Found rchos_version_from_iso: {rchos_version_from_iso}")
    return rchos_version_from_iso

def test_test_infra_passes(args, branch, release):
    username, password = get_login(args.jenkins_user_password)
    jenkins_client = jenkins.Jenkins(JENKINS_URL, username=username, password=password)
    next_build_number = jenkins_client.get_job_info(TEST_INFRA_JOB)['nextBuildNumber']
    logger.info("Test-infra build number: {}".format(next_build_number))

    github_username, *_ = get_login(args.github_user_password)
    jenkins_client.build_job(TEST_INFRA_JOB,
                             parameters={"SERVICE_BRANCH": branch,
                                         "NOTIFY": False,
                                         "JOB_NAME": "Update_ocp_version",
                                         "SERVICE_REPO":ASSISTED_SERVICE_FORKED_URL.format(github_user=github_username),
                                         "OPENSHIFT_VERSION": release}
                             )
    time.sleep(10)
    url = jenkins_client.get_build_info(TEST_INFRA_JOB, next_build_number)["url"]
    logger.info("Test-infra build url: {}".format(url))
    while not jenkins_client.get_build_info(TEST_INFRA_JOB, next_build_number)["result"]:
        logger.info(f"Waiting for job {url} to finish")
        time.sleep(30)
    job_info = jenkins_client.get_build_info(TEST_INFRA_JOB, next_build_number)
    result = job_info["result"]
    url = job_info["url"]
    logger.info(f"Job finished with result {result}")
    return result == "SUCCESS", url


def create_task(args, description: str):
    jira_client = get_jira_client(*get_login(args.jira_user_password))
    task = create_jira_ticket(jira_client, description)
    return jira_client, task


def get_default_release_json():
    res = requests.get(ASSISTED_SERVICE_MASTER_DEFAULT_OCP_VERSIONS_JSON_URL)
    if not res.ok:
        raise RuntimeError(
            f"GET {ASSISTED_SERVICE_MASTER_DEFAULT_OCP_VERSIONS_JSON_URL} failed status {res.status_code}")
    return json.loads(res.text)


def get_login(user_password):
    try:
        username, password = user_password.split(":", 1)
    except ValueError:
        logger.error("Failed to parse user:password")
        raise

    return username, password


def create_jira_ticket(jira_client, description):
    ticket_text = description
    new_task = jira_client.create_issue(project="MGMT",
                                        summary=ticket_text,
                                        priority={'name': 'Blocker'},
                                        components=[{'name': "Assisted-Installer CI"}],
                                        issuetype={'name': 'Task'},
                                        description=ticket_text)
    jira_client.assign_issue(new_task, DEFAULT_ASSIGN)
    logger.info(f"Task created: {new_task} - {JIRA_BROWSE_TICKET.format(ticket_id=new_task)}")
    # TODO debug watcherrs call
    # add_watchers(jira_client, new_task)
    return new_task


def add_watchers(jira_client, issue):
    for w in DEFAULT_WATCHERS:
        jira_client.add_watcher(issue.key, w)


def get_jira_client(username, password):
    logger.info("log-in with username: %s", username)
    return jira.JIRA(JIRA_SERVER, basic_auth=(username, password))

def clone_assisted_service(github_user):
    cmd(["rm", "-rf", ASSISTED_SERVICE_CLONE_DIR])

    cmd(["git", "clone", ASSISTED_SERVICE_CLONE_URL.format(github_user=github_user), ASSISTED_SERVICE_CLONE_DIR])

    def git_cmd(*args: str):
        return cmd(("git", "-C", ASSISTED_SERVICE_CLONE_DIR) + args)

    git_cmd("remote", "add", "upstream", ASSISTED_SERVICE_UPSTREAM_URL)
    git_cmd("fetch", "upstream")
    git_cmd("reset", "upstream/master", "--hard")

def commit_and_push_version_update_changes(message_prefix):
    def git_cmd(*args: str, stdout=None):
        return cmd(("git", "-C", ASSISTED_SERVICE_CLONE_DIR) + args, stdout=stdout)

    git_cmd("commit", "-a", "-m", f"{message_prefix} Updating versions to latest releases")

    branch = BRANCH_NAME.format(prefix=message_prefix)

    status_stdout, _ = git_cmd("status", "--porcelain", stdout=subprocess.PIPE)
    if len(status_stdout) != 0:
        for line in status_stdout.splitlines():
            logging.info(f"Found on-prem change in file {line}")

        git_cmd("commit", "-a", "-m", f"{message_prefix} Updating versions to latest releases")

    git_cmd("push", "origin", f"HEAD:{branch}")
    return branch


def verify_latest_config():
    try:
        cmd(["make", "generate-ocp-version"], cwd=ASSISTED_SERVICE_CLONE_DIR)
    except subprocess.CalledProcessError as e:
        if e.returncode == 2:
            # We run the command just for its side-effects, we don't care if it fails
            return
        raise

def open_pr(args, task):
    branch = BRANCH_NAME.format(prefix=task)

    github_client = github.Github(*get_login(args.github_user_password))
    repo = github_client.get_repo(ASSISTED_SERVICE_GITHUB_REPO)
    body = " ".join([f"@{user}" for user in PR_MENTION])
    pr = repo.create_pull(
        title=PR_MESSAGE.format(task=task),
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


def open_app_interface_pr(fork, branch, task):
    body = " ".join([f"@{user}" for user in PR_MENTION])
    body = f"{body}\nSee ticket {JIRA_BROWSE_TICKET.format(ticket_id=task)}"

    pr = fork.mergerequests.create({
        'title': PR_MESSAGE.format(task=task),
        'description': body,
        'source_branch': branch,
        'target_branch': 'master',
        'target_project_id': fork.forked_from_project["id"],
        'source_project_id': fork.id
    })

    logging.info(f"New PR opened {pr.web_url}")
    return pr

def get_latest_release_from_minor(minor_release: str, all_releases: list):
    def is_pre_release(release):
        return "-fc" in release or "-rc" in release

    all_relevant_releases = [r for r in all_releases if r.startswith(minor_release) and not is_pre_release(r)]

    if not all_relevant_releases:
        all_relevant_releases = [r for r in all_releases if r.startswith(minor_release)]


    return sorted(all_relevant_releases, key=LooseVersion)[-1]

def get_all_releases(path: str):
    res = requests.get(path)
    if not res.ok:
        raise Exception("can\'t get releses")

    page = res.text
    soup = BeautifulSoup(page, 'html.parser')
    return [node.get('href').replace("/", "") for node in soup.find_all('a')]

def get_rchos_release_from_default_version_json(ocp_version_minor, release_json):
    rchos_release_image = release_json[ocp_version_minor]['rhcos_image']
    result = RHCOS_LIVE_ISO_REGEX.search(rchos_release_image)
    rhcos_default_version = result.group(1)
    return rhcos_default_version

def is_open_update_version_ticket(args):
    jira_client = get_jira_client(*get_login(args.jira_user_password))
    open_tickets = jira_client.search_issues(f'component = "Assisted-Installer CI" AND status="TO DO" AND Summary~"{TICKET_DESCRIPTION}"', maxResults=False, fields=['summary', 'key'])
    if open_tickets:
        open_ticket_id = open_tickets[0].key
        logger.info(f"ticket {open_ticket_id} with updates waiting to get resolved, not checking for new updates until it is closed")
        return True
    return False


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


def clone_app_interface(gitlab_key_file):
    cmd(["rm", "-rf", APP_INTERFACE_CLONE_DIR])

    cmd_with_git_ssh_key(gitlab_key_file)(
        ["git", "clone", f"git@{APP_INTERFACE_GITLAB}:{APP_INTERFACE_GITLAB_REPO}.git", "--depth=1",
         APP_INTERFACE_CLONE_DIR]
    )


def commit_and_push_version_update_changes_app_interface(key_file, fork, message_prefix):
    branch = BRANCH_NAME.format(prefix=message_prefix)

    def git_cmd(*args: str):
        cmd_with_git_ssh_key(key_file)(("git", "-C", APP_INTERFACE_CLONE_DIR) + args)

    fork_remote_name = "fork"

    git_cmd("fetch", "--unshallow", "origin")
    git_cmd("remote", "add", fork_remote_name, fork.ssh_url_to_repo)
    git_cmd("checkout", "-b", branch)
    git_cmd("commit", "-a", "-m", f"{message_prefix} Updating versions to latest releases")
    git_cmd("push", "--set-upstream", fork_remote_name)

    return branch


class NoChangesNeeded(Exception):
    pass


def change_version_in_files_app_interface(openshift_versions_json):
    # Use a round trip loader to preserve the file as much as possible
    with open(APP_INTERFACE_SAAS_YAML) as f:
        saas = ruamel.yaml.round_trip_load(f, preserve_quotes=True)

    target_environments = {
        "integration": "/services/assisted-installer/namespaces/assisted-installer-integration.yml",
        "staging": "/services/assisted-installer/namespaces/assisted-installer-stage.yml",
        "production": "/services/assisted-installer/namespaces/assisted-installer-production.yml",
    }

    # Ref is used to identify the environment inside the JSON
    changed = False
    for environment, ref in target_environments.items():
        if environment in APP_INTERFACE_VERSIONS_OVERRIDE_EXCLUDED_ENVIRONMENTS:
            continue

        changed = True

        environment_conf = next(target for target in saas["resourceTemplates"][0]["targets"]
                                if target["namespace"] == {"$ref": ref})

        environment_conf["parameters"]["OPENSHIFT_VERSIONS"] = openshift_versions_json

    if not changed:
        raise NoChangesNeeded

    with open(APP_INTERFACE_SAAS_YAML, "w") as f:
        # Dump with a round-trip dumper to preserve the file as much as possible.
        # Use arbitrarily high width to prevent diff caused by line wrapping
        ruamel.yaml.round_trip_dump(saas, f, width=2 ** 16)


def main(args):
    dry_run = args.dry_run
    if dry_run:
        logger.info("Running dry-run")

    if not dry_run:
        if is_open_update_version_ticket(args) and not dry_run:
            logger.info("No updates today since there is a update waiting to be merged")
            return

    default_version_json = get_default_release_json()
    updated_version_json = copy.deepcopy(default_version_json)
    all_ocp_releases = get_all_releases(OCP_RELEASES)

    updates_made = set()

    for release in default_version_json:
        latest_ocp_release = get_latest_release_from_minor(release, all_ocp_releases)
        current_default_ocp_release = default_version_json.get(release).get("display_name")

        if current_default_ocp_release != latest_ocp_release or dry_run:
            updates_made.add(release)
            logger.info(f"New latest ocp release available for {release}, {current_default_ocp_release} -> {latest_ocp_release}")
            updated_version_json[release]["display_name"] = latest_ocp_release
            updated_version_json[release]["release_image"] = updated_version_json[release]["release_image"].replace(current_default_ocp_release, latest_ocp_release)

        rhcos_default_release = get_rchos_release_from_default_version_json(release, default_version_json)
        rhcos_latest_of_releases = get_all_releases(RHCOS_RELEASES.format(minor=release))
        rhcos_latest_release = get_latest_release_from_minor(release, rhcos_latest_of_releases)

        if rhcos_default_release != rhcos_latest_release or dry_run:
            updates_made.add(release)
            logger.info(f"New latest rhcos release available, {rhcos_default_release} -> {rhcos_latest_release}")
            updated_version_json[release]["rhcos_image"] = updated_version_json[release]["rhcos_image"].replace(rhcos_default_release, rhcos_latest_release)

            if dry_run:
                rchos_version_from_iso = "8888888"
            else:
                rchos_version_from_iso = get_rchos_version_from_iso(rhcos_latest_release, RCHOS_LIVE_ISO_URL.format(minor=release, version=rhcos_latest_release))
            updated_version_json[release]["rhcos_version"] = rchos_version_from_iso

    if updates_made:
        logger.info(f"changes were made on the fallowing versions: {updates_made}")

        if dry_run:
            jira_client, task = None, "TEST-8888"
        else:
            jira_client, task = create_task(args, TICKET_DESCRIPTION)

        clone_assisted_service(ASSISTED_SERVICE_GITHUB_REPO_ORGANIZATION if dry_run else get_login(args.github_user_password)[0])

        with open(os.path.join(ASSISTED_SERVICE_CLONE_DIR, DEFAULT_VERSIONS_FILES), 'w') as outfile:
            json.dump(updated_version_json, outfile, indent=8)
        verify_latest_config()

        if dry_run:
            return

        clone_app_interface(args.gitlab_key_file)
        with open(ASSISTED_SERVICE_OPENSHIFT_TEMPLATE_YAML) as f:
            openshift_versions_json = next(
                param["value"] for param in yaml.safe_load(f)["parameters"] if param["name"] == "OPENSHIFT_VERSIONS"
            )

        branch = commit_and_push_version_update_changes(task)
        github_pr = open_pr(args, task)
        github_pr.create_issue_comment(f"Currently no test platform is available, un-holding")
        unhold_pr(github_pr)

        try:
            change_version_in_files_app_interface(openshift_versions_json)
        except NoChangesNeeded:
            pass
        else:
            app_interface_fork = create_app_interface_fork(args)
            app_interface_branch = commit_and_push_version_update_changes_app_interface(args.gitlab_key_file, app_interface_fork, task)
            gitlab_pr = open_app_interface_pr(app_interface_fork, app_interface_branch, task)

            jira_client.add_comment(task, f"Created a PR in app-interface GitLab {gitlab_pr.web_url}")
            github_pr.create_issue_comment(f"Created a PR in app-interface GitLab {gitlab_pr.web_url}")


        # test changes,
        # TODO add a testing platform for this section
        # release_hold_label = True
        # for changed_release in updates_made:
        #     succeeded, url = test_test_infra_passes(args, branch, changed_release)
        #     if succeeded:
        #         logging.info(f"Test-infra for {changed_release} test passed, removing hold branch")
        #         github_pr.create_issue_comment(f"test-infra test passed for release {changed_release}, see {url}")
        #     else:
        #         release_hold_label = False
        #         logging.warning(f"Test-infra test failed for release {changed_release}")
        #         github_pr.create_issue_comment(f"test-infra test failed for release {changed_release}, see {url}")
        #
        # if release_hold_label:
        #     logger.info(f"All tests infra tests passed, releasing HOLD label")
        #     github_pr.create_issue_comment(f"All tests infra tests passed, releasing HOLD label")
        #     unhold_pr(github_pr)
        # else:
        #     logger.info("Some or all of the test-infra tests failed, need to be checked before removing hold label")
        #     github_pr.create_issue_comment("Some or all of the test-infra tests failed, need to be checked before removing hold label")


if __name__ == "__main__":
    main(parse_args())
