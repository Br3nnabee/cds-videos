# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2018, 2020 CERN.
#
# Invenio is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Invenio is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

"""CDS Fixture Modules."""


import click
from click import ClickException
from flask import current_app
from flask.cli import with_appcontext
from invenio_db import db
from invenio_files_rest.models import ObjectVersion, ObjectVersionTag
from invenio_records_files.models import RecordsBuckets

from cds.modules.flows.deposit import index_deposit_project
from cds.modules.flows.models import FlowMetadata
from cds.modules.flows.tasks import ExtractFramesTask
from cds.modules.maintenance.subformats import (
    create_all_missing_subformats,
    create_all_subformats,
    create_subformat,
)
from cds.modules.records.api import CDSVideosFilesIterator
from cds.modules.deposit.api import Video
from cds.modules.records.resolver import record_resolver
from cds.modules.deposit.tasks import datacite_register
from invenio_db import db
from invenio_indexer.api import RecordIndexer
from datacite.errors import DataCiteError



def abort_if_false(ctx, param, value):
    if not value:
        ctx.abort()


@click.group()
def subformats():
    """Slaves command line utilities."""


# TODO: Test all the commands


@subformats.command()
@click.option("--recid", "recid", help="ID of the video record", default=None)
@click.option("--depid", "depid", help="ID of the video deposit", default=None)
@with_appcontext
def missing(recid, depid):
    """Create missing subformats given a record id or deposit id."""
    if not recid and not depid:
        raise ClickException('Missing option "--recid" or "--depid"')

    value = recid or depid
    type_ = "recid" if recid else "depid"
    output = create_all_missing_subformats(id_type=type_, id_value=value)
    if output:
        click.echo("Creating the following subformats: {0}".format(output))
    else:
        click.echo("No missing subformats to create.")


@subformats.group()
def recreate():
    """Recreate subformats for a video."""


@recreate.command()
@click.argument("quality")
@click.option("--recid", "recid", help="ID of the video record", default=None)
@click.option("--depid", "depid", help="ID of the video deposit", default=None)
@with_appcontext
def quality(recid, depid, quality):
    """Recreate subformat for the given quality."""
    if not recid and not depid:
        raise ClickException('Missing option "--recid" or "--depid"')

    value = recid or depid
    type_ = "recid" if recid else "depid"

    qualities = list(current_app.config["CDS_OPENCAST_QUALITIES"].keys())
    if quality not in qualities:
        raise ClickException("Input quality must be one of {0}".format(qualities))

    output = create_subformat(id_type=type_, id_value=value, quality=quality)
    if output:
        click.echo("Creating the following subformat: {0}.".format(output))
    else:
        click.echo("This subformat cannot be transcoded.")


@recreate.command()
@click.option("--recid", "recid", help="ID of the video record", default=None)
@click.option("--depid", "depid", help="ID of the video deposit", default=None)
@click.option(
    "--yes",
    is_flag=True,
    callback=abort_if_false,
    expose_value=False,
    prompt="Do you really want to recreate all subformats?",
)
@with_appcontext
def all(recid, depid):
    """Recreate all subformats."""
    if not recid and not depid:
        raise ClickException('Missing option "--recid" or "--depid"')

    value = recid or depid
    type_ = "recid" if recid else "depid"

    output = create_all_subformats(id_type=type_, id_value=value)
    click.echo("Creating the following subformats: {0}.".format(output))


@click.group()
def videos():
    """Videos deposit command line utilities."""


