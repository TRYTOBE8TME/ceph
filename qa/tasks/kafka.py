"""
Deploy and configure Kafka for Teuthology
"""
from io import BytesIO
from io import StringIO
from configobj import ConfigObj
import base64
import argparse
import contextlib
import logging
import os
import random
import six
import string
import subprocess
import json
import sys
from pathlib import Path

from collections import OrderedDict
from itertools import chain

from teuthology import misc as teuthology
from teuthology import contextutil
from teuthology.config import config as teuth_config
from teuthology.orchestra import run
from teuthology.packaging import install_package
from teuthology.packaging import remove_package
from teuthology.exceptions import ConfigError

log = logging.getLogger(__name__)

def get_kafka_dir(ctx):
    return '{tdir}/ceph/kafka-2.6.0-src'.format(tdir=teuthology.get_testdir(ctx))

def run_in_kafka_dir(ctx, client, args, **kwargs):
    return ctx.cluster.only(client).run(
        args=[ 'cd', get_kafka_dir(ctx), run.Raw('&&'), ] + args,
        **kwargs
    )

def get_toxvenv_dir(ctx):
    return ctx.tox.venv_path

def toxvenv_sh(ctx, remote, args, **kwargs):
    activate = get_toxvenv_dir(ctx) + '/bin/activate'
    return remote.sh(['source', activate, run.Raw('&&')] + args, **kwargs)

@contextlib.contextmanager
def download(ctx, config):
    """
    Download the bucket notification tests from the git builder.
    Remove downloaded test file upon exit.
    The context passed in should be identical to the context
    passed in to the main task.
    """
    assert isinstance(config, dict)
    log.info('Downloading bucket-notification-tests...')
    testdir = teuthology.get_testdir(ctx)
    for (client, client_config) in config.items():
        bntests_branch = client_config.get('force-branch', None)
        if not bntests_branch:
            raise ValueError(
                "Could not determine what branch to use for bn-tests. Please add 'force-branch: {bn-tests branch name}' to the .yaml config for this bucket notifications tests task.")

        log.info("Using branch '%s' for bucket notifications tests", bntests_branch)
        sha1 = client_config.get('sha1')
        git_remote = client_config.get('git_remote', teuth_config.ceph_git_base_url)
        ctx.cluster.only(client).run(
            args=[
                'git', 'clone',
                '-b', bntests_branch,
                git_remote + 'ceph.git',
                '{tdir}/ceph'.format(tdir=testdir),
                ],
            )
        if sha1 is not None:
            ctx.cluster.only(client).run(
                args=[
                    'cd', '{tdir}/ceph'.format(tdir=testdir),
                    run.Raw('&&'),
                    'git', 'reset', '--hard', sha1,
                    ],
                )
    try:
        yield
    finally:
        log.info('Removing bucket-notifications-tests...')
        testdir = teuthology.get_testdir(ctx)
        for client in config:
            ctx.cluster.only(client).run(
                args=[
                    'rm',
                    '-rf',
                    '{tdir}/ceph'.format(tdir=testdir),
                    ],
                )

@contextlib.contextmanager
def build(ctx, config):
    """
    Building ceph to get the build directory
    """
    assert isinstance(config, dict)
    log.info('Building ceph...')
    testdir = teuthology.get_testdir(ctx)
    for (client, _) in config.items():
        (remote,) = ctx.cluster.only(client).remotes.keys()
        ctx.cluster.only(client).run(args=['cd', '{tdir}/ceph'.format(tdir=testdir), run.Raw('&&'), './install-deps.sh'])
        ctx.cluster.only(client).run(args=['cd', '{tdir}/ceph'.format(tdir=testdir), run.Raw('&&'), './do_cmake.sh', '-DWITH_MGR_DASHBOARD_FRONTEND=OFF', '-DWITH_KRBD=OFF'])
        ctx.cluster.only(client).run(args=['cd', '{tdir}/ceph/build'.format(tdir=testdir), run.Raw('&&'), 'make', '-j', '4'])

    try:
        yield
    finally:
        pass

@contextlib.contextmanager
def install_kafka(ctx, config):
    """
    Downloading the kafka tar file.
    """
    assert isinstance(config, dict)
    log.info('Installing Kafka...')

    for (client, _) in config.items():
        (remote,) = ctx.cluster.only(client).remotes.keys()
        test_dir=teuthology.get_testdir(ctx)
        toxvenv_sh(ctx, remote, ['cd', '{tdir}/ceph'.format(tdir=test_dir), run.Raw('&&'), 'wget', 'https://mirrors.estointernet.in/apache/kafka/2.6.0/kafka-2.6.0-src.tgz'])
        toxvenv_sh(ctx, remote, ['cd', '{tdir}/ceph'.format(tdir=test_dir), run.Raw('&&'), 'tar', '-xvzf', 'kafka-2.6.0-src.tgz'])

    try:
        yield
    finally:
        log.info('Removing packaged dependencies of Kafka...')
        test_dir=get_kafka_dir(ctx)
        for client in config:
            ctx.cluster.only(client).run(
                args=['rm', '-rf', test_dir],
            )

