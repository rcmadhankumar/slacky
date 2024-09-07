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

import re
from datetime import datetime
from unittest.mock import patch

import slacky

testing_CONF = {}
testing_CONF['DEFAULT'] = {}
testing_CONF['obs'] = {'host': 'localhost'}


@patch('slacky.post_failure_notification_to_slack', return_value=None)
def test_pending_bs_requests_grouping(mock_post_failure_notification):
    bot = slacky.Slacky()
    slacky.CONF = testing_CONF

    bot.bs_requests = {
        1: slacky.bs_Request(
            id=1,
            targetproject='project1',
            targetpackage='package1',
            created_at=datetime(2023, 1, 2),
        ),
        2: slacky.bs_Request(
            id=2,
            targetproject='project1',
            targetpackage='package2',
            created_at=datetime(2023, 1, 3),
        ),
    }

    bot.last_interval_check = datetime(2023, 1, 1)
    bot.check_pending_requests()
    mock_post_failure_notification.assert_called_once_with(
        ':request-changes:',
        '2 open requests to project1 / package1, package2 ',
        'localhost/project/requests/project1',
    )
    for _, req in bot.bs_requests.items():
        assert req.is_announced


@patch('slacky.post_failure_notification_to_slack', return_value=None)
def test_pending_bs_requests_single(mock_post_failure_notification):
    bot = slacky.Slacky()
    slacky.CONF = testing_CONF

    bot.bs_requests = {
        1: slacky.bs_Request(
            id=1,
            targetproject='project1',
            targetpackage='package1',
            created_at=datetime(2023, 1, 2),
        )
    }

    bot.last_interval_check = datetime(2023, 1, 1)
    bot.check_pending_requests()
    mock_post_failure_notification.assert_called_once_with(
        ':request-changes:',
        'Request to project1 / package1 is still open ',
        'localhost/project/requests/project1',
    )


@patch('slacky.post_failure_notification_to_slack', return_value=None)
def test_declined_bs_requests_single(mock_post_failure_notification):
    bot = slacky.Slacky()
    slacky.CONF = testing_CONF

    body = '{"number": 1, "state": "new", "actions": [{"type": "submit", "targetproject": "SUSE:SLE-15-SP6:Update:BCI", "targetpackage": "test"}]}'
    bot.handle_obs_request_event('suse.obs.request.create', body)
    mock_post_failure_notification.assert_called_with(
        ':announcement:',
        'SUSE:SLE-15-SP6:Update:BCI / test: New request ',
        'localhost/request/show/1',
    )

    body = '{"number": 1, "state": "declined"}'
    bot.handle_obs_request_event('suse.obs.request.state_change', body)
    mock_post_failure_notification.assert_called_with(
        ':request-changes:',
        'Request to SUSE:SLE-15-SP6:Update:BCI / test got declined.',
        'localhost/request/show/1',
    )


@patch('slacky.post_failure_notification_to_slack', return_value=None)
def test_obs_repo_publish(mock_post_failure_notification):
    bot = slacky.Slacky()
    bot.repo_re = re.compile(r'^SUSE:Containers:SLE-SERVER:')

    slacky.CONF = testing_CONF

    with patch('slacky.datetime') as mock_datetime:
        mock_datetime.now.return_value = datetime(2023, 1, 2)
        body = '{"state": "publishing", "project": "SUSE:Containers:SLE-SERVER:15", "repo": "containers"}'
        bot.handle_obs_repo_event('suse.obs.repo', body)
    assert len(bot.repo_publishes.keys()) == 1
    bot.check_pending_requests()
    mock_post_failure_notification.assert_called_with(
        ':published:',
        'SUSE:Containers:SLE-SERVER:15 / containers is not published after a while!',
        'localhost/repositories/SUSE:Containers:SLE-SERVER:15/containers',
    )
