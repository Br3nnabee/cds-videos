# -*- coding: utf-8 -*-
#
# This file is part of CERN Document Server.
# Copyright (C) 2020 CERN.
#
# CERN Document Server is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# CERN Document Server is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with CERN Document Server; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place, Suite 330, Boston,
# MA 02111-1307, USA.
#
# In applying this license, CERN does not
# waive the privileges and immunities granted to it by virtue of its status

# as an Intergovernmental Organization or submit itself to any jurisdiction.

"""Flow migration helper functions."""

from flask import current_app
from flask_login import current_user
from invenio_db import db

from .api import Flow
from ..deposit.api import deposit_video_resolver
from ..opencast.utils import can_be_transcoded
from ..records.api import CDSVideosFilesIterator
from .models import Status


def migrate_event(deposit_id):
    """Migrate an old event into Flows."""
    # Update flow task status depending on the content of th record
    deposit = deposit_video_resolver(deposit_id)

    original_file = CDSVideosFilesIterator.get_master_video_file(deposit)
    has_metadata = 'extracted_metadata' in deposit.get('_cds', {})
    has_frames = bool(CDSVideosFilesIterator.get_video_frames(original_file))
    subformats = CDSVideosFilesIterator.get_video_subformats(original_file)

    flow = Flow(deposit_id=deposit_id,
                user_id=current_user,
                payload=dict(
                    version_id=original_file["version_id"],
                    key=original_file["key"],
                    bucket_id=deposit['_buckets']['deposit'],
                    deposit_id=deposit_id
                ))

    subformat_done = [
        f.get('tags', {}).get('preset_quality', '') for f in subformats
    ]
    missing_subformats = [
        s
        for s in set(current_app.config['CDS_OPENCAST_QUALITIES'].keys()) - set(subformat_done)
        if can_be_transcoded(
            s,
            int(original_file['tags']['width']),
            int(original_file['tags']['height']),
        )
    ]

    with db.session.begin_nested():
        for task in flow.model.tasks:
            if 'DownloadTask' in task.name:
                task.status = Status.SUCCESS
            elif 'ExtractFramesTask' in task.name:
                task.status = Status.SUCCESS if has_frames else Status.PENDING
            elif 'ExtractMetadataTask' in task.name:
                task.status = (
                    Status.SUCCESS if has_metadata else Status.PENDING
                )
            elif 'TranscodeVideoTask' in task.name:
                preset_quality = task.payload.get('preset_quality')
                if preset_quality in missing_subformats:
                    task.status = Status.FAILURE
                elif preset_quality not in subformat_done:
                    task.status = Status.SUCCESS
                    task.message = 'Not transcoding for {}'.format(
                        preset_quality
                    )
            db.session.add(task)
    db.session.commit()

    return flow