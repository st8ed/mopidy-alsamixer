import errno
import logging
import math
import os
import random
import select
import struct
import time

import alsaaudio
import gi
import pykka

gi.require_version("GstAudio", "1.0")  # noqa
from gi.repository import GstAudio  # noqa isort:skip

from mopidy import exceptions, mixer  # noqa isort:skip


logger = logging.getLogger(__name__)


class AlsaMixer(pykka.ThreadingActor, mixer.Mixer):

    name = "alsamixer"

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.cardindex = self.config["alsamixer"]["card"]
        self.device = self.config["alsamixer"]["device"]
        self.control = self.config["alsamixer"]["control"]
        self.min_volume = self.config["alsamixer"]["min_volume"]
        self.max_volume = self.config["alsamixer"]["max_volume"]
        self.volume_scale = self.config["alsamixer"]["volume_scale"]

        if self.cardindex is not None:
            cardname = f"soundcard with index {self.cardindex:d}"
        else:
            cardname = f"soundcard with name '{self.device}'"

        known_cards = alsaaudio.cards()
        try:
            if self.cardindex is not None:
                known_controls = alsaaudio.mixers(cardindex=self.cardindex)
            else:
                known_controls = alsaaudio.mixers(device=self.device)
        except alsaaudio.ALSAAudioError:
            raise exceptions.MixerError(
                f"Could not find ALSA {cardname}. "
                "Known soundcards include: "
                f"{', '.join(known_cards)}"
            )

        if self.control not in known_controls:
            raise exceptions.MixerError(
                f"Could not find ALSA mixer control {self.control} on {cardname}. "
                f"Known mixers on {cardname} include: "
                f"{', '.join(known_controls)}"
            )

        self._last_volume = None
        self._last_mute = None
        self._observer = None
        self._observer_pipe = None

        logger.info(
            f"Mixing using ALSA, {cardname}, "
            f"mixer control {self.control!r}."
        )

    def on_start(self):
        rfd, wfd = os.pipe()
        self._observer_pipe = wfd
        self._observer = AlsaMixerObserver.start(self.actor_ref.proxy(), rfd)

    def on_stop(self):
        if self._observer is not None:
            os.write(self._observer_pipe, b"\xFF")
            self._observer.stop()

        if self._observer_pipe is not None:
            os.close(self._observer_pipe)

    def on_failure(self):
        if self._observer is not None:
            self._observer.stop()

    @property
    def mixer(self):
        try:
            return self._mixer
        except alsaaudio.ALSAAudioError:
            return None

    @property
    def _mixer(self):
        # The mixer must be recreated every time it is used to be able to
        # observe volume/mute changes done by other applications.
        if self.cardindex is not None:
            return alsaaudio.Mixer(
                cardindex=self.cardindex,
                control=self.control,
            )
        else:
            return alsaaudio.Mixer(
                device=self.device,
                control=self.control,
            )

    def get_volume(self):
        try:
            channels = self._mixer.getvolume()
        except alsaaudio.ALSAAudioError:
            return None
        if not channels:
            return None
        elif channels.count(channels[0]) == len(channels):
            return self.mixer_volume_to_volume(channels[0])
        else:
            # Not all channels have the same volume
            return None

    def set_volume(self, volume):
        try:
            self._mixer.setvolume(self.volume_to_mixer_volume(volume))
        except alsaaudio.ALSAAudioError as exc:
            logger.debug(f"Setting volume failed: {exc}")
            return False
        return True

    def mixer_volume_to_volume(self, mixer_volume):
        volume = mixer_volume
        if self.volume_scale == "cubic":
            volume = (
                GstAudio.StreamVolume.convert_volume(
                    GstAudio.StreamVolumeFormat.CUBIC,
                    GstAudio.StreamVolumeFormat.LINEAR,
                    volume / 100.0,
                )
                * 100.0
            )
        elif self.volume_scale == "log":
            # Uses our own formula rather than GstAudio.StreamVolume.
            # convert_volume(GstAudio.StreamVolumeFormat.LINEAR,
            # GstAudio.StreamVolumeFormat.DB, mixer_volume / 100.0)
            # as the result is a DB value, which we can't work with as
            # self._mixer provides a percentage.
            volume = math.pow(10, volume / 50.0)
        volume = (
            (volume - self.min_volume)
            * 100.0
            / (self.max_volume - self.min_volume)
        )
        return int(volume)

    def volume_to_mixer_volume(self, volume):
        mixer_volume = (
            self.min_volume
            + volume * (self.max_volume - self.min_volume) / 100.0
        )
        if self.volume_scale == "cubic":
            mixer_volume = (
                GstAudio.StreamVolume.convert_volume(
                    GstAudio.StreamVolumeFormat.LINEAR,
                    GstAudio.StreamVolumeFormat.CUBIC,
                    mixer_volume / 100.0,
                )
                * 100.0
            )
        elif self.volume_scale == "log":
            # Uses our own formula rather than GstAudio.StreamVolume.
            # convert_volume(GstAudio.StreamVolumeFormat.LINEAR,
            # GstAudio.StreamVolumeFormat.DB, mixer_volume / 100.0)
            # as the result is a DB value, which we can't work with as
            # self._mixer wants a percentage.
            mixer_volume = 50 * math.log10(mixer_volume)
        return int(mixer_volume)

    def get_mute(self):
        try:
            channels_muted = self._mixer.getmute()
        except alsaaudio.ALSAAudioError:
            return None
        if all(channels_muted):
            return True
        elif not any(channels_muted):
            return False
        else:
            # Not all channels have the same mute state
            return None

    def set_mute(self, mute):
        try:
            self._mixer.setmute(int(mute))
            return True
        except alsaaudio.ALSAAudioError as exc:
            logger.debug(f"Setting mute state failed: {exc}")
            return False

    def trigger_events_for_changed_values(self):
        old_volume, self._last_volume = self._last_volume, self.get_volume()
        old_mute, self._last_mute = self._last_mute, self.get_mute()

        if old_volume != self._last_volume:
            self.trigger_volume_changed(self._last_volume)

        if old_mute != self._last_mute:
            self.trigger_mute_changed(self._last_mute)


