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
import pickle
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pika
import requests
from pika.adapters.blocking_connection import BlockingChannel

CONF = configparser.ConfigParser(strict=False)
OPENQA_GROUPS_FILTER: tuple[int] = (586, 582, 538, 475, 453, 445, 444, 443, 442, 428)

HANGING_REQUESTS_SEC = 12 * 60 * 60
HANGING_REPO_PUBLISH_SEC = 90 * 60
HANGING_CONTAINER_TAG_SEC = 10 * 24 * 60 * 60
OPENQA_FAIL_WAIT = 15 * 60


def post_failure_notification_to_slack(status, body, link_to_failure) -> None:
    """Post a message to slack with the given parameters by using a webhook."""
    LOG.debug(
        f'post_failure_notification_to_slack({status}, {body}, {link_to_failure})'
    )

    if not CONF['DEFAULT'].get('slack_trigger_url'):
        LOG.debug('Slack notifications are disabled')
        return

    resp = requests.post(
        url=CONF['DEFAULT']['slack_trigger_url'],
        headers={'Content-Type': 'application/json'},
        json={'status': status, 'body': body, 'link_to_failure': link_to_failure},
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as err:
        LOG.error(f'Failed to post failure notification to slack: {err}')


@dataclass
class openQAJob:
    """Track the state of a openQA job identified by id"""

    test_id: str
    build: str
    result: str
    finished_at: datetime | None = None


@dataclass
class bs_Request:
    """Track build service requests identified by id"""

    id: int
    targetproject: str
    targetpackage: str
    created_at: datetime
    is_announced: bool = False
    is_create_announced: bool = False


@dataclass
class repo_publish:
    """Track repository publishing"""

    project: str
    repository: str
    state: str
    state_changed: datetime
    is_announced: bool = False


class Slacky:
    # when adding more state, please update load_state()
    openqa_jobs = collections.defaultdict(list)
    bs_requests = collections.defaultdict(None)
    repo_publishes: dict = {}
    container_publishes: dict = {}
    last_interval_check: datetime = datetime.now()

    def handle_openqa_event(self, routing_key, body):
        """Find failed jobs without pending jobs and then post a message to slack."""
        msg = json.loads(body)
        if msg.get('group_id') not in OPENQA_GROUPS_FILTER:
            return

        build_id: str = msg.get('BUILD')
        qajob: tuple[int, str] = (msg['group_id'], build_id)
        test_id: str = f"{msg.get('TEST')}/{msg.get('ARCH')}"

        def find_test_id(job):
            return job.test_id == test_id

        LOG.debug(f' [x] {routing_key!r}:{msg!r}')
        if 'suse.openqa.job.create' in routing_key:
            self.openqa_jobs[qajob].append(
                openQAJob(test_id=test_id, build=build_id, result='pending')
            )
            LOG.info(f'Job {qajob}/{test_id} created (pending)')
        if 'suse.openqa.job.restart' in routing_key:
            for job in filter(find_test_id, self.openqa_jobs[qajob]):
                job.result = 'pending'
                job.finished_at = None
                break
            else:
                self.openqa_jobs[qajob].append(
                    openQAJob(test_id=test_id, build=build_id, result='pending')
                )
            LOG.info(f'Job {qajob}/{test_id} restarted and stored as (pending)')
        elif 'suse.openqa.job.done' in routing_key:
            for job in filter(find_test_id, self.openqa_jobs[qajob]):
                if msg.get('reason') is not None:
                    LOG.info(f'Job {qajob}/{test_id} is going to restart')
                    continue
                job.result = msg['result']
                job.finished_at = datetime.now()

    def handle_obs_package_event(self, routing_key, body):
        """Post any build failures for the configured projects to slack."""
        msg = json.loads(body)

        if (
            not self.project_re.match(msg.get('project', ''))
            or msg.get('previouslyfailed') == '1'
        ):
            return

        if 'suse.obs.package.build_fail' in routing_key:
            LOG.info(
                f"obs build fail {msg['project']}/{msg['package']}/{msg['repository']}/{msg['arch']}"
            )
            post_failure_notification_to_slack(
                ':obs:',
                f"{msg['project']}/{msg['package']}/{msg['repository']}/{msg['arch']} failed to build.",
                urllib.parse.urljoin(
                    CONF['obs']['host'],
                    f"/package/live_build_log/{msg['project']}/{msg['package']}/{msg['repository']}/{msg['arch']}",
                ),
            )

    def handle_obs_repo_event(self, routing_key, body):
        """Post any build failures for the configured projects to slack."""
        msg = json.loads(body)

        if not self.repo_re.match(msg.get('project')) or not msg.get('state'):
            return

        prjrepo = f"{msg['project']}/{msg['repo']}"
        LOG.info(f'repo event for {prjrepo}: {msg}')
        if msg['state'] == 'published':
            if prjrepo in self.repo_publishes:
                del self.repo_publishes[prjrepo]
            return

        self.repo_publishes[prjrepo] = repo_publish(
            project=msg['project'],
            repository=msg['repo'],
            state=msg['state'],
            state_changed=datetime.now(),
        )

    def handle_obs_request_event(self, routing_key, body):
        """Warn when requests get declined, track them for hang detection."""
        msg = json.loads(body)

        if 'suse.obs.request.create' in routing_key:
            for action in msg['actions']:
                if action['type'] == 'submit' and 'BCI' in action['targetproject']:
                    LOG.info(
                        f"found new submitrequest against {action['targetproject']}: id {msg['number']}"
                    )
                    bs_request = bs_Request(
                        id=msg['number'],
                        targetproject=action['targetproject'],
                        targetpackage=action['targetpackage'],
                        created_at=datetime.now(),
                    )
                    self.bs_requests[msg['number']] = bs_request

        if 'suse.obs.request.state_change' in routing_key:
            bs_request = self.bs_requests.get(msg['number'])
            if bs_request:
                bs_request.state = msg['state']
                if msg['state'] in ('declined',):
                    post_failure_notification_to_slack(
                        ':request-changes:',
                        f'Request to {bs_request.targetproject} / {bs_request.targetpackage} got declined.',
                        urllib.parse.urljoin(
                            CONF['obs']['host'], f'/request/show/{bs_request.id}'
                        ),
                    )
                    bs_request.is_announced = True
                    bs_request.is_create_announced = True
                if msg['state'] in ('accepted', 'revoked', 'superseded'):
                    LOG.info(f"request {msg['number']} entered final state.")
                    del self.bs_requests[msg['number']]

    def handle_container_event(self, routing_key, body):
        """Warn when a :latest tag didn't get published a long while."""
        msg = json.loads(body)

        if 'suse.obs.container.published' in routing_key:
            if not msg.get('container') or not self.repo_re.match(
                msg.get('project', '')
            ):
                return

            repository, _, tag = msg['container'].partition(':')
            tag_version = tag.rpartition('-')[0] if '-' in tag else tag
            if tag_version.count('.') >= 2:
                return
            if 'registry.suse.com' not in repository:
                return

            repo_tag: str = f'{repository.partition("/")[2]}:{tag_version}'
            LOG.info(f'Container {repo_tag} published.')
            self.container_publishes[repo_tag] = datetime.now()

    def check_pending_requests(self):
        """Announce for things that are hanging around"""

        # Announce request that are open for a long time
        for prj, reqcount in collections.Counter(
            (
                req.targetproject
                for req in self.bs_requests.values()
                if (
                    not req.is_announced
                    and (datetime.now() - req.created_at).total_seconds()
                    > HANGING_REQUESTS_SEC
                )
            )
        ).most_common():
            pkgs = set()
            for req in self.bs_requests.values():
                if req.targetproject == prj and not req.is_announced:
                    pkgs.add(req.targetpackage)
                    req.is_announced = True
                    req.is_create_announced = True
            post_failure_notification_to_slack(
                ':request-changes:',
                f'{reqcount} hanging requests to {prj} / {", ".join(sorted(pkgs))} '
                if reqcount > 1
                else f'Request to {prj} / {", ".join(pkgs)} is still open ',
                urllib.parse.urljoin(CONF['obs']['host'], f'/project/requests/{prj}'),
            )

        # Announce requests that have been recently created
        for prj, reqcount in collections.Counter(
            (
                req.targetproject
                for req in self.bs_requests.values()
                if not req.is_create_announced
            )
        ).most_common():
            newest_request_age: int = HANGING_REQUESTS_SEC
            for req in self.bs_requests.values():
                if req.targetproject == prj and not req.is_create_announced:
                    if (
                        datetime.now() - req.created_at
                    ).total_seconds() < newest_request_age:
                        newest_request_age = (
                            datetime.now() - req.created_at
                        ).total_seconds()
            # If we haven't seen a new request in a while, time to announce
            if 60 < newest_request_age < HANGING_REQUESTS_SEC:
                pkgs = set()
                for req in self.bs_requests.values():
                    if req.targetproject == prj and not req.is_create_announced:
                        pkgs.add(req.targetpackage)
                        req.is_create_announced = True
                post_failure_notification_to_slack(
                    ':announcement:',
                    f'{reqcount} open requests to {prj} / {", ".join(sorted(pkgs))} for review. '
                    if reqcount > 1
                    else f'New request to {prj} / {", ".join(pkgs)} available for review. ',
                    urllib.parse.urljoin(
                        CONF['obs']['host'], f'/project/requests/{prj}'
                    ),
                )

        # Announce hanging repo publishes
        for repo in self.repo_publishes.values():
            if (
                not repo.is_announced
                and (datetime.now() - repo.state_changed).total_seconds()
                > HANGING_REPO_PUBLISH_SEC
            ):
                post_failure_notification_to_slack(
                    ':published:',
                    f'{repo.project} / {repo.repository} is not published after a while!',
                    urllib.parse.urljoin(
                        CONF['obs']['host'],
                        f'/project/repository_state/{repo.project}/{repo.repository}',
                    ),
                )
                repo.is_announced = True

        # Announce container tags that have not been published for a while
        hanging_containers = sorted(
            [
                c
                for c, publishdate in self.container_publishes.items()
                if (
                    (datetime.now() - publishdate).total_seconds()
                    > HANGING_CONTAINER_TAG_SEC
                )
            ]
        )
        if hanging_containers:
            post_failure_notification_to_slack(
                ':question:',
                f'These tags were not published for a while: {",".join(hanging_containers)}',
                '',
            )
            for container in hanging_containers:
                self.container_publishes.pop(container)

        # Announce any openqa runs that have failures even after a while
        builds_to_delete = []
        for (group_id, build_id), build_results in self.openqa_jobs.items():
            results = collections.Counter(j.result for j in build_results)
            result_times = sorted(
                [
                    j.finished_at
                    for j in filter(lambda x: x.finished_at is not None, build_results)
                ],
                reverse=True,
            )
            if (
                len(result_times)
                and result_times[0]
                and (datetime.now() - result_times[0]).total_seconds()
                > OPENQA_FAIL_WAIT
            ):
                LOG.info(f'Job {build_id} ended - results: {results}')
                if not results.get('pending') and results.get('failed'):
                    body: str = (
                        f"Build {build_id} has {results['failed']} failed tests."
                    )
                    post_failure_notification_to_slack(
                        ':openqa:',
                        body,
                        urllib.parse.urljoin(
                            CONF['openqa']['host'],
                            f'/tests/overview?build={build_id}&groupid={group_id}',
                        ),
                    )
                if not results.get('pending'):
                    builds_to_delete.append((group_id, build_id))
        for group_id, build_id in builds_to_delete:
            del self.openqa_jobs[(group_id, build_id)]

    def load_state(self) -> None:
        """Restore persisted from a previously launched slacky"""
        state_file = Path(__file__).resolve().parent / 'state.pickle'
        if state_file.is_file():
            with open(Path(__file__).resolve().parent / 'state.pickle', 'rb') as f:
                data = pickle.load(f)
                # copy over the state from a previous launched slacky
                self.openqa_jobs = data.openqa_jobs
                LOG.info(f'Loaded state(openqa_jobs = {self.openqa_jobs})')
                self.bs_requests = data.bs_requests
                LOG.info(f'Loaded state(bs_requests = {self.bs_requests})')
                self.repo_publishes = data.repo_publishes
                LOG.info(f'Loaded state(repo_publish = {self.repo_publishes})')
                self.container_publishes = data.container_publishes
                LOG.info(
                    f'Loaded state(container_publishes = {self.container_publishes})'
                )

    def save_state(self) -> None:
        """pickle the slacky state for future instance preservation"""
        with open(Path(__file__).resolve().parent / 'state.pickle', 'wb') as f:
            pickle.dump(self, f)
            LOG.info('Saved state to state.pickle')

    def run(self):
        """pubsub subscribe to events posted on the AMPQ channel."""
        channel: BlockingChannel = pika.BlockingConnection(
            pika.URLParameters(CONF['DEFAULT']['listen_url'])
        ).channel()
        channel.exchange_declare(
            exchange='pubsub', exchange_type='topic', passive=True, durable=False
        )
        queue_name = channel.queue_declare('', exclusive=True).method.queue
        channel.queue_bind(exchange='pubsub', queue=queue_name, routing_key='#')

        self.load_state()
        self.project_re = re.compile(CONF['obs']['project_re'])
        self.repo_re = re.compile(CONF['obs']['repo_re'])

        def callback(_, method, _unused, body) -> None:
            """Generic dispatcher for events posted on the AMPQ channel."""

            if (datetime.now() - self.last_interval_check).total_seconds() > 120:
                self.check_pending_requests()
                self.last_interval_check = datetime.now()

            routing_key = method.routing_key
            if routing_key.startswith('suse.openqa'):
                self.handle_openqa_event(routing_key, body)
            elif routing_key.startswith('suse.obs.package'):
                self.handle_obs_package_event(routing_key, body)
            elif routing_key.startswith('suse.obs.request'):
                self.handle_obs_request_event(routing_key, body)
            elif routing_key.startswith('suse.obs.repo'):
                self.handle_obs_repo_event(routing_key, body)
            elif routing_key.startswith('suse.obs.container'):
                self.handle_container_event(routing_key, body)

        channel.basic_consume(queue_name, callback, auto_ack=True)
        try:
            print(' [*] Waiting for events. To exit press CTRL+C')
            channel.start_consuming()
        except KeyboardInterrupt:
            channel.stop_consuming()
            self.save_state()
            LOG.info('State saved!')
            sys.exit(0)


def main():
    parse = argparse.ArgumentParser(
        description='Bot to forward BCI pipeline failures to Slack'
    )
    parse.add_argument('-d', '--debug', action='store_true')

    args = parse.parse_args()
    LOG.basicConfig(
        level=LOG.DEBUG if args.debug else LOG.INFO,
        datefmt='%y-%m-%d %H:%M:%S',
        format='%(asctime)s %(message)s',
    )
    LOG.getLogger('pika').setLevel(LOG.ERROR)

    with open(os.path.expanduser('~/.config/slacky'), encoding='utf8') as f:
        CONF.read_file(f)

    while True:
        slacky = Slacky()
        try:
            slacky.run()
        except (pika.exceptions.ConnectionClosed, pika.exceptions.AMQPHeartbeatTimeout):
            time.sleep(random.randint(10, 100))


if __name__ == '__main__':
    main()
