#!/usr/bin/python3
"""
Copyright (C) 2023 Dirk Müller, SUSE LLC

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

SPDX-License-Identifier: GPL-2.0-or-later
"""

import argparse
import collections
import configparser
import json
import logging as LOG
import os
import random
import re
import time
from dataclasses import dataclass

import pika
import requests
from pika.adapters.blocking_connection import BlockingChannel

CONF = configparser.ConfigParser(strict=False)
OPENQA_GROUPS_FILTER: tuple[int] = (538, 475, 443, 442, 428)


def post_failure_notification_to_slack(status, body, link_to_failure) -> None:
    """Post a message to slack with the given parameters by using a webhook."""
    LOG.debug(
        f'post_failure_notification_to_slack({status}, {body}, {link_to_failure})'
    )

    resp = requests.post(
        url=CONF['DEFAULT']['slack_trigger_url'],
        headers={'Content-Type': 'application/json'},
        json={'status': status, 'body': body, 'link_to_failure': link_to_failure},
    )
    resp.raise_for_status()


@dataclass
class openQAJob:
    """Track the state of a openQA job identified by id"""

    id: int
    build: str
    result: str


def listen_amqp_events():
    """pubsub subscribe to events posted on the AMPQ channel."""
    channel: BlockingChannel = pika.BlockingConnection(
        pika.URLParameters(CONF['DEFAULT']['listen_url'])
    ).channel()
    channel.exchange_declare(
        exchange='pubsub', exchange_type='topic', passive=True, durable=False
    )
    queue_name = channel.queue_declare('', exclusive=True).method.queue
    channel.queue_bind(exchange='pubsub', queue=queue_name, routing_key='#')

    print(' [*] Waiting for events. To exit press CTRL+C')

    openqa_jobs = collections.defaultdict(list)
    project_re = re.compile(CONF['obs']['project_re'])

    def callback(_, method, _unused, body) -> None:
        """Find failed jobs without pending jobs and then post a message to slack."""
        if method.routing_key.startswith('suse.openqa.job'):
            handle_openqa_event(method, body)
        elif method.routing_key.startswith('suse.obs.package'):
            handle_obs_package_event(method, body)
        elif not method.routing_key.startswith(
            'suse.obs.metrics'
        ) and 'Containers' in str(body):
            LOG.info(f' [x] {method.routing_key!r}:{body!r}')

    def handle_openqa_event(method, body):
        msg = json.loads(body)
        build_id: str = msg.get('BUILD')

        if msg.get('group_id') in OPENQA_GROUPS_FILTER:
            LOG.info(f' [x] {method.routing_key!r}:{msg!r}')
            if 'suse.openqa.job.create' in method.routing_key:
                if msg.get('id'):
                    openqa_jobs[build_id].append(
                        openQAJob(id=msg['id'], build=build_id, result='pending')
                    )
                    LOG.info(f"Job {build_id}/{msg['id']} created (pending)")
            elif 'suse.openqa.job.done' in method.routing_key:
                if build_id not in openqa_jobs:
                    # we might have been just restarted, insert it
                    openqa_jobs[build_id].append(
                        openQAJob(id=msg['id'], build=build_id, result='pending')
                    )
                if build_id in openqa_jobs:
                    for job in openqa_jobs[build_id]:
                        if job.id == msg['id']:
                            job.result = msg['result']

                    # Find for any failures
                    results = collections.Counter(
                        j.result for j in openqa_jobs[build_id]
                    )
                    LOG.info(f'Job ended - results: {results}')
                    if not results.get('pending') and results.get('failed'):
                        body: str = (
                            f"Build {build_id} has {results['failed']} failed tests."
                        )
                        post_failure_notification_to_slack(
                            ':openqa:',
                            body,
                            f"{CONF['openqa']['host']}tests/overview?build={build_id}&groupid={msg['group_id']}",
                        )

                    if not results.get('pending'):
                        # Clear the build from pending jobs
                        del openqa_jobs[build_id]

    def handle_obs_package_event(method, body):
        msg = json.loads(body)

        if (
            not project_re.match(msg.get('project', ''))
            or msg.get('previouslyfailed') == '1'
        ):
            return

        if 'suse.obs.package.build_fail' in method.routing_key:
            LOG.info(
                f"obs build fail {msg['project']}/{msg['package']}/{msg['repository']}/{msg['arch']}"
            )
            post_failure_notification_to_slack(
                ':obs:',
                f"{msg['project']}/{msg['package']}/{msg['repository']}/{msg['arch']} failed to build.",
                f"{CONF['obs']['host']}{msg['project']}/{msg['package']}/{msg['repository']}/{msg['arch']}",
            )

    channel.basic_consume(queue_name, callback, auto_ack=True)
    channel.start_consuming()


def main():
    parse = argparse.ArgumentParser(
        description='Bot to forward BCI openQA test failures to Slack'
    )
    parse.add_argument('-d', '--debug', action='store_true')

    args = parse.parse_args()
    LOG.basicConfig(level=LOG.DEBUG if args.debug else LOG.INFO)

    with open(os.path.expanduser('~/.config/slacky'), encoding='utf8') as f:
        CONF.read_file(f)

    while True:
        try:
            listen_amqp_events()
        except (pika.exceptions.ConnectionClosed, pika.exceptions.AMQPHeartbeatTimeout):
            time.sleep(random.randint(10, 100))


if __name__ == '__main__':
    main()
