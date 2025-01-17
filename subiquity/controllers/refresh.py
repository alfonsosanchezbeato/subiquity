# Copyright 2019 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import enum
import logging
import os
import time

import requests.exceptions

from subiquitycore.controller import BaseController
from subiquitycore.core import Skip

log = logging.getLogger('subiquity.controllers.refresh')


class CheckState(enum.IntEnum):
    NOT_STARTED = enum.auto()
    CHECKING = enum.auto()
    FAILED = enum.auto()

    AVAILABLE = enum.auto()
    UNAVAILABLE = enum.auto()

    def is_definite(self):
        return self in [self.AVAILABLE, self.UNAVAILABLE]


class SwitchState(enum.IntEnum):
    NOT_STARTED = enum.auto()
    SWITCHING = enum.auto()
    SWITCHED = enum.auto()


class RefreshController(BaseController):

    signals = [
        ('snapd-network-change', 'snapd_network_changed'),
    ]

    def __init__(self, app):
        super().__init__(app)
        self.snap_name = os.environ.get("SNAP_NAME", "subiquity")
        self.check_state = CheckState.NOT_STARTED
        self.switch_state = SwitchState.NOT_STARTED
        self.network_state = "down"

        self.current_snap_version = "unknown"
        self.new_snap_version = ""

        self.offered_first_time = False

    def start(self):
        self.switch_state = SwitchState.SWITCHING
        self.run_in_bg(self._bg_get_snap_details, self._got_snap_details)

    def _bg_get_snap_details(self):
        return self.app.snapd_connection.get(
            'v2/snaps/{snap_name}'.format(snap_name=self.snap_name))

    def _got_snap_details(self, fut):
        try:
            response = fut.result()
            response.raise_for_status()
        except requests.exceptions.RequestException:
            log.exception("_got_snap_details")
        else:
            r = response.json()
            self.current_snap_version = r['result']['version']
            log.debug(
                "current version of snap is: %r",
                self.current_snap_version)
        channel = self.get_refresh_channel()
        self.run_in_bg(
            lambda: self._bg_switch_snap(channel),
            self._snap_switched)

    def get_refresh_channel(self):
        """Return the channel we should refresh subiquity to."""
        if 'channel' in self.answers:
            return self.answers['channel']
        with open('/proc/cmdline') as fp:
            cmdline = fp.read()
        prefix = "subiquity-channel="
        for arg in cmdline.split():
            if arg.startswith(prefix):
                log.debug(
                    "get_refresh_channel: found %s on kernel cmdline", arg)
                return arg[len(prefix):]

        info_file = '/cdrom/.disk/info'
        try:
            fp = open(info_file)
        except FileNotFoundError:
            if self.opts.dry_run:
                info = (
                    'Ubuntu-Server 18.04.2 LTS "Bionic Beaver" - '
                    'Release amd64 (20190214.3)')
            else:
                log.debug(
                    "get_refresh_channel: failed to find .disk/info file")
                return
        else:
            with fp:
                info = fp.read()
        release = info.split()[1]
        return 'stable/ubuntu-' + release

    def _bg_switch_snap(self, channel):
        log.debug("switching %s to %s", self.snap_name, channel)
        try:
            response = self.app.snapd_connection.post(
                'v2/snaps/{}'.format(self.snap_name),
                {'action': 'switch', 'channel': channel})
            response.raise_for_status()
        except requests.exceptions.RequestException:
            log.exception("switching channels")
            return
        change = response.json()["change"]
        while True:
            try:
                response = self.app.snapd_connection.get(
                    'v2/changes/{}'.format(change))
                response.raise_for_status()
            except requests.exceptions.RequestException:
                log.exception("checking switch")
                return
            if response.json()["result"]["status"] == "Done":
                return True
            time.sleep(0.1)

    def _snap_switched(self, fut):
        log.debug("snap switching completed")
        try:
            success = fut.result()
        except Exception:
            log.exception("_snap_switched")
            return
        if not success:
            return
        self.switch_state = SwitchState.SWITCHED
        self._maybe_check_for_update()

    def snapd_network_changed(self):
        self.network_state = "up"
        self._maybe_check_for_update()

    def _maybe_check_for_update(self):
        # If we have not yet switched to the right channel, wait.
        if self.switch_state != SwitchState.SWITCHED:
            return
        # If the network is not yet up, wait.
        if self.network_state == "down":
            return
        # If we restarted into this version, don't check for a new version.
        if self.app.updated:
            return
        # If we got an answer, don't check again.
        if self.check_state.is_definite():
            return
        self.check_state = CheckState.CHECKING
        self.run_in_bg(self._bg_check_for_update, self._check_result)

    def _bg_check_for_update(self):
        return self.app.snapd_connection.get('v2/find', select='refresh')

    def _check_result(self, fut):
        # If we managed to send concurrent requests and one has
        # already provided an answer, just forget all about the other
        # one!
        if self.check_state.is_definite():
            return
        try:
            response = fut.result()
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            log.exception("checking for update")
            self.check_error = e
            self.check_state = CheckState.FAILED
        else:
            result = response.json()
            log.debug("_check_result %s", result)
            for snap in result["result"]:
                if snap["name"] == self.snap_name:
                    self.check_state = CheckState.AVAILABLE
                    self.new_snap_version = snap["version"]
                    log.debug(
                        "new version of snap available: %r",
                        self.new_snap_version)
                    break
            else:
                self.check_state = CheckState.UNAVAILABLE
        if self.showing:
            self.ui.body.update_check_state()

    def start_update(self, callback):
        update_marker = os.path.join(self.app.state_dir, 'updating')
        open(update_marker, 'w').close()
        self.run_in_bg(
            self._bg_start_update,
            lambda fut: self.update_started(fut, callback))

    def _bg_start_update(self):
        return self.app.snapd_connection.post(
            'v2/snaps/{}'.format(self.snap_name), {'action': 'refresh'})

    def update_started(self, fut, callback):
        try:
            response = fut.result()
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            log.exception("requesting update")
            self.update_state = CheckState.FAILED
            self.update_failure = e
            return
        result = response.json()
        log.debug("refresh requested: %s", result)
        callback(result['change'])

    def get_progress(self, change, callback):
        self.run_in_bg(
            lambda: self._bg_get_progress(change),
            lambda fut: self.got_progress(fut, callback))

    def _bg_get_progress(self, change):
        return self.app.snapd_connection.get('v2/changes/{}'.format(change))

    def got_progress(self, fut, callback):
        try:
            response = fut.result()
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            log.exception("checking for progress")
            self.update_state = CheckState.FAILED
            self.update_failure = e
            return
        result = response.json()
        callback(result['result'])

    def start_ui(self, index=1):
        from subiquity.ui.views.refresh import RefreshView
        if self.app.updated:
            raise Skip()
        show = False
        if index == 1:
            if self.check_state == CheckState.AVAILABLE:
                show = True
                self.offered_first_time = True
        elif index == 2:
            if not self.offered_first_time:
                if self.check_state in [CheckState.AVAILABLE,
                                        CheckState.CHECKING]:
                    show = True
        else:
            raise AssertionError("unexpected index {}".format(index))
        if show:
            self.ui.set_body(RefreshView(self))
            if 'update' in self.answers:
                if self.answers['update']:
                    self.ui.body.update()
                else:
                    self.done()
        else:
            raise Skip()

    def done(self, sender=None):
        log.debug("RefreshController.done next-screen")
        self.signal.emit_signal('next-screen')

    def cancel(self, sender=None):
        self.signal.emit_signal('prev-screen')