@contextlib.contextmanager
def run_kafka(ctx,config):
    """
    This includes two parts:
    1. Starting Zookeeper service
    2. Starting Kafka service
    """
    assert isinstance(config, dict)
    log.info('Bringing up Zookeeper and Kafka services...')
    for (client,_) in config.items():
        (remote,) = ctx.cluster.only(client).remotes.keys()

        toxvenv_sh(ctx, remote,
            ['cd', '{tdir}'.format(tdir=get_kafka_dir(ctx)), run.Raw('&&'),
             './gradlew', 'jar', 
             '-PscalaVersion=2.13.2'
            ],
        )

        toxvenv_sh(ctx, remote,
            ['cd', '{tdir}/bin'.format(tdir=get_kafka_dir(ctx)), run.Raw('&&'),
             './zookeeper-server-start.sh',
             '{tir}/config/zookeeper.properties'.format(tir=get_kafka_dir(ctx)),
             run.Raw('&'), 'exit'
            ],
        )

        toxvenv_sh(ctx, remote,
            ['cd', '{tdir}/bin'.format(tdir=get_kafka_dir(ctx)), run.Raw('&&'),
             './kafka-server-start.sh',
             '{tir}/config/server.properties'.format(tir=get_kafka_dir(ctx)),
             run.Raw('&'), 'exit'
            ],
        )

    try:
        yield
    finally:
        log.info('Stopping Zookeeper and Kafka Services...')

        for (client, _) in config.items():
            (remote,) = ctx.cluster.only(client).remotes.keys()

            toxvenv_sh(ctx, remote, 
                ['cd', '{tdir}/bin'.format(tdir=get_kafka_dir(ctx)), run.Raw('&&'),
                 './kafka-server-stop.sh',  
                 '{tir}/config/kafka.properties'.format(tir=get_kafka_dir(ctx)),
                ]
            )

            toxvenv_sh(ctx, remote, 
                ['cd', '{tdir}/bin'.format(tdir=get_kafka_dir(ctx)), run.Raw('&&'), 
                 './zookeeper-server-stop.sh',
                 '{tir}/config/zookeeper.properties'.format(tir=get_kafka_dir(ctx)),
                ]
            )

@contextlib.contextmanager
def run_admin_cmds(ctx,config):
    """
    Running Keycloak Admin commands(kcadm commands) in order to get the token, aud value, thumbprint and realm name.
    """
    assert isinstance(config, dict)
    log.info('Checking kafka server through producer/consumer commands...')
    for (client,_) in config.items():
        (remote,) = ctx.cluster.only(client).remotes.keys()

        toxvenv_sh(ctx, remote,
            [
                'cd', '{tdir}/bin'.format(tdir=get_kafka_dir(ctx)), run.Raw('&&'), 
                './kafka-topics.sh', '--create', '--topic', 'quickstart-events',
                '--bootstrap-server', 'localhost:9092'
            ])

        toxvenv_sh(ctx, remote,
            [
                'cd', '{tdir}/bin'.format(tdir=get_kafka_dir(ctx)), run.Raw('&&'),
                'echo', "First", run.Raw('|'),
                './kafka-console-producer.sh', '--topic', 'quickstart-events',
                '--bootstrap-server', 'localhost:9092'
            ])

        toxvenv_sh(ctx, remote,
            [
                'cd', '{tdir}/bin'.format(tdir=get_kafka_dir(ctx)), run.Raw('&&'),
                './kafka-console-consumer.sh', '--topic', 'quickstart-events',
                '--from-beginning',
                '--bootstrap-server', 'localhost:9092',
                run.Raw('&'), 'exit'
            ])

    try:
        yield
    finally:
        pass

