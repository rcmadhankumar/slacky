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
