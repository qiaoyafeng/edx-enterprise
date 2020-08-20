# -*- coding: utf-8 -*-
"""
Class for transmitting content metadata to Moodle.
"""

from integrated_channels.integrated_channel.transmitters.content_metadata import ContentMetadataTransmitter
from integrated_channels.moodle.client import MoodleAPIClient


class MoodleContentMetadataTransmitter(ContentMetadataTransmitter):
    """
    This transmitter transmits exported content metadata to Moodle.
    """

    def __init__(self, enterprise_configuration, client=MoodleAPIClient):
        """
        Use the ``MoodleAPIClient`` for content metadata transmission to Moodle.
        """
        super(MoodleContentMetadataTransmitter, self).__init__(
            enterprise_configuration=enterprise_configuration,
            client=client
        )

    def _prepare_items_for_transmission(self, channel_metadata_items):
        course_list = []
        items = {}
        for index, item in enumerate(channel_metadata_items):
            for key in item:
                new_key = 'courses[{0}][{1}]'.format(index, key)
                items[new_key] = item[key]
            course_list.append(items)
        return course_list
