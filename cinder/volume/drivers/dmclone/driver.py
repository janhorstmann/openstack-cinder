# SPDX-License-Identifier: Apache-2.0

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
from cinder.image import image_utils
from cinder import interface
from cinder import objects
from cinder import utils
from cinder.volume import configuration
from cinder.volume.drivers.dmclone.dmsetup import DMSetup
from cinder.volume.drivers import lvm
from cinder.volume import rpcapi as volume_rpcapi
from cinder.volume import volume_utils

LOG = logging.getLogger(__name__)

driver_opts = [
    cfg.StrOpt(
        "metadata_volume_group",
        default=None,
        sample_default='Defaults to "volume_group"',
        help="Name for the VG that will contain exported volumes",
    ),
    cfg.StrOpt(
        "metadata_volume_size",
        default="16s",
        help="Size clone metadata volumes will be created with."
        "Values get parsed by lvcreate`s '--size' option",
    ),
    cfg.IntOpt(
        "clone_region_size",
        default=8,
        help="The size of a region in sectors"
        "https://docs.kernel.org/admin-guide/device-mapper/"
        "dm-clone.html#constructor",
    ),
    cfg.BoolOpt(
        "clone_no_discard_passdown",
        default=False,
        help="Disable passing down discards to the destination device"
        "https://docs.kernel.org/admin-guide/device-mapper/"
        "dm-clone.html#constructor",
    ),
    cfg.IntOpt(
        "clone_hydration_threshold",
        default=None,
        sample_default="Use device mapper clone target defaults",
        help="Maximum number of regions being copied from the source "
        "to the destination device at any one time, during "
        "background hydration."
        "https://docs.kernel.org/admin-guide/device-mapper/"
        "dm-clone.html#constructor",
    ),
    cfg.IntOpt(
        "clone_hydration_batch_size",
        default=None,
        sample_default="Use device mapper clone target defaults",
        help="During background hydration, try to batch together "
        "contiguous regions, so we copy data from the source to "
        "the destination device in batches of this many regions."
        "https://docs.kernel.org/admin-guide/device-mapper/"
        "dm-clone.html#constructor",
    ),
]

CONF = cfg.CONF
CONF.register_opts(driver_opts, group=configuration.SHARED_CONF_GROUP)


