#  Copyright 2019 MongoDB Inc.
#
#  Licensed to the Apache Software Foundation (ASF) under one
#  or more contributor license agreements.  See the NOTICE file
#  distributed with this work for additional information
#  regarding copyright ownership.  The ASF licenses this file
#  to you under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance
#  with the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing,
#  software distributed under the License is distributed on an
#  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#  KIND, either express or implied.  See the License for the
#  specific language governing permissions and limitations
#  under the License.

import pathlib
import sys
import webbrowser

import requests

from invoke import task, UnexpectedExit

from serverworkflowtool import config
from serverworkflowtool.config import DownloadConfig
from serverworkflowtool.templates import evergreen_yaml_template, shell_profile_template
from serverworkflowtool.utils.log import get_logger, actionable, log_func, log_multiline, req_input


def evergreen_yaml(conf):
    # initialize Jira to get the jira user name for Evergreen.
    if config.EVG_CONFIG_FILE.exists():
        get_logger().info(
            'Found existing ~/.evergreen.yml, skipping adding Evergreen configuration')
        get_logger().info(
            'Please ensure your ~/.evergreen.yml was generated by this tool. If not, '
            'make sure you know what\'s in there')
    else:
        settings_url = 'https://evergreen.mongodb.com/login/key'
        while True:
            res = requests.post(settings_url, json={'username': conf.username, 'password': conf.jira_pwd})
            if res.status_code != 200:
                get_logger().error('Failed to fetch API key from evergreen. Error: %s', str(res))
                req_input('Press any key to retry...')
                conf.reset_jira_credentials()
                continue
            res_json = res.json()

            evg_config = evergreen_yaml_template.format(res_json['user'], res_json['api_key'])

            with open(config.EVG_CONFIG_FILE, 'w') as fh:
                fh.write(evg_config)
            break

    return True


def ssh_keys(ctx):
    ssh_dir = config.HOME / '.ssh'
    ssh_dir.mkdir(exist_ok=True)

    if config.SSH_KEY_FILE.is_file():
        get_logger().info('Found existing key ~/.ssh/id_rsa, skipping setting up ssh keys')
        get_logger().info('Please ensure your keys are added to your GitHub account')
        return
    else:
        get_logger().info('Did not find existing ssh key in ~/.ssh/id_rsa')

    with open(ssh_dir / 'config', 'w') as fh:
        get_logger().info('Disabling host key checking for github.com in %s',
                          str(ssh_dir / 'config'))
        fh.write('Host github.com\n\tStrictHostKeyChecking no\n')

    res = req_input('Opening browser for instructions to setting up ssh keys in GitHub, '
                    'press any key to continue, enter "skip" to skip: ')

    if res != 'skip':
        webbrowser.open(config.GITHUB_SSH_HELP_URL)
        get_logger().info(actionable('Once you\'ve generated SSH keys and added them to GitHub, '
                                     'please rerun `workflow setup.macos`'))
        sys.exit(0)

    else:
        get_logger().info('Skipping adding SSH Keys to GitHub')


def clone_repos(ctx):
    config.REPO_ROOT.mkdir(exist_ok=True)
    get_logger().info('Placing MongoDB Git repositories in %s', config.REPO_ROOT)

    with ctx.cd(str(config.REPO_ROOT)):
        for repo_config in config.REQUIRED_REPOS:
            repo_dir = config.REPO_ROOT / repo_config.relative_local
            if repo_dir.exists():
                get_logger().warning('Local directory %s exists.', str(repo_dir))
                get_logger().warning('If you\'d like to re-clone, please delete this directory first')
            else:
                cmd = f'git clone {repo_config.remote} {repo_dir}'
                get_logger().info(cmd)
                ctx.run(cmd, hide=False)


def create_dir(ctx, conf, dir_absolute):
    d = pathlib.Path(dir_absolute)
    if d.exists() and d.owner() == config.USER:
        get_logger().warning(f'Directory {d} exists and is owned by the current user, skipping creation')
        return True

    # Need Invoke for sudo. Can't use native Python mkdir().
    ctx.sudo(f'mkdir -p {d}', warn=False, password=conf.get_sudo_pwd(ctx))
    ctx.sudo(f'chown {config.USER} {d}', warn=False, password=conf.get_sudo_pwd(ctx))

    get_logger().info(f'Created directory {d}')