class AlsaMixerObserver(pykka.ThreadingActor):
    name = "alsamixer-observer"

    def __init__(self, parent, wake_fd=None):
        super().__init__()

        self._parent = parent
        self._wake_fd = wake_fd

    def on_start(self):
        while True:
            try:
                self._listen()
            except (exceptions.MixerError, OSError) as exc:
                logger.debug(
                    "ALSA mixer observer is unable to poll controls. "
                    "Retrying in a few seconds... "
                    f"Error: {exc}"
                )
                time.sleep(random.uniform(5, 10))
            except SystemExit:
                logger.info("Stopping ALSA mixer observer loop...")
                return

    def _listen(self):
        poll = self._create_poll()

        while True:
            changes = False

            for _fd, event in poll.poll():
                if event & select.EPOLLHUP:
                    return
                elif event & select.EPOLLERR:
                    raise OSError(errno.EBADF)
                else:
                    changes = True

            if not self._parent.actor_ref.is_alive():
                if self._wake_fd is not None:
                    os.close(self._wake_fd)
                raise SystemExit()

            if changes:
                self._call_parent()

    def _create_poll(self):
        fds = self._mixer.polldescriptors()

        poll = select.epoll()

        if self._wake_fd is not None:
            poll.register(self._wake_fd, select.EPOLLIN | select.EPOLLET)

        # FIXME: Remove when pyalsaaudio is upgraded
        # See https://github.com/larsimmisch/pyalsaaudio/pull/108
        def check_fd(fd):
            return fd != -1 and fd != struct.unpack("I", b"\xFF\xFF\xFF\xFF")[0]

        for fd, event_mask in fds:
            if check_fd(fd):
                poll.register(fd, event_mask | select.EPOLLET)

        return poll

    @property
    def _mixer(self):
        mixer = self._parent.mixer.get()
        if mixer is None:
            raise exceptions.MixerError("Mixer is not available")

        return mixer

    def _call_parent(self):
        self._parent.trigger_events_for_changed_values().get()
