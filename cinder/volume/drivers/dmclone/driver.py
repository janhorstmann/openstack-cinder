#  Copyright 2024 OSISM GmbH
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import time

from oslo_config import cfg
from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_utils import excutils

from cinder.brick.local_dev import lvm as brick_lvm
from cinder.common import constants
from cinder import context
from cinder import exception
from cinder import interface
from cinder import objects
from cinder import utils
from cinder.volume import configuration
from cinder.volume import driver
from cinder.volume.drivers.dmclone.dmsetup import DMSetup
from cinder.volume.drivers import lvm
from cinder.volume import rpcapi as volume_rpcapi
from cinder.volume import volume_utils

LOG = logging.getLogger(__name__)

driver_opts = []

CONF = cfg.CONF
CONF.register_opts(driver_opts, group=configuration.SHARED_CONF_GROUP)


@interface.volumedriver
class DMCloneVolumeDriver(lvm.LVMVolumeDriver):
    VERSION = '0.0.1'

    # ThirdPartySystems wiki page
    CI_WIKI_NAME = ""

    def __init__(self, *args, **kwargs):
        super(DMCloneVolumeDriver, self).__init__(*args, **kwargs)
        self.backend_name = self.configuration.safe_get(
            'volume_backend_name'
        ) or 'DMClone'
        root_helper = utils.get_root_helper()
        self.dmsetup = DMSetup(root_helper)
        # TODO: remove hardcoded VG
        self.vg_metadata = brick_lvm.LVM('vg0', root_helper)

    def check_for_setup_error(self):
        super(DMCloneVolumeDriver, self).check_for_setup_error()
        self.migration_monitor = loopingcall.FixedIntervalLoopingCall(
            self._migration_monitor
        )
        self.migration_monitor.start(
            interval=10,
            stop_on_exception=False
        )

    def _metadata_dev_name(self, volume):
        return volume.name + '-metadata'

    def _dm_target_name(self, volume):
        return volume.name + '-handle'

    def _get_connector(self, volume):
        # NOTE(jhorstmann): Figure out how to do this properly
        # One cannot just call the remote initialize_connection(), since this
        # will return a local connector with this driver

        # protocol = self.configuration.safe_get(
        #     'target_protocol'
        # )
        protocol = 'iscsi'
        if protocol.lower() == 'iscsi':
            connector_data = driver.ISCSIDriver(
            )._get_iscsi_properties(volume)

        connector = volume_utils.brick_get_connector(
            protocol
        )
        return (connector, connector_data)

    def _switch_volumes(self, volume, other_volume):
        tmp = volume.name_id
        volume.name_id = other_volume.name_id
        other_volume.name_id = tmp
        for field in (
            'host',
            'cluster_name',
            'availability_zone',
            'provider_id',
            'provider_location',
            'provider_auth',
            'provider_geometry'
        ):
            tmp = volume[field]
            volume[field] = other_volume[field]
            other_volume[field] = tmp
        volume.save()
        other_volume.save()

    def _find_src_volume(self, volume):
        # NOTE(jhorstmann): Find the source volume.
        src_volume_id = volume['migration_status'].split(':')[1]
        ctxt = context.get_admin_context()
        try:
            src_volume = objects.Volume.get_by_id(ctxt,
                                                  src_volume_id)
            LOG.debug('Found source volume: %(volume)s',
                      {'volume': src_volume})
        except exception.VolumeNotFound:
            src_volume = None
            LOG.error(
                'Source volume not found for volume ID: %(id)s',
                {'id': src_volume_id}
            )

        return src_volume

    def _disconnect_volume(self, volume):
        try:
            connector, connector_data = self._get_connector(
                volume
            )
            connector.disconnect_volume(
                connection_properties=connector_data,
                device_info=connector_data,
                force=True
            )
        except Exception:
            LOG.error(
                'Error disconnecting volume: %(volume)s',
                {'volume': volume}
            )
        rpcapi = volume_rpcapi.VolumeAPI()
        LOG.debug(
            'Calling RPC API to remove export for volume: '
            '%(volume)s',
            {'volume': volume}
        )
        ctxt = context.get_admin_context()
        rpcapi.remove_export(ctxt, volume, sync=True)

    def _migration_monitor(self):
        LOG.debug(
            'Starting migration monitor'
        )
        try:
            host = CONF.host + '@' + self.capabilities['volume_backend_name']
        except KeyError:
            # NOTE(jhorstmann): Capabilities seem to be initialized
            # asynchronously, so just skip running until they are available
            LOG.debug(
                'Could not find `volume_backend_name` in capablities. Maybe '
                'capabilities have not been initialized yet'
            )
            return
        ctxt = context.get_admin_context()
        migrating_volumes = [
            v for v in objects.volume.VolumeList.get_all_by_host(
                ctxt,
                host
            )
            if (v['migration_status']
                and v['migration_status'].startswith('target:'))
        ]
        LOG.debug(
            'Found migrating volumes: %(volumes)s',
            {'volumes': migrating_volumes}
        )
        for volume in migrating_volumes:
            dm_status = self.dmsetup.status(
                self._dm_target_name(volume)
            )
            if dm_status[2] != 'clone':
                LOG.error(
                    'Volume %(id)s has migration_status %(migration_status)s, '
                    'but device mapper target is %(dm_status)s where clone '
                    'was expected',
                    {
                        'id': volume.name_id,
                        'migration_status': volume['migration_status'],
                        'dm_status': dm_status[2]
                    }
                )
                continue
            else:
                # NOTE(jhorstmann): Status output for clone target described in
                # https://docs.kernel.org/admin-guide/device-mapper/dm-clone.html#status
                # E.g.:
                # 0 2097152 clone 8 30/262144 8 262144/262144 0 0 4 \
                # hydration_threshold 1 hydration_batch_size 1 rw
                hydrated = dm_status[6].split('/')
                # NOTE(jhorstmann): If hydration completed we finish the
                # migration process
                if hydrated[0] == hydrated[1] and dm_status[7] == '0':
                    LOG.debug(
                        'Completing migration for volume %(volume)s',
                        {'voume': volume}
                    )
                    src_volume = self._find_src_volume(volume)
                    volume.update({'migration_status': 'completing'})
                    volume.save()
                    LOG.debug(
                        'Hydration completed for volume: %(volume)s ',
                        {'volume': volume}
                    )
                    self.dmsetup.suspend(
                        self._dm_target_name(volume)
                    )
                    self.dmsetup.load(
                        self._dm_target_name(volume),
                        ' '.join(
                            [
                                '0',
                                str(volume['size'] * 2097152),
                                'linear',
                                self.local_path(volume),
                                '0'
                            ]

                        )
                    )
                    self.dmsetup.resume(
                        self._dm_target_name(volume)
                    )
                    LOG.debug(
                        'Removing metadata device: %(device)s',
                        {'device': self._metadata_dev_name(volume)}
                    )
                    self.vg_metadata.delete(
                        self._metadata_dev_name(volume)
                    )
                    if src_volume:
                        self._disconnect_volume(src_volume)
                        LOG.debug(
                            'Calling RPC API to delete volume: '
                            '%(volume)s',
                            {'volume': src_volume}
                        )
                        rpcapi = volume_rpcapi.VolumeAPI()
                        rpcapi.delete_volume(ctxt, src_volume)

                    volume.update({'migration_status': 'success'})
                    volume.save()

    def _update_volume_stats(self):
        super(DMCloneVolumeDriver, self)._update_volume_stats()

        data = {}

        data["volume_backend_name"] = self.backend_name
        data["vendor_name"] = 'Open Source'
        data["driver_version"] = self.VERSION
        data["storage_protocol"] = self.protocol
        self._stats.update(data)

    def create_volume(self, volume):
        LOG.debug('Creating volume: %(volume)s', {'volume': volume})
        super(DMCloneVolumeDriver, self).create_volume(volume)
        try:
            if volume['migration_status'] in ['starting', 'migrating']:
                # NOTE(jhorstmann): Use the volume's user-facing ID here
                filters = {'migration_status': 'target:' + volume['id']}
                LOG.debug(
                    'Looking for source volume with filters :%(filters)s`',
                    {'filters': filters}
                )
                ctxt = context.get_admin_context()
                src_volume = objects.volume.VolumeList.get_all(
                    ctxt,
                    limit=1,
                    filters=filters
                )[0]
                if not src_volume:
                    raise exception.ValidationError(
                        'Source volume not found for volume: {0}'.format(
                            volume
                        )
                    )
                LOG.debug('Found source volume: %(volume)s',
                          {'volume': src_volume})

                connector, connector_data = self._get_connector(
                    src_volume
                )
                src_volume_handle = connector.connect_volume(connector_data)
                LOG.debug(
                    'Obtained handle for source volume %(volume)s: %(handle)s',
                    {'volume': src_volume, 'handle': src_volume_handle}
                )

                self.vg_metadata.create_volume(
                    self._metadata_dev_name(volume),
                    '1g'
                )

                # NOTE(jhorstmann): Sizes in device mapper are in sectors
                # A sector is 512 Byte and volume['size'] is in GiByte
                # GiByte / 512 Byte/sector
                # = 1024 * 1024 * 1024 Byte / 512 Byte/sector
                # = 2097152 sector
                self.dmsetup.create(
                    self._dm_target_name(volume),
                    ' '.join(
                        [
                            '0',
                            str(volume['size'] * 2097152),
                            'clone',
                            # TODO: remove hardcoded VG
                            '/dev/vg0/' + self._metadata_dev_name(volume),
                            self.local_path(volume),
                            src_volume_handle['path'],
                            '8',
                            '1',
                            'no_hydration'
                        ]
                    )
                )
            if volume['migration_status'] == 'migrating':
                self.dmsetup.message(
                    self._dm_target_name(volume),
                    '0',
                    'enable_hydration'
                )
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(
                    "Failed to create migration volume: %(volume)s",
                    {'volume': volume}
                )
                volume['status'] = 'error'
                volume['migration_status'] = 'error'
                volume.save()
                self.dmsetup.remove(
                    self._dm_target_name(volume)
                )
                connector.disconnect_volume(
                    connector_data,
                    src_volume_handle['path'],
                    force=True,
                    ignore_errors=True
                )
                self.vg_metadata.delete(
                    self._metadata_dev_name(volume)
                )
                super(DMCloneVolumeDriver, self).delete_volume(volume)

    # #######  Interface methods for DataPath (Connector) ########

    def initialize_connection(self, volume, connector, **kwargs):
        LOG.debug('Initializing connection for connector: %(connector)s',
                  {'connector': connector})
        if connector['host'] == volume_utils.extract_host(volume['host'],
                                                          'host'):
            # NOTE(jhorstmann): If volume and connector are local add a linear
            # target for consistency

            self.dmsetup.create(
                self._dm_target_name(volume),
                ' '.join(
                    [
                        '0',
                        str(volume['size'] * 2097152),
                        'linear',
                        self.local_path(volume),
                        '0'
                    ]

                )
            )

        else:
            # NOTE(jhorstmann): Call target driver in case it does some local
            # initialization
            self.target_driver.initialize_connection(volume, connector)

            ctxt = context.get_admin_context()

            # NOTE(jhorstmann): The assumption is that the remote backend
            # is the same as the local one
            dst_host = connector['host'] + '@' + volume['host'].split('@')[1]
            dst_service = objects.Service.get_by_args(
                ctxt,
                volume_utils.extract_host(dst_host, 'backend'),
                constants.VOLUME_BINARY
            )

            if volume.status == 'reserved':
                migration_status = 'migrating'

            elif volume.status == 'in-use':
                migration_status = 'starting'
            else:
                migration_status = None

            new_volume = objects.Volume(
                context=ctxt,
                host=dst_service['host'],
                availability_zone=dst_service.availability_zone,
                status='creating',
                attach_status=objects.fields.VolumeAttachStatus.DETACHED,
                cluster_name=dst_service['cluster_name'],
                migration_status=migration_status,
                use_quota=False,  # Don't use quota for temporary volume
                size = volume.size,
                user_id = volume.user_id,
                project_id = volume.project_id,
            )

            new_volume.create()
            LOG.debug(
                'Created destination volume object: %(volume)s ',
                {'volume': new_volume}
            )

            # NOTE(jhorstmann): Order is important, this will be used by the
            # driver's create_volume() method.
            # Use the volume's user-facing ID here
            volume.update({'migration_status': 'target:' + new_volume['id']})
            volume.save()
            LOG.debug('Updated volume: %(volume)s ', {'volume': volume})

            LOG.debug(
                'Calling RPC API to create volume: %(volume)s',
                {'volume': new_volume}
            )
            rpcapi = volume_rpcapi.VolumeAPI()
            rpcapi.create_volume(
                ctxt, new_volume, None, None, allow_reschedule=False
            )
            LOG.debug(
                'Waiting for creation of volume: %(volume)s',
                {'volume': new_volume}
            )

            # Wait for new_volume to become ready
            deadline = time.time() + 60
            new_volume.refresh()
            tries = 0
            while new_volume.status != 'available':
                tries += 1
                if time.time() > deadline or new_volume.status == 'error':
                    try:
                        rpcapi.delete_volume(ctxt, new_volume)
                    except exception.VolumeNotFound:
                        LOG.info('Could not find the temporary volume '
                                 '%(vol)s in the database. There is no need '
                                 'to clean up this volume.',
                                 {'vol': new_volume.id})

                    new_volume.destroy()
                    volume.update({'migration_status': 'error'})
                    volume.save()
                    LOG.debug(
                        'Updated volume: %(volume)s ',
                        {'volume': volume}
                    )
                    if new_volume.status == 'error':
                        raise exception.VolumeMigrationFailed(
                            reason='Error creating remote volume'
                        )
                    else:
                        raise exception.VolumeMigrationFailed(
                            reason='Timeout wating for remote volume creation'
                        )
                else:
                    time.sleep(tries ** 2)
                new_volume.refresh()

            # NOTE(jhorstmann): It seems that new volumes always end up
            # 'available'.The status is set to 'maintenance' here, so it
            # cannot be messed with
            new_volume.update({'status': 'maintenance'})
            # Use the volume's user-facing ID here
            new_volume['display_description'] = 'migration src for ' \
                                                + volume['id']
            new_volume.save()
            LOG.debug(
                'Updated migration_status for volume %(id)s to %(status)s',
                {'id': volume['id'], 'status': volume['status']}
            )

            # NOTE(jhorstmann): Switch volume identities, so that the current
            # volume references the newly created volume on the destination
            # and vice versa
            self._switch_volumes(volume, new_volume)

        return {
            'driver_volume_type': 'local',
            'data': {
                "device_path": '/dev/mapper/' + self._dm_target_name(volume)
            }
        }

    def terminate_connection(self, volume, connector, **kwargs):
        """Disallow connection from connector

        :param volume: The volume to be disconnected.
        :param connector: A dictionary describing the connection with details
                          about the initiator. Can be None.
        """
        if (
            volume['migration_status']
            and volume['migration_status'].startswith('target:')
        ):
            if volume['status'] == 'reserved':
                # NOTE(jhorstmann): Aborting attachment, clean up locally
                src_volume = self._find_src_volume(volume)
                self._switch_volumes(volume, src_volume)
                self.dmsetup.remove(
                    self._dm_target_name(src_volume)
                )
                self.vg_metadata.delete(
                    self._metadata_dev_name(src_volume)
                )
                self._disconnect_volume(volume)
                LOG.debug(
                    'Calling RPC API to delete volume: '
                    '%(volume)s',
                    {'volume': src_volume}
                )
                ctxt = context.get_admin_context()
                rpcapi = volume_rpcapi.VolumeAPI()
                rpcapi.delete_volume(ctxt, src_volume)
                volume['migration_status'] = None
                volume.save()
            elif volume['status'] == 'in-use':
                # NOTE(jhorstmann): Live-migration scenario.
                # The connector is required to decide what to do
                if not connector:
                    raise exception.InvalidConnectorException(
                        missing='Connector object is None'
                    )
                if connector['host'] == volume_utils.extract_host(
                    volume['host'],
                    'host'
                ):
                    # NOTE(jhorstmann): Disconnection on this host means
                    # live-migration has failed and we need to clean up
                    src_volume = self._find_src_volume(volume)
                    self._switch_volumes(volume, src_volume)
                    self.dmsetup.remove(
                        self._dm_target_name(src_volume)
                    )
                    self.vg_metadata.delete(
                        self._metadata_dev_name(src_volume)
                    )
                    self._disconnect_volume(volume)
                    LOG.debug(
                        'Calling RPC API to delete volume: '
                        '%(volume)s',
                        {'volume': src_volume}
                    )
                    ctxt = context.get_admin_context()
                    rpcapi = volume_rpcapi.VolumeAPI()
                    rpcapi.delete_volume(ctxt, src_volume)
                    volume['migration_status'] = None
                    volume.save()
                else:
                    # NOTE(jhorstmann): Disconnection on the remote host means
                    # live-migration has succeded and we need to actually
                    # disconnect the remote volume and start hydration
                    src_volume = self._find_src_volume(volume)
                    ctxt = context.get_admin_context()
                    rpcapi = volume_rpcapi.VolumeAPI()
                    rpcapi.terminate_connection(ctxt, src_volume, connector)
                    src_volume['migration_status'] = 'migrating'
                    src_volume.save()
                    self.dmsetup.message(
                        self._dm_target_name(volume),
                        '0',
                        'enable_hydration'
                    )
        # TODO: Consider if this should be a general `else`
        elif volume['status'] == 'detaching':
            self.dmsetup.remove(self._dm_target_name(volume))
        elif (
            volume['status'] == 'maintenance'
            and volume['migration_status'] == 'starting'
        ):
            self.dmsetup.remove(self._dm_target_name(volume))