@videos.command()
@click.option("--recid", "recid", help="ID of the video record", default=None)
@with_appcontext
def fix_bucket_conflict(recid):
    """Create missing subformats given a record id or deposit id."""

    def _force_sync_deposit_bucket(record):
        """Replace deposit bucket with a copy of the record bucket."""
        deposit = Video.get_record(record.depid.object_uuid)
        deposit_old_bucket = deposit.files.bucket
        # create a copy of record bucket
        new_bucket = record.files.bucket.snapshot()
        new_bucket.locked = False
        db.session.commit()
        rb = RecordsBuckets.query.filter(
            RecordsBuckets.bucket_id == deposit_old_bucket.id
        ).one()
        rb.bucket = new_bucket
        db.session.add(rb)
        db.session.commit()

        # Put tags correctly pointing to the right object
        master_file = CDSVideosFilesIterator.get_master_video_file(record)
        if master_file:
            master_deposit_obj = ObjectVersion.get(new_bucket, master_file["key"])

            for slave in (
                ObjectVersion.get_by_bucket(bucket=new_bucket)
                .join(ObjectVersion.tags)
                .filter(
                    ObjectVersion.file_id.isnot(None),
                    ObjectVersionTag.key == "master",
                )
            ):
                ObjectVersionTag.create_or_update(
                    slave, "master", str(master_deposit_obj.version_id)
                )
                db.session.add(slave)
                db.session.commit()

        # Delete the old bucket
        deposit_old_bucket.locked = False
        _ = deposit_old_bucket.remove()

        deposit["_buckets"]["deposit"] = str(new_bucket.id)
        record["_buckets"]["deposit"] = str(new_bucket.id)
        record["_deposit"] = deposit["_deposit"]
        deposit["_files"] = deposit.files.dumps()
        deposit.commit()
        record.commit()
        db.session.commit()

        return deposit_old_bucket.id, new_bucket.id

    if not recid:
        raise ClickException('Missing option "--recid"')

    pid, record = record_resolver.resolve(recid)
    old_bucket_id, new_bucket_id = _force_sync_deposit_bucket(record)

    click.echo(
        "Deposit bucket re-created from record bucket. Old bucket id: {0} - "
        "New bucket id: {1}".format(old_bucket_id, new_bucket_id)
    )


@videos.command()
@click.option("--recid", "recid", help="ID of the video record", default=None)
@click.option("--depid", "depid", help="ID of the video deposit", default=None)
@with_appcontext
def extract_frames(recid, depid):
    """Re-trigger the extract frames task."""
    if not recid and not depid:
        raise ClickException('Missing option "--recid" or "--depid"')

    if recid:
        _, record = record_resolver.resolve(recid)
        depid = record["_deposit"]["id"]

    flow_metadata = FlowMetadata.get_by_deposit(depid)
    assert flow_metadata, "Cannot find Flow for given deposit id {0}".format(depid)

    payload = flow_metadata.payload
    payload = dict(
        deposit_id=payload["deposit_id"],
        flow_id=payload["flow_id"],
        key=payload["key"],
        version_id=payload["version_id"],
    )

    ExtractFramesTask.create_flow_tasks(payload)
    db.session.commit()

    ExtractFramesTask().s(**payload).apply_async()

    db.session.commit()
    index_deposit_project(payload["deposit_id"])


@videos.command()
@click.option("--recid", "recid", help="ID of the video record", default=None, required=True)
@with_appcontext
def create_doi(recid):
    """Mints the DOI for a video record."""
    # Get the video object with recid
    _, record = record_resolver.resolve(recid)
    depid = record.depid
    video_deposit = Video.get_record(depid.object_uuid)

    try:
        # Mint the doi and publish
        edit_record = video_deposit.edit().mint_doi().publish().commit()
        # Save changes
        db.session.commit()

        # Index the record again
        _, record_video = edit_record.fetch_published()
        RecordIndexer().index(record_video)
        click.echo(f"DOI minted, and indexed successfully for record '{recid}'")

        # Register the doi to datacite 
        datacite_register(recid, str(record_video.id))
    
    except DataCiteError as dexc:
        raise ClickException(f"Failed to register DOI on datacite for the video record '{recid}': {str(dexc)}")
    except Exception as exc:
        db.session.rollback()
        raise ClickException(f"Failed to mint DOI for the video record '{recid}': {str(exc)}")