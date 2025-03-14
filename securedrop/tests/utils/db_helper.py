"""Testing utilities that involve database (and often related
filesystem) interaction.
"""

import datetime
import io
import math
import os
import random
import subprocess
from pathlib import Path
from typing import Dict, List

from db import db
from encryption import EncryptionManager
from journalist_app.utils import mark_seen
from models import Journalist, Reply, SeenReply, Submission
from passphrases import PassphraseGenerator
from source_user import create_source_user
from store import Storage


def init_journalist(first_name=None, last_name=None, is_admin=False):
    """Initialize a journalist into the database. Return their
    :class:`Journalist` object and password string.

    :param bool is_admin: Whether the user is an admin.

    :returns: A 2-tuple. The first entry, an :obj:`Journalist`
              corresponding to the row just added to the database. The
              second, their password string.
    """
    username = PassphraseGenerator.get_default().generate_passphrase()
    user_pw = PassphraseGenerator.get_default().generate_passphrase()
    user = Journalist(
        username=username,
        password=user_pw,
        first_name=first_name,
        last_name=last_name,
        is_admin=is_admin,
    )
    db.session.add(user)
    db.session.commit()
    return user, user_pw


def delete_journalist(journalist):
    """Deletes a journalist from the database.

    :param Journalist journalist: The journalist to delete

    :returns: None
    """
    journalist.delete()
    db.session.commit()


def reply(storage, journalist, source, num_replies):
    """Generates and submits *num_replies* replies to *source*
    from *journalist*. Returns reply objects as a list.

    :param Journalist journalist: The journalist to write the
                                     reply from.

    :param Source source: The source to send the reply to.

    :param int num_replies: Number of random-data replies to make.

    :returns: A list of the :class:`Reply`s submitted.
    """
    assert num_replies >= 1
    replies = []
    for _ in range(num_replies):
        source.interaction_count += 1
        fname = f"{source.interaction_count}-{source.journalist_filename}-reply.gpg"

        EncryptionManager.get_default().encrypt_journalist_reply(
            for_source=source,
            reply_in=str(os.urandom(1)),
            encrypted_reply_path_out=storage.path(source.filesystem_id, fname),
        )

        reply = Reply(journalist, source, fname, storage)
        replies.append(reply)
        db.session.add(reply)
        seen_reply = SeenReply(reply=reply, journalist=journalist)
        db.session.add(seen_reply)

    db.session.commit()
    return replies


def init_source(storage):
    """Initialize a source: create their database record, the
    filesystem directory that stores their submissions & replies,
    and their GPG key encrypted with their codename. Return a source
    object and their codename string.

    :returns: A 2-tuple. The first entry, the :class:`Source`
    initialized. The second, their codename string.
    """
    passphrase = PassphraseGenerator.get_default().generate_passphrase()
    source_user = create_source_user(
        db_session=db.session,
        source_passphrase=passphrase,
        source_app_storage=storage,
    )
    return source_user.get_db_record(), passphrase


def submit(storage, source, num_submissions, submission_type="message"):
    """Generates and submits *num_submissions*
    :class:`Submission`s on behalf of a :class:`Source`
    *source*.

    :param Storage storage: the Storage object to use.

    :param Source source: The source on who's behalf to make
                             submissions.

    :param int num_submissions: Number of random-data submissions
                                to make.

    :returns: A list of the :class:`Submission`s submitted.
    """
    assert num_submissions >= 1
    source.last_updated = datetime.datetime.utcnow()
    db.session.add(source)
    submissions = []
    for _ in range(num_submissions):
        source.interaction_count += 1
        source.pending = False
        if submission_type == "file":
            fpath = storage.save_file_submission(
                source.filesystem_id,
                source.interaction_count,
                source.journalist_filename,
                "pipe.txt",
                io.BytesIO(b"Ceci n'est pas une pipe."),
            )
        else:
            fpath = storage.save_message_submission(
                source.filesystem_id,
                source.interaction_count,
                source.journalist_filename,
                str(os.urandom(1)),
            )
        submission = Submission(source, fpath, storage)
        submissions.append(submission)
        db.session.add(source)
        db.session.add(submission)

    db.session.commit()
    return submissions


def new_codename(client, session):
    """Helper function to go through the "generate codename" flow."""
    client.post("/generate", data={"tor2web_check": 'href="fake.onion"'})
    tab_id, codename = next(iter(session["codenames"].items()))
    client.post("/create", data={"tab_id": tab_id})
    return codename


def bulk_setup_for_seen_only(journo: Journalist, storage: Storage) -> List[Dict]:
    """
    Create some sources with some seen submissions that are not marked as 'downloaded' in the
    database and some seen replies from journo.
    """

    setup_collection = []

    for _i in range(random.randint(2, 4)):
        collection = {}

        source, _ = init_source(storage)

        submissions = submit(storage, source, random.randint(2, 4))
        half = math.ceil(len(submissions) / 2)
        messages = submissions[half:]
        files = submissions[:half]
        replies = reply(storage, journo, source, random.randint(1, 3))

        seen_files = random.sample(files, math.ceil(len(files) / 2))
        seen_messages = random.sample(messages, math.ceil(len(messages) / 2))
        seen_replies = random.sample(replies, math.ceil(len(replies) / 2))

        mark_seen(seen_files, journo)
        mark_seen(seen_messages, journo)
        mark_seen(seen_replies, journo)

        unseen_files = list(set(files).difference(set(seen_files)))
        unseen_messages = list(set(messages).difference(set(seen_messages)))
        unseen_replies = list(set(replies).difference(set(seen_replies)))
        not_downloaded = list(set(files + messages).difference(set(seen_files + seen_messages)))

        collection["source"] = source
        collection["seen_files"] = seen_files
        collection["seen_messages"] = seen_messages
        collection["seen_replies"] = seen_replies
        collection["unseen_files"] = unseen_files
        collection["unseen_messages"] = unseen_messages
        collection["unseen_replies"] = unseen_replies
        collection["not_downloaded"] = not_downloaded

        setup_collection.append(collection)

    return setup_collection


def reset_database(database_file: Path) -> None:
    database_file.unlink(missing_ok=True)  # type: ignore
    database_file.touch()
    subprocess.check_call(["sqlite3", database_file, ".databases"])
