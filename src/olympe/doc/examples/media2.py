import olympe
import os
import subprocess
import tempfile
from olympe.media import (
    media_created,
    resource_created,
    media_removed,
    resource_removed,
    resource_downloaded,
    indexing_state,
    delete_media,
    download_media,
    download_media_thumbnail,
    MediaEvent,
)
from olympe.messages.camera2 import Command, Config, Event
from logging import getLogger

olympe.log.update_config(
    {
        "loggers": {
            "olympe": {"level": "INFO", "handlers": ["console"]},
            "urllib3": {"level": "DEBUG", "handlers": ["console"]},
            __name__: {"level": "DEBUG", "handlers": ["console"]},
        }
    }
)

logger = getLogger(__name__)

DRONE_IP = os.environ.get("DRONE_IP", "192.168.42.1")
DRONE_MEDIA_PORT = os.environ.get("DRONE_MEDIA_PORT", "80")


class MediaEventListener(olympe.EventListener):
    def __init__(self, media):
        super().__init__(media, timeout=60)
        self._media = media
        self._media_id = []
        self._downloaded_resources = []
        self.remote_resource_count = 0
        self.local_resource_count = 0

    @olympe.listen_event(media_created())
    def onMediaCreated(self, event, scheduler):
        self._media_id.append(event.media_id)
        logger.info(f"media_created {event.media_id}")
        # When using the photo burst mode, the `media_created` event is sent by the
        # drone when the first photo resource is available for download.  The
        # `media_created` event does not include a full listing of all the future
        # resources of this media. The `resource_created` event will be sent
        # by the drone for the remaining resources.
        # However, the "download_media" and "download_media_thumbnail" will do
        # the right thing and download for you any subsequent resources associated
        # to this media id automatically.
        self._media(
            download_media_thumbnail(event.media_id) & download_media(event.media_id)
        )

    @olympe.listen_event(resource_created())
    def onResourceCreated(self, event, scheduler):
        logger.info(f"resource_created {event.resource_id}")

    @olympe.listen_event(media_removed())
    def onMediaRemoved(self, event, scheduler):
        logger.info(f"media_removed {event.media_id}")

    @olympe.listen_event(resource_removed())
    def onResourceRemoved(self, event, scheduler):
        logger.info(f"resource_removed {event.resource_id}")

    @olympe.listen_event(resource_downloaded())
    def onResourceDownloaded(self, event, scheduler):
        if event.is_thumbnail:
            return
        logger.info(
            "resource_downloaded {} {}".format(
                event.resource_id,
                event.data["download_path"],
            )
        )
        self._downloaded_resources.append(
            self._media.resource_info(resource_id=event.resource_id)
        )

    @olympe.listen_event()
    def default(self, event, scheduler):
        if isinstance(event, MediaEvent):
            logger.info(event)

    def unsubscribe(self):
        self._media.wait_for_pending_downloads()
        # Sanity check 1/2: md5 checksum
        # The integrity check has already been performed by Olympe
        # For this example the following step demonstrate how to perform the media
        # integrity check afterward using the "md5summ --check *.md5" command.
        for resource in self._downloaded_resources:
            check = subprocess.run(
                ["md5sum", "--check", resource.download_md5_path],
                stdout=subprocess.PIPE,
                cwd=os.path.dirname(resource.download_path),
            )
            stdout = check.stdout.decode().strip()
            if check.returncode == 0:
                logger.info("Integrity check: " + stdout)
            else:
                logger.error("Integrity check: " + stdout)
                super().unsubscribe()
                return

        # Sanity check 2/2: local downloaded resources equals the number of remote resources
        self.remote_resource_count = sum(
            map(
                lambda id_: len(self._media.resource_info(media_id=id_)), self._media_id
            )
        )
        self.local_resource_count = len(self._downloaded_resources)
        if self.local_resource_count != self.remote_resource_count:
            logger.error(
                "Downloaded {} resources instead of {}".format(
                    self.local_resource_count,
                    self.remote_resource_count,
                )
            )
            super().unsubscribe()
            return

        # OK then, we can now safely delete the remote media
        for media_id in self._media_id:
            delete = delete_media(media_id, _timeout=10)
            if not self._media(delete).wait().success():
                logger.error(
                    f"Failed to delete media {media_id} {delete.explain()}"
                )
        super().unsubscribe()


def setup_photo_burst_mode(drone):
    # For the file_format: jpeg is the only available option
    # dng is not supported in burst mode
    drone(
        Command.Configure(
            camera_id=0,
            config=Config(
                camera_mode="photo",
                photo_mode="burst",
                photo_file_format="jpeg",
                photo_burst_value="14_over_1s",
                photo_dynamic_range="standard",
                photo_resolution="12_mega_pixels",
            ),
            _timeout=3.0,
        )
    ).wait()


def main(drone, media=None):
    setup_photo_burst_mode(drone)
    if media is None:
        assert drone.media_autoconnect
        media = drone.media
    media.download_dir = tempfile.mkdtemp(prefix="olympe_media_example_")
    media.integrity_check = True
    logger.info("waiting for media resources indexing...")
    if not media(indexing_state(state="indexed")).wait(_timeout=60).success():
        logger.error("Media indexing timed out")
        return
    logger.info("media resources indexed")
    with MediaEventListener(media) as media_listener:
        media_state = drone(media_created(_timeout=3.0))
        photo_capture = drone(
            Event.Photo(
                type="stop",
                stop_reason="capture_done",
                _timeout=3.0,
                _policy="wait",
            )
            & Command.StartPhoto(camera_id=0)
        ).wait()
        assert photo_capture, photo_capture.explain()
        media_state.wait()
        assert media_state, media_state.explain()
    assert (
        media_listener.remote_resource_count > 0
    ), "remote resource count == 0"
    assert (
        media_listener.remote_resource_count == media_listener.local_resource_count
    ), "remote resource count = {} != {}".format(
        media_listener.remote_resource_count, media_listener.local_resource_count
    )


def test_media():
    # Here we want to demonstrate olympe.Media class usage as a standalone API
    # so we disable the drone object media autoconnection (we won't use
    # drone.media) and choose to instantiate the olympe.Media class ourselves
    with olympe.Drone(
        DRONE_IP,
        media_autoconnect=False,
        media_port=DRONE_MEDIA_PORT,
        name="example_media_standalone",
    ) as drone:
        assert drone.connect()
        media = olympe.Media(
            f"{DRONE_IP}:{DRONE_MEDIA_PORT}",
            name="example_media_standalone",
            scheduler=drone.scheduler,
        )
        assert media.connect()
        main(drone, media)
        media.shutdown()
        assert drone.disconnect()

    # By default, the drone instantiate an internal olympe.Media object (media_autoconnect=True
    # by default). This olympe.Media object is exposed through the Drone.media property. In this
    # case the connection to the remote media API endpoint is automatically handled by the olympe
    # drone controller class.
    with olympe.Drone(
        DRONE_IP, media_port=DRONE_MEDIA_PORT, name="example_media_autoconnect"
    ) as drone:
        assert drone.connect(retry=5, timeout=60)
        main(drone)
        assert drone.disconnect()


if __name__ == "__main__":
    test_media()
