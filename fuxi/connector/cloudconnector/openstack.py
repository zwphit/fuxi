# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import os
import time

from cinderclient import exceptions as cinder_exception
from novaclient import exceptions as nova_exception
from oslo_concurrency import lockutils
from oslo_concurrency import processutils
from oslo_log import log as logging

from fuxi.common import blockdevice
from fuxi.common import config
from fuxi.common import constants as consts
from fuxi.common import state_monitor
from fuxi.connector import connector
from fuxi import exceptions
from fuxi.i18n import _
from fuxi import utils

CONF = config.CONF

LOG = logging.getLogger(__name__)


class CinderConnector(connector.Connector):
    def __init__(self):
        super(CinderConnector, self).__init__()
        self.cinderclient = utils.get_cinderclient()
        self.novaclient = utils.get_novaclient()

    @lockutils.synchronized('openstack-attach-volume')
    def connect_volume(self, volume, **connect_opts):
        bdm = blockdevice.BlockerDeviceManager()
        ori_devices = bdm.device_scan()

        # Do volume-attach
        try:
            server_id = connect_opts.get('server_id', None)
            if not server_id:
                server_id = utils.get_instance_uuid()

            LOG.info("Start to connect to volume %s", volume)
            nova_volume = self.novaclient.volumes.create_server_volume(
                server_id=server_id,
                volume_id=volume.id,
                device=None)

            volume_monitor = state_monitor.StateMonitor(
                self.cinderclient,
                nova_volume,
                'in-use',
                ('available', 'attaching',))
            attached_volume = volume_monitor.monitor_cinder_volume()
        except nova_exception.ClientException as ex:
            LOG.error("Attaching volume %(vol)s to server %(s)s "
                      "failed. Error: %(err)s",
                      {'vol': volume.id, 's': server_id, 'err': ex})
            raise

        # Get all devices on host after do volume-attach,
        # and then find attached device.
        LOG.info("After connected to volume, scan the added "
                 "block device on host")
        curr_devices = bdm.device_scan()
        start_time = time.time()
        delta_devices = list(set(curr_devices) - set(ori_devices))
        while not delta_devices:
            time.sleep(consts.DEVICE_SCAN_TIME_DELAY)
            curr_devices = bdm.device_scan()
            delta_devices = list(set(curr_devices) - set(ori_devices))
            if time.time() - start_time > consts.DEVICE_SCAN_TIMEOUT:
                msg = _("Could not detect added device with "
                        "limited time")
                raise exceptions.FuxiException(msg)
        LOG.info("Get extra added block device %s", delta_devices)

        for device in delta_devices:
            if bdm.get_device_size(device) == volume.size:
                device = device.replace('/sys/block', '/dev')
                LOG.info("Find attached device %(dev)s"
                         " for volume %(at)s %(vol)s",
                         {'dev': device, 'at': attached_volume.name,
                          'vol': volume})

                link_path = os.path.join(consts.VOLUME_LINK_DIR, volume.id)
                try:
                    utils.execute('ln', '-s', device,
                                  link_path,
                                  run_as_root=True)
                except processutils.ProcessExecutionError as e:
                    LOG.error("Error happened when create link file for"
                              " block device attached by Nova."
                              " Error: %s", e)
                    raise
                return {'path': link_path}

        LOG.warning("Could not find matched device")
        raise exceptions.NotFound("Not Found Matched Device")

    def disconnect_volume(self, volume, **disconnect_opts):
        try:
            volume = self.cinderclient.volumes.get(volume.id)
        except cinder_exception.ClientException as e:
            LOG.error("Get Volume %s from Cinder failed", volume.id)
            raise

        try:
            link_path = self.get_device_path(volume)
            utils.execute('rm', '-f', link_path, run_as_root=True)
        except processutils.ProcessExecutionError as e:
            LOG.warning("Error happened when remove docker volume"
                        " mountpoint directory. Error: %s", e)

        try:
            self.novaclient.volumes.delete_server_volume(
                utils.get_instance_uuid(),
                volume.id)
        except nova_exception.ClientException as e:
            LOG.error("Detaching volume %(vol)s failed. Err: %(err)s",
                      {'vol': volume.id, 'err': e})
            raise

        volume_monitor = state_monitor.StateMonitor(self.cinderclient,
                                                    volume,
                                                    'available',
                                                    ('in-use', 'detaching',))
        return volume_monitor.monitor_cinder_volume()

    def get_device_path(self, volume):
        return os.path.join(consts.VOLUME_LINK_DIR, volume.id)