@interface.volumedriver
class DMCloneVolumeDriver(lvm.LVMVolumeDriver):
    VERSION = "0.0.1"

    # ThirdPartySystems wiki page
    CI_WIKI_NAME = ""

    def __init__(self, *args, **kwargs):
        super(DMCloneVolumeDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(driver_opts)
        self.backend_name = (
            self.configuration.safe_get("volume_backend_name") or "DMClone"
        )
        root_helper = utils.get_root_helper()
        self.dmsetup = DMSetup(root_helper)
        self.metadata_volume_group = self.configuration.safe_get(
            "metadata_volume_group"
        ) or self.configuration.safe_get("volume_group")
        self.vg_metadata = brick_lvm.LVM(self.metadata_volume_group, root_helper)

    def _load_or_create_clone_target(
        self, volume, src_dev, enable_hydration=False, create=False
    ):
        # NOTE(jhorstmann): Sizes in device mapper are in sectors
        # A sector is 512 Byte and volume['size'] is in GiByte
        # GiByte / 512 Byte/sector
        # = 1024 * 1024 * 1024 Byte / 512 Byte/sector
        # = 2097152 sector
        options = [
            "0",
            str(volume["size"] * 2097152),
            "clone",
            "/dev/{0}/{1}".format(
                self.metadata_volume_group, self._metadata_dev_name(volume)
            ),
            self.local_path(volume),
            src_dev,
            str(self.configuration.clone_region_size),
        ]
        feature_args = []
        if not enable_hydration:
            feature_args.append("no_hydration")
        if self.configuration.clone_no_discard_passdown:
            feature_args.append("no_discard_passdown")
        options.append(str(len(feature_args)))
        options += feature_args
        core_args = []
        if self.configuration.clone_hydration_threshold:
            core_args.append("hydration_threshold")
            core_args.append(self.configuration.clone_hydration_threshold)
        if self.configuration.clone_hydration_batch_size:
            core_args.append("hydration_batch_size")
            core_args.append(self.configuration.clone_hydration_batch_size)
        if len(core_args) > 0:
            options.append(str(len(core_args)))
            options += core_args
        if create:
            self.dmsetup.create(self._dm_target_name(volume), " ".join(options))
        else:
            self.dmsetup.suspend(self._dm_target_name(volume))
            self.dmsetup.load(self._dm_target_name(volume), " ".join(options))
            self.dmsetup.resume(self._dm_target_name(volume))

    def _load_or_create_linear_target(self, volume, create=False):
        options = [
            "0",
            str(volume["size"] * 2097152),
            "linear",
            self.local_path(volume),
            "0",
        ]
        if create:
            self.dmsetup.create(self._dm_target_name(volume), " ".join(options))
        else:
            self.dmsetup.suspend(self._dm_target_name(volume))
            self.dmsetup.load(self._dm_target_name(volume), " ".join(options))
            self.dmsetup.resume(self._dm_target_name(volume))

    def check_for_setup_error(self):
        LOG.debug("Running check for setup error")
        super(DMCloneVolumeDriver, self).check_for_setup_error()

        # NOTE(jhorstmann): Find volumes of this host
        # TODO: Instead of `volume_backend_name` we need the OptGroup of the
        # backend configuration here. The volume_backend_name currently needs
        # to be the same as the OptGroup
        # Fix this!
        host = self.hostname + "@" + self.backend_name
        ctxt = context.get_admin_context()
        volumes = objects.volume.VolumeList.get_all_by_host(ctxt, host)

        LOG.debug(
            "Received volume list for host %(host)s: %(volumes)s",
            {"host": host, "volumes": volumes},
        )
        for volume in volumes:
            # NOTE(jhorstmann): Create dm targets if they do not exist
            if volume.status == "creating":
                continue
            try:
                self.dmsetup.status(self._dm_target_name(volume))
            except Exception:
                # TODO: Check for exact error
                source = volume.admin_metadata.get("dmclone:source", None)
                if source:
                    if self.vg_metadata.get_volume(self._metadata_dev_name(volume)):
                        src_volume = objects.volume.Volume.get_by_id(ctxt, source)
                        connector, connector_data = self._get_connector(src_volume)
                        src_volume_handle = connector.connect_volume(connector_data)
                        LOG.debug(
                            "Obtained handle for source volume %(volume)s: "
                            "%(handle)s",
                            {"volume": src_volume, "handle": src_volume_handle},
                        )

                        hydration = volume.admin_metadata.get(
                            "dmclone:hydration", False
                        )
                        if not hydration:
                            self._load_or_create_clone_target(
                                volume, src_volume_handle["path"], create=True
                            )
                        else:
                            self._load_or_create_clone_target(
                                volume,
                                src_volume_handle["path"],
                                enable_hydration=True,
                                create=True,
                            )
                    else:
                        volume["status"] = "maintenance"
                        volume.save()
                        raise exception.InvalidVolume(
                            reason="Volume is still migrating, but has no "
                            "metadata device"
                        )
                else:
                    self._load_or_create_linear_target(volume, create=True)

            # NOTE(jhorstmann): Make sure source volumes are exported
            source = volume.admin_metadata.get("dmclone:source", None)
            if source:
                self.ensure_export(ctxt, volume)

        self.migration_monitor = loopingcall.FixedIntervalLoopingCall(
            self._migration_monitor
        )
        self.migration_monitor.start(interval=10, stop_on_exception=False)

    def _metadata_dev_name(self, volume):
        return volume.name + "-metadata"

    def _dm_target_name(self, volume):
        return volume.name + "-handle"

    def _switch_volumes(self, volume, other_volume):
        # TODO: Switch service uuid?
        tmp = volume.name_id
        volume.name_id = other_volume.name_id
        other_volume.name_id = tmp
        for field in (
            "host",
            "cluster_name",
            "availability_zone",
            "provider_id",
            "provider_location",
            "provider_auth",
            "provider_geometry",
        ):
            tmp = volume[field]
            volume[field] = other_volume[field]
            other_volume[field] = tmp
        volume.save()
        other_volume.save()

    def _migration_monitor(self):
        LOG.debug("Starting migration monitor")
        host = self.hostname + "@" + self.backend_name
        ctxt = context.get_admin_context()
        migrating_volumes = [
            v
            for v in objects.volume.VolumeList.get_all_by_host(ctxt, host)
            if v.admin_metadata.get("dmclone:source", None)
        ]
        LOG.debug(
            "Found migrating volumes: %(volumes)s", {"volumes": migrating_volumes}
        )
        for volume in migrating_volumes:
            dm_status = self.dmsetup.status(self._dm_target_name(volume))
            if dm_status[2] != "clone":
                LOG.error(
                    "Volume %(id)s has 'dmclone:source' %(source)s, "
                    "but device mapper target is %(dm_status)s where clone "
                    "was expected",
                    {
                        "id": volume.name_id,
                        "source": volume.admin_metadata.get("dmclone:source", None),
                        "dm_status": dm_status[2],
                    },
                )
                continue
            else:
                # NOTE(jhorstmann): Status output for clone target described in
                # https://docs.kernel.org/admin-guide/device-mapper/dm-clone.html#status
                # E.g.:
                # 0 2097152 clone 8 30/262144 8 262144/262144 0 0 4 \
                # hydration_threshold 1 hydration_batch_size 1 rw
                hydrated = dm_status[6].split("/")
                # NOTE(jhorstmann): If hydration completed we finish the
                # migration process
                if hydrated[0] == hydrated[1] and dm_status[7] == "0":
                    LOG.debug(
                        "Completing migration for volume %(volume)s", {"voume": volume}
                    )
                    volume.admin_metadata.pop("dmclone:hydration", None)
                    src_volume = objects.Volume.get_by_id(
                        ctxt, volume.admin_metadata.pop("dmclone:source", None)
                    )
                    self._load_or_create_linear_target(volume)
                    LOG.debug(
                        "Removing metadata device: %(device)s",
                        {"device": self._metadata_dev_name(volume)},
                    )
                    self.vg_metadata.delete(self._metadata_dev_name(volume))
                    LOG.debug(
                        "Looking for attachment in %(attachments)s",
                        {"attachments": src_volume.volume_attachment},
                    )

                    # NOTE(jhorstmann): There should only ever be one
                    # active attachment on source volumes.
                    attachments = [
                        attachment
                        for attachment in src_volume.volume_attachment
                        if attachment.attach_status == "attached"
                    ]
                    if len(attachments) != 1:
                        raise exception.InvalidVolume(
                            reason="Unexpected number of attachments for "
                            "volume {src} while trying to complete "
                            "migration of volume {dst}".format(
                                src=src_volume.id, dst=volume.id
                            )
                        )
                    attachment = attachments[0]
                    connector = volume_utils.brick_get_connector(
                        attachment.connection_info["driver_volume_type"],
                        use_multipath=self.configuration.use_multipath_for_image_xfer,
                        device_scan_attempts=self.configuration.num_volume_device_scan_tries,
                        conn=attachment.connection_info,
                    )
                    LOG.debug(
                        "Disconnecting source volume: " "%(connection)s",
                        {"connection": attachment.connection_info},
                    )
                    connector.disconnect_volume(
                        connection_properties=attachment.connection_info,
                        device_info=attachment.connection_info,
                        force=True,
                    )
                    LOG.debug(
                        "Calling RPC API to delete volume: " "%(volume)s",
                        {"volume": src_volume},
                    )
                    rpcapi = volume_rpcapi.VolumeAPI()
                    rpcapi.attachment_delete(
                        ctxt,
                        attachment.id,
                        src_volume,
                    )
                    rpcapi.delete_volume(ctxt, src_volume)

                    if volume["status"] == "maintenance":
                        volume["status"] = "available"

                    volume.save()

    def _update_volume_stats(self):
        super(DMCloneVolumeDriver, self)._update_volume_stats()

        data = {}

        data["volume_backend_name"] = self.backend_name
        data["vendor_name"] = "Open Source"
        data["driver_version"] = self.VERSION
        data["storage_protocol"] = self.protocol

        # NOTE(jhorstmann): Extend attached volume is currently missing on nova
        # side for libvirt and local volume type
        # https://github.com/janhorstmann/openstack-nova/commit/bf0239bb4899a973b357bcc91c193b27e2508758
        data['online_extend_support'] = False

        self._stats.update(data)

    def create_volume(self, volume):
        LOG.debug("Creating volume: %(volume)s", {"volume": volume})
        super(DMCloneVolumeDriver, self).create_volume(volume)
        self._load_or_create_linear_target(volume, create=True)
        source = volume.admin_metadata.get("dmclone:source", None)
        if source:
            try:
                ctxt = context.get_admin_context()
                src_volume = objects.volume.Volume.get_by_id(ctxt, source)
                if not src_volume:
                    raise exception.ValidationError(
                        "Source volume not found for volume: {0}".format(volume)
                    )
                LOG.debug("Found source volume: %(volume)s", {"volume": src_volume})

                # NOTE(jhorstmann): Push the magic button for a remote
                # connection
                src_volume.admin_metadata.update(
                    {"dmclone:request_remote_connection": True}
                )
                src_volume.save()

                attachment = src_volume.begin_attach("ro")

                connector_properties = volume_utils.brick_get_connector_properties(
                    self.configuration.use_multipath_for_image_xfer,
                    enforce_multipath=False,
                )
                rpcapi = volume_rpcapi.VolumeAPI()
                attachment.connection_info = rpcapi.attachment_update(
                    ctxt, src_volume, connector_properties, attachment.id
                )
                LOG.debug(
                    "Connection details: %(con)s", {"con": attachment.connection_info}
                )
                connector = volume_utils.brick_get_connector(
                    attachment.connection_info["driver_volume_type"],
                    use_multipath=self.configuration.use_multipath_for_image_xfer,
                    device_scan_attempts=self.configuration.num_volume_device_scan_tries,
                    conn=attachment.connection_info,
                )
                src_volume_handle = connector.connect_volume(attachment.connection_info)
                attachment.finish_attach(
                    None, self.hostname, src_volume_handle["path"], "ro"
                )
                attachment.save()

                self.vg_metadata.create_volume(
                    self._metadata_dev_name(volume),
                    self.configuration.metadata_volume_size,
                )

                self._load_or_create_clone_target(volume, src_volume_handle["path"])
                # TODO: Move the move of the attachment to after the volume switch in
                # initialize_connection
                attachment.volume_id = volume.id
                attachment.save()
                # Cinder also adds an `access_mode=ro` property to admin
                # metadata for historical reasons. This needs to be moved as
                # well
                access_mode = src_volume.admin_metadata.pop("access_mode", None)
                if access_mode:
                    volume.admin_metadata.update({"access_mode": access_mode})
                    src_volume.save()
                    volume.save()

            except Exception:
                with excutils.save_and_reraise_exception():
                    LOG.exception(
                        "Failed to create migration volume: %(volume)s",
                        {"volume": volume},
                    )
                    volume["status"] = "error"
                    volume.save()
                    self._load_or_create_linear_target(volume)
                    connector.disconnect_volume(
                        attachment.connection_info["data"],
                        src_volume_handle["path"],
                        force=True,
                        ignore_errors=True,
                    )
                    rpcapi.attachment_delete(ctxt, attachment.id, volume)
                    self.vg_metadata.delete(self._metadata_dev_name(volume))
                    super(DMCloneVolumeDriver, self).delete_volume(volume)

        if volume.admin_metadata.get("dmclone:hydration", False):
            LOG.debug("Starting migration of volume %(volume)s", {"volume": volume})
            self.dmsetup.message(self._dm_target_name(volume), "0", "enable_hydration")

    def delete_volume(self, volume):
        LOG.debug("Deleting volume: %(volume)s", {"volume": volume})
        self.dmsetup.remove(self._dm_target_name(volume))
        super(DMCloneVolumeDriver, self).delete_volume(volume)

    def copy_image_to_volume(
        self, context, volume, image_service, image_id, disable_sparse=False
    ):
        """Fetch the image from image_service and write it to the volume."""
        image_utils.fetch_to_raw(
            context,
            image_service,
            image_id,
            "/dev/mapper/%s" % (self._dm_target_name(volume)),
            self.configuration.volume_dd_blocksize,
            size=volume["size"],
            disable_sparse=disable_sparse,
        )

    def copy_volume_to_image(self, context, volume, image_service, image_meta):
        """Copy the volume to the specified image."""
        volume_utils.upload_volume(
            context,
            image_service,
            image_meta,
            "/dev/mapper/%s" % (self._dm_target_name(volume)),
            volume,
        )

    def extend_volume(self, volume, new_size):
        """Extend an existing volume's size."""
        dm_table = self.dmsetup.table(self._dm_target_name(volume))

        if dm_table[2] != "linear":
            # NOTE(jhorstmann): Changing the size of a clone target is not supported
            # https://github.com/torvalds/linux/blob/da3ea35007d0af457a0afc87e84fddaebc4e0b63/drivers/md/dm-clone-metadata.c#L336
            raise exception.InvalidVolume(reason="Volume is still migrating")

        # NOTE(jhorstmann): We cannot simply call super().extend_volume() since
        # it also calls target_driver.extend_target(). We do not need to do
        # that since this driver's volumes are local.
        # This is taken from the lvm driver:
        ######################################################################
        self.vg.extend_volume(volume["name"], self._sizestr(new_size))
        ######################################################################

        dm_table[1] = str(new_size * 2097152)
        self.dmsetup.suspend(self._dm_target_name(volume))
        self.dmsetup.load(self._dm_target_name(volume), " ".join(dm_table))
        self.dmsetup.resume(self._dm_target_name(volume))

    def before_volume_copy(self, context, src_vol, dest_vol, remote=None):
        """Driver-specific actions before copyvolume data.

        This method will be called before _copy_volume_data during volume
        migration
        """
        dest_vol.admin_metadata.update({"dmclone:request_remote_connection": True})
        dest_vol.save()

    def after_volume_copy(self, context, src_vol, dest_vol, remote=None):
        """Driver-specific actions after copyvolume data.

        This method will be called after _copy_volume_data during volume
        migration
        """
        dest_vol.admin_metadata.pop("dmclone:request_remote_connection", None)
        dest_vol.save()

    def update_migrated_volume(self, ctxt, volume, new_volume, original_volume_status):
        """Return model update for migrated volume.

        Each driver implementing this method needs to be responsible for the
        values of _name_id and provider_location. If None is returned or either
        key is not set, it means the volume table does not need to change the
        value(s) for the key(s).
        The return format is {"_name_id": value, "provider_location": value}.

        :param volume: The original volume that was migrated to this backend
        :param new_volume: The migration volume object that was created on
                           this backend as part of the migration process
        :param original_volume_status: The status of the original volume
        :returns: model_update to update DB with any needed changes
        """
        model_update = super(DMCloneVolumeDriver, self).update_migrated_volume(
            ctxt, volume, new_volume, original_volume_status
        )
        # NOTE(jhorstmann): Rename the handle if the lv has been renamed
        current_name = self._dm_target_name(new_volume)
        new_name = (CONF.volume_name_template + "-handle") % (
            model_update["_name_id"] or volume.id
        )
        LOG.debug(
            "Updating volume after migration: %(old)s -> %(new)s",
            {"old": current_name, "new": new_name},
        )
        if current_name != new_name:
            self.dmsetup.rename(current_name, new_name)
        # NOTE(jhorstmann): After the update self._dm_target_name() should return the correct name again
        return model_update

    # #######  Interface methods for DataPath (Connector) ########

    def ensure_export(self, context, volume):
        self.vg.activate_lv(volume["name"])

        volume_path = "/dev/mapper/%s" % (self._dm_target_name(volume))

        model_update = self.target_driver.ensure_export(context, volume, volume_path)
        return model_update

    def create_export(self, context, volume, connector):
        self.vg.activate_lv(volume["name"])

        volume_path = "/dev/mapper/%s" % (self._dm_target_name(volume))

        export_info = self.target_driver.create_export(context, volume, volume_path)
        return {
            "provider_location": export_info["location"],
            "provider_auth": export_info["auth"],
        }

    def remove_export(self, context, volume):
        self.target_driver.remove_export(context, volume)

    def initialize_connection(self, volume, connector, **kwargs):
        LOG.debug("Initializing connection for volume %(volume)s", {"volume": volume})
        if volume.admin_metadata.pop("dmclone:request_remote_connection", False):
            volume.save()
            # NOTE(jhorstmann): Remote connection is requested:
            # Call target driver to initialize the connection and return it
            return self.target_driver.initialize_connection(volume, connector)

        LOG.debug(
            "Initializing connection for connector: %(connector)s",
            {"connector": connector},
        )
        if connector["host"] != volume_utils.extract_host(volume["host"], "host"):
            if volume.admin_metadata.get("dmclone:source", None):
                # NOTE(jhorstmann): Data migration is still ongoing.
                # We do not want to chain migrations, so the volume is
                # set to maintenance so that it cannot be attached
                # until migration is done and the state reset
                # This is so that there is visible feedback for the user
                # instead of silent failure
                LOG.info(
                    "Volume %(id)s still migrating during initialization "
                    "of connection, setting status to maintenance",
                    {"id": volume["id"]},
                )
                volume["status"] = "maintenance"
                volume.save()
                raise exception.InvalidVolume(reason="Volume is still migrating")

            # NOTE(jhorstmann): The assumption is that the remote backend
            # is the same as the local one
            dst_host = connector["host"] + "@" + volume["host"].split("@")[1]
            ctxt = context.get_admin_context()
            dst_service = objects.Service.get_by_args(
                ctxt,
                volume_utils.extract_host(dst_host, "backend"),
                constants.VOLUME_BINARY,
            )

            attachments = [
                attachment
                for attachment in volume.volume_attachment
                if attachment.attach_status in ["attached", "detaching"]
            ]
            LOG.debug(
                "Got attachments for volume %(id)s: %(attachments)s",
                {"id": volume["id"], "attachments": attachments},
            )

            options = {"dmclone:source": volume.id}
            if len(attachments) == 0:
                options.update({"dmclone:hydration": True})

            new_volume = objects.Volume(
                context=ctxt,
                host=dst_service["host"],
                availability_zone=dst_service.availability_zone,
                status="creating",
                attach_status=objects.fields.VolumeAttachStatus.DETACHED,
                cluster_name=dst_service["cluster_name"],
                use_quota=False,  # Don't use quota for temporary volume
                size=volume.size,
                user_id=volume.user_id,
                project_id=volume.project_id,
                display_description="migration src for " + volume["id"],
                admin_metadata=options,
            )

            # TODO: Get lock for new_volume
            new_volume.create()
            LOG.debug(
                "Created destination volume object: %(volume)s ", {"volume": new_volume}
            )

            LOG.debug(
                "Calling RPC API to create volume: %(volume)s", {"volume": new_volume}
            )
            rpcapi = volume_rpcapi.VolumeAPI()
            rpcapi.create_volume(ctxt, new_volume, None, None, allow_reschedule=False)
            LOG.debug(
                "Waiting for creation of volume: %(volume)s", {"volume": new_volume}
            )

            # Wait for new_volume to become ready
            deadline = time.time() + 60
            new_volume.refresh()
            tries = 0
            while new_volume.status != "available":
                tries += 1
                if time.time() > deadline or new_volume.status == "error":
                    try:
                        rpcapi.delete_volume(ctxt, new_volume)
                    except exception.VolumeNotFound:
                        LOG.info(
                            "Could not find the temporary volume "
                            "%(vol)s in the database. There is no need "
                            "to clean up this volume.",
                            {"vol": new_volume.id},
                        )

                    new_volume.destroy()
                    LOG.debug("Updated volume: %(volume)s ", {"volume": volume})
                    if new_volume.status == "error":
                        raise exception.VolumeMigrationFailed(
                            reason="Error creating remote volume"
                        )
                    else:
                        raise exception.VolumeMigrationFailed(
                            reason="Timeout waiting for remote volume creation"
                        )
                else:
                    time.sleep(tries**2)
                new_volume.refresh()

            # NOTE(jhorstmann): It seems that new volumes always end up
            # 'available'.The status is set to 'maintenance' here, so it
            # cannot be messed with
            new_volume.update({"status": "maintenance"})
            new_volume.save()
            LOG.debug(
                "Updated status for volume %(id)s to %(status)s",
                {"id": volume["id"], "status": volume["status"]},
            )

            # NOTE(jhorstmann): Switch volume identities, so that the current
            # volume references the newly created volume on the destination
            # and vice versa
            self._switch_volumes(volume, new_volume)
            # NOTE(jhorstmann): 'dmclone:source' points to the wrong volume now, change that
            new_volume.admin_metadata.pop("dmclone:source", None)
            volume.admin_metadata.update({"dmclone:source": new_volume.id})
            # NOTE(jhorstmann): Add a marker for the destination to the new source volume
            new_volume.admin_metadata.update({"dmclone:destination": volume.id})
            # NOTE(jhorstmann): Also move 'dmclone:hydration'
            if new_volume.admin_metadata.pop("dmclone:hydration", False):
                volume.admin_metadata.update({"dmclone:hydration": True})
            volume.save()
            new_volume.save()

            # TODO: For some reason the attachment volume_id is not updated.
            # Fix this and delete the attachment move at the end of create_volume
            # # NOTE(jhorstmann): Implicitly moving the local attachment to the
            # # remote volume with the volume switch above was intentional, but
            # # the remote attachment to the source volume needs to be moved back
            # attachments = objects.VolumeAttachmentList.get_all_by_volume_id(
            #     ctxt,
            #     volume.id
            # )
            # for attachment in attachments:
            #     LOG.debug(
            #         'Attachment after initialize_connection: %(attachment)s',
            #         {'attachment': attachment}
            #     )
            # attachment = [
            #     attachment
            #     for attachment in attachments
            #     if attachment.connection_info.get(
            #         'driver_volume_type', None) != 'local'
            # ][0]
            # attachment.volume_id = new_volume.id
            # attachment.save()
            # LOG.debug(
            #     'Attachment after initialize_connection: %(attachment)s',
            #     {'attachment': attachment}
            # )
            # TODO: Release lock for new_volume

        return {
            "driver_volume_type": "local",
            "data": {"device_path": "/dev/mapper/" + self._dm_target_name(volume)},
        }

    def terminate_connection(self, volume, connector, **kwargs):
        """Disallow connection from connector

        :param volume: The volume to be disconnected.
        :param connector: A dictionary describing the connection with details
                          about the initiator. Can be None.
        """
        LOG.debug("Terminating connection for volume %(volume)s", {"volume": volume})
        ctxt = context.get_admin_context()
        attachments = [
            attachment
            for attachment in volume.volume_attachment
            if attachment.attach_status in ["attached", "attaching", "detaching"]
        ]
        LOG.debug(
            "Got attachments for volume %(id)s: %(attachments)s",
            {"id": volume["id"], "attachments": attachments},
        )
        if (
            len(attachments) == 1
            and attachments[0].connection_info["driver_volume_type"] != "local"
        ):
            # NOTE(jhorstmann): Termination of non-local attachments.
            # These are the terminations which actually need
            # to be passed to the target driver
            self.target_driver.terminate_connection(volume, connector)

        else:
            source = volume.admin_metadata.get("dmclone:source", None)
            if source:
                src_volume = objects.Volume.get_by_id(ctxt, source)
                hydration = volume.admin_metadata.get("dmclone:hydration", False)
                if not hydration:
                    # The connector is required to decide what to do
                    if not connector:
                        raise exception.InvalidConnectorException(
                            missing="Connector object is None"
                        )
                    if connector["host"] == self.hostname:
                        # NOTE(jhorstmann): Data migration has not started yet
                        # and everything may be cleaned up
                        self._load_or_create_linear_target(volume)
                        self.vg_metadata.delete(self._metadata_dev_name(volume))
                        attachment = [
                            attachment
                            for attachment in src_volume.volume_attachment
                            if (
                                attachment.connection_info["driver_volume_type"]
                                != "local"
                                and attachment.attached_host == self.hostname
                            )
                        ][0]
                        connector = volume_utils.brick_get_connector(
                            attachment.connection_info["driver_volume_type"],
                            use_multipath=self.configuration.use_multipath_for_image_xfer,
                            device_scan_attempts=self.configuration.num_volume_device_scan_tries,
                            conn=attachment.connection_info,
                        )
                        LOG.debug(
                            "Disconnecting source volume: " "%(connection)s",
                            {"connection": attachment.connection_info},
                        )
                        connector.disconnect_volume(
                            connection_properties=attachment.connection_info,
                            device_info=attachment.connection_info,
                            force=True,
                        )
                        self._switch_volumes(volume, src_volume)
                        LOG.debug(
                            "Calling RPC API to delete volume: " "%(volume)s",
                            {"volume": src_volume},
                        )
                        rpcapi = volume_rpcapi.VolumeAPI()
                        rpcapi.delete_volume(ctxt, src_volume)
                        volume.admin_metadata.pop("dmclone:source", None)
                        volume.admin_metadata.pop("dmclone:hydration", None)
                        volume.save()
                    else:
                        # NOTE(jhorstmann): Disconnection on the remote host
                        # means live-migration has succeded and we need to
                        # actually disconnect its local volume and start
                        # hydration
                        attachment = [
                            attachment
                            for attachment in attachments
                            if (
                                attachment.connection_info["driver_volume_type"]
                                == "local"
                                and attachment.attached_host != self.hostname
                            )
                        ][0]
                        connector = volume_utils.brick_get_connector(
                            attachment.connection_info["driver_volume_type"],
                            use_multipath=self.configuration.use_multipath_for_image_xfer,
                            device_scan_attempts=self.configuration.num_volume_device_scan_tries,
                            conn=attachment.connection_info,
                        )
                        LOG.debug(
                            "Disconnecting source volume: " "%(connection)s",
                            {"connection": attachment.connection_info},
                        )
                        connector.disconnect_volume(
                            connection_properties=attachment.connection_info,
                            device_info=attachment.connection_info,
                            force=True,
                        )
                        volume.admin_metadata.update({"dmclone:hydration": True})
                        self.dmsetup.message(
                            self._dm_target_name(volume), "0", "enable_hydration"
                        )
                        volume.save()