def _do_download(ctx, download_config):
    with ctx.cd(str(config.HOME)):
        local_path = config.HOME / download_config.relative_local
        if local_path.exists():
            get_logger().warning('File %s exists. If you\'d like to re-download this '
                                 'file, please delete the local copy first.', local_path)
        else:
            cmd = f'curl -o {download_config.relative_local} {download_config.remote}'
            get_logger().info(cmd)
            ctx.run(cmd)


def _download_executable_tarball(ctx, download_config, default_name, pretty_name):
    bin_dir = config.HOME / 'bin'

    if (bin_dir / pretty_name).exists():
        get_logger().warning('File/Directory %s already exists. Skipping install', str(bin_dir / pretty_name))
        return

    download_config.relative_local = f'bin/{pretty_name}.tar.xz'
    _do_download(ctx, download_config)

    with ctx.cd(str(bin_dir)):
        ctx.run(f'tar -xvzf {pretty_name}.tar.xz')
        ctx.run(f'rm -f {pretty_name}.tar.xz')
        ctx.run(f'mv {default_name} {pretty_name}')


def download_clang_format(ctx):
    bin_dir = config.HOME / 'bin'

    if (bin_dir / 'clang-format').exists():
        get_logger().warning('File %s already exists. Skipping install',
                             str(bin_dir / 'clang-format'))
        return

    dc = DownloadConfig(config.CLANG_FORMAT_URL)

    default_name = 'clang+llvm-3.8.0-x86_64-apple-darwin'
    pretty_name = 'llvm-3.8.0'

    _download_executable_tarball(ctx, dc, default_name=default_name, pretty_name=pretty_name)

    with ctx.cd(str(bin_dir)):
        # softlink clang-format to PATH.
        ctx.run(f'ln -s {str(bin_dir / pretty_name / "bin" / "clang-format")} clang-format')


def download_eslint(ctx):
    dc = DownloadConfig(config.ESLINT_URL)

    default_name = 'eslint-Darwin-x86_64'
    pretty_name = 'eslint'

    _download_executable_tarball(ctx, dc, default_name=default_name, pretty_name=pretty_name)


def download_evergreen(ctx):
    bin_dir = config.HOME / 'bin'

    if (bin_dir / 'evergreen').exists():
        get_logger().warning('File %s already exists. Skipping downloading evergreen CLI',
                             str(bin_dir / 'evergreen'))
    else:
        dc = DownloadConfig(
            'https://evergreen.mongodb.com/clients/darwin_amd64/evergreen',
            relative_local='bin/evergreen'
        )

        _do_download(ctx, dc)

    with ctx.cd(str(bin_dir)):
        # chmod is cheap enough that we'll just always do it instead of checking if it's already done.
        ctx.run('chmod +x evergreen')


def install_githooks(ctx):
    hooks_dir = config.HOME / '.githooks'

    # Dupe of path defined in REQUIRED_REPO.
    kernel_tools_dir = config.REPO_ROOT / 'kernel-tools'
    mongo_dir = config.REPO_ROOT / 'mongo'

    hooks_dir.mkdir(exist_ok=True, parents=True)

    if not (hooks_dir / 'mongo').exists():
        ctx.run(f'ln -s {str(kernel_tools_dir / "githooks")} {str(hooks_dir / "mongo")}')

    with ctx.cd(str(mongo_dir)):
        ctx.run(f'source buildscripts/install-hooks -f')

        todo = actionable('TODO:')
        get_logger().info(f'{todo} Please consult with your mentor on which githooks are needed. Some hooks may be')
        get_logger().info('      unnecessarily cumbersome for your project. You can delete any unneeded hooks in ')
        get_logger().info(f'     `%s` and rerun `workflow macos.setup`',
                          str(kernel_tools_dir / "githooks" / "pre-push"))