@contextlib.contextmanager
def configure(ctx, config):
    """
    Configure the bucket notifications tests for the kafka endpoint.
    This includes writing in the local configuration file.
    """
    assert isinstance(config, dict)
    log.info('Configuring bucket-notifications-tests...')
    testdir = teuthology.get_testdir(ctx)

    for client, properties in config['clients'].items():
        bntests_conf = config['bntests_conf'][client]
        bntests_conf['DEFAULT']['num_zonegroups'] = '1'
        bntests_conf['DEFAULT']['num_zones'] = '1'
        bntests_conf['DEFAULT']['num_ps_zones'] = '0'
        bntests_conf['DEFAULT']['gateways_per_zone'] = '1'
        bntests_conf['DEFAULT']['no_bootstrap'] = False
        bntests_conf['DEFAULT']['log_level'] = '5'

        (remote,) = ctx.cluster.only(client).remotes.keys()

        conf_fp = BytesIO()
        bntests_conf.write(conf_fp)

        remote.write_file(
            path='{tdir}/ceph/test_multi.{client}.conf'.format(tdir=testdir,client=client),
            data=conf_fp.getvalue(),
            )

    try:
        yield
    finally:
        log.info('Removing test_multi.conf file...')
        for client, properties in config['clients'].items():
            (remote,) = ctx.cluster.only(client).remotes.keys()
            remote.run(
                 args=['rm', '-f',
                       '{tdir}/ceph/test_multi.{client}.conf'.format(tdir=testdir,client=client),
                 ],
                 )

@contextlib.contextmanager
def configures(ctx, config):
    assert isinstance(config, dict)
    log.info('Configuring bucket-notifications-tests...')
    testdir = teuthology.get_testdir(ctx)
    for client in config['clients']:
        (remote,) = ctx.cluster.only(client).remotes.keys()
        remote.run(
            args=[
                'cd', 
                '{tdir}/ceph/src/test/rgw/bucket_notification'.format(tdir=testdir),
                run.Raw('&&'),
                './bootstrap',
                ],
            )


@contextlib.contextmanager
def run_tests(ctx, config):
    """
    Run the bucket notifications tests after everything is set up.
    :param ctx: Context passed to task
    :param config: specific configuration information
    """
    assert isinstance(config, dict)
    log.info('Running bucket-notifications-tests...')
    testdir = teuthology.get_testdir(ctx)
    for client, client_config in config.items():
        (remote,) = ctx.cluster.only(client).remotes.keys()

        '''
        args = [
            'KAFKA_DIR={dir}'.format(dir=get_kafka_dir(ctx)),
            ]

        args += [
            'RGW_MULTI_TEST_CONF={tdir}/ceph/test_multi.{client}.conf'.format(tdir=testdir,client=client)
            ]

        args += [
            'python3', '-m', 'nose',
            '-w',
            '{tdir}/ceph/src/test/rgw/rgw_multi'.format(tdir=testdir),
            '-m',
            'test_ps_*'
            ]
        '''

        args = [
            '{tdir}/ceph/src/test/rgw/bucket_notification/virtualenv/bin/python'.format(tdir=testdir),
            '-m', 'nose',
            '-s',
            '{tdir}/ceph/src/test/rgw/bucket_notification/test.py'
            ]

        remote.run(
            args=args,
            label="bucket notification tests against kafka server"
            )
    yield

@contextlib.contextmanager
def task(ctx,config):
    """
    To run keycloak the prerequisite is to run the tox task. Following is the way how to run
    tox and then keycloak::

    tasks:
    - tox: [ client.0 ]
    - kafka:
        client.0:

    """
    assert config is None or isinstance(config, list) \
        or isinstance(config, dict), \
        "task kafka only supports a list or dictionary for configuration"

    if not hasattr(ctx, 'tox'):
        raise ConfigError('kafka must run after the tox task')

    all_clients = ['client.{id}'.format(id=id_)
                   for id_ in teuthology.all_roles_of_type(ctx.cluster, 'client')]
    if config is None:
        config = all_clients
    if isinstance(config, list):
        config = dict.fromkeys(config)
    clients=config.keys()

    overrides = ctx.config.get('overrides', {})
    # merge each client section, not the top level.
    for client in config.keys():
        if not config[client]:
            config[client] = {}
        teuthology.deep_merge(config[client], overrides.get('kafka', {}))

    log.debug('Kafka config is %s', config)

    '''
    bntests_conf = {}
    for client in clients:
        bntests_conf[client] = ConfigObj(
            indent_type='',
            infile={
            'DEFAULT':
                {
                'num_zonegroups'  : {},
                'num_zones'  : {},
                'num_ps_zones'  : {},
                'gateways_per_zone'  : {},
                'no_bootstrap'  : {},
                'log_level'  : {},
                },
            }
            )
    '''

    with contextutil.nested(
        lambda: download(ctx=ctx, config=config),
        #lambda: build(ctx=ctx, config=config),
        lambda: install_kafka(ctx=ctx, config=config),
        lambda: run_kafka(ctx=ctx, config=config),
        #lambda: configure(ctx=ctx, config=dict(clients=config,bntests_conf=bntests_conf,)),
        lambda: configures(ctx=ctx, config=dict(clients=clients)),
        lambda: run_tests(ctx=ctx, config=config),
        #lambda: run_admin_cmds(ctx=ctx, config=config),
        ):
        yield