def setup_mongo_repo_env(ctx):
    mongo_dir = config.REPO_ROOT / 'mongo'
    python3_venv_dir = 'python3-venv'

    def run_cmds(cmds):
        for cmd in cmds:
            res = ctx.run(cmd)
            get_logger().info('Ran cmd: %s', res.command)

    with ctx.cd(str(mongo_dir)):
        if not (mongo_dir / python3_venv_dir).exists():
            install_venv_cmds = [
                'python3 -m pip install virtualenv',
                f'python3 -m virtualenv {python3_venv_dir}',
            ]

            run_cmds(install_venv_cmds)
        else:
            get_logger().warning('Found existing Python3 virtualenv at %s, skipping creating a new one',
                                 str(mongo_dir / python3_venv_dir))

        with ctx.prefix('source python3-venv/bin/activate'):
            install_cmds = [
                'pip install -r etc/pip/dev-requirements.txt',
                'pip install regex',
            ]

            # Lazily create build.ninja since it takes a long time.
            if not pathlib.Path(mongo_dir / 'build.ninja').exists():
                install_cmds.append(
                    'python buildscripts/scons.py CC=clang CXX=clang++ VARIANT_DIR=ninja  MONGO_VERSION=\'0.0.0\' '
                    'MONGO_GIT_HASH=\'unknown\' --link-model=dynamic build.ninja')

            run_cmds(install_cmds)

        compiledb_cmds = [
            'ninja compiledb'
        ]
        run_cmds(compiledb_cmds)


def install_ninja(ctx):
    try:
        ctx.run('ninja --version')
    except UnexpectedExit:
        ctx.run('brew install ninja')
    else:
        get_logger().warning('ninja appears to be already installed, skipping install')


def install_shell_profile(ctx):
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    profile = config.CONFIG_DIR / 'profile'
    with open(profile, 'w') as fh:
        fh.write(shell_profile_template)

    todo = actionable('TODO:')
    get_logger().info(f'{todo} Please add the following line to your shell config file, if you haven\'t already ')
    get_logger().info('      done so. The default shell config file is ~/.profile. If you\'re using a')
    get_logger().info('      different shell, e.g. zsh, you may have a different config file, e.g. ~/.zshrc')
    get_logger().info('')
    get_logger().info(actionable('      source %s'), str(profile))


def post_task_instructions():
    lines = [
        actionable('Note on Using "compiledb":'),
        '    A Clang JSON Compilation Database (compiledb) has been generated for the mongo repository.',
        '    It enables features like jump to definition and semantic code completion in code editors. Please refer',
        '    to the following web page on how to integrate compiledb with your favorite editor',
        '',
        '    https://sarcasm.github.io/notes/dev/compilation-database.html#text-editors-and-ides',
        '',
        '    When you switch branches or add/remove files, compiledb needs to be updated by running `ninja compiledb`',
        '',
        '    If you\'d like to use an editor that "just works", The CLion IDE is a good option. You just need',
        '    to install it and open the "mongo" directory. Code completion and jumping to definitions will',
        '    automatically work',
        '',
        '    To install CLion, run: `brew cask install clion`'
    ]

    log_multiline(get_logger().info, lines)


@task
def macos(ctx):
    """
    Set up macOS for MongoDB server development.

    If you're running the workflow tool for the first time, please use the bootstrap script from README.md.
    """
    conf = config.Config()

    funcs = [
        # Do tasks that require user interaction first.
        (lambda: ssh_keys(ctx), 'Configure SSH Keys'),
        (lambda: create_dir(ctx, conf, '/data'), 'Create MongoDB Data Directory'),
        (lambda: create_dir(ctx, conf, '/opt/mongodbtoolchain'), 'Create MongoDB Toolchain Directory'),
        (lambda: create_dir(ctx, conf, str(config.HOME / 'bin')), 'Create User bin Directory'),

        # Then do the automated tasks that don't require user interaction.
        (lambda: evergreen_yaml(conf), 'Configure Evergeen'),
        (lambda: clone_repos(ctx), 'Clone MongoDB Repositories'),
        (lambda: download_clang_format(ctx), 'Download clang-format'),
        (lambda: download_eslint(ctx), 'Download eslint'),
        (lambda: download_evergreen(ctx), 'Download evergreen CLI'),
        (lambda: install_ninja(ctx), 'Install ninja'),

        # Next do mongo repo setup. These tasks require the system setup steps above to have run.
        (lambda: setup_mongo_repo_env(ctx), 'Setup the mongo Repository'),

        # Do tasks that require followup work last.
        (lambda: install_githooks(ctx), 'Install Git Hooks'),
        (lambda: install_shell_profile(ctx), 'Install Shell Profile'),
        (lambda: post_task_instructions(), 'Post Setup Instructions')
    ]

    for func in funcs:
        log_func(func[0], func[1])
