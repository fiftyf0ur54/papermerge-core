import logging

from channels.layers import get_channel_layer
from pathlib import Path
from asgiref.sync import async_to_sync

from django.db.models.signals import post_save, post_delete, pre_delete
from celery.signals import (
    task_received,
    task_postrun,
    task_prerun,
    heartbeat_sent,
    worker_ready,
    worker_shutdown
)

from django.dispatch import receiver
from django.conf import settings

from papermerge.core.models import (
    Document,
    Folder,
    User,
)
from papermerge.core.storage import get_storage_instance
from .tasks import delete_user_data as delete_user_data_task

logger = logging.getLogger(__name__)

# Tasks that need to notify websocket clients
MONITORED_TASKS = (
    'papermerge.core.tasks.ocr_document_task',
)

HEARTBEAT_FILE = Path("/tmp/worker_heartbeat")
READINESS_FILE = Path("/tmp/worker_ready")


@receiver(pre_delete, sender=Document)
def delete_files(sender, instance: Document, **kwargs):
    """
    Deletes physical (e.g. pdf) file associated
    with given (Document) instance.

    More exactly it will delete whatever it is inside
    associated folder in which original file was saved
    (e.g. all preview images).
    """
    for document_version in instance.versions.all():
        try:
            get_storage_instance().delete_doc(
                document_version.document_path
            )
        except IOError as error:
            logger.error(
                f"Error deleting associated file for document.pk={instance.pk}"
                f" {error}"
            )


@receiver(post_delete, sender=User)
def delete_user_data(sender, instance, **kwargs):
    """Deletes associated user folder(s) under media root"""
    delete_user_data_task.delay(str(instance.pk))


@receiver(post_save, sender=Document)
def inherit_metadata_keys(sender, instance, created, **kwargs):
    """
    When moved into new folder, documents will inherit their parent
    metadata keys
    """
    pass
    # if doc has a parent
    # if instance.parent:
    #    instance.inherit_kv_from(instance.parent)
    #    for page in instance.pages.all():
    #        page.inherit_kv_from(instance.parent)
    # else:
    #    for page in instance.pages.all():
    #        page.inherit_kv_from(instance)


@receiver(post_save, sender=Folder)
def inherit_metadata_keys_from_parent(sender, instance, created, **kwargs):
    """
    When created or moved folders will inherit metadata keys from their
    parent.
    """
    # if folder was just created and has a parent
    if created and instance.parent:
        instance.inherit_kv_from(instance.parent)


@receiver(post_save, sender=User)
def user_init(sender, instance, created, **kwargs):
    """
    Signal sent when user model is saved
    (create=True if user was actually created).
    Create user specific data when user is initially created
    """
    if created:
        if settings.PAPERMERGE_CREATE_SPECIAL_FOLDERS:
            instance.create_special_folders()


@receiver([post_delete, post_save], sender=Document)
@receiver([post_delete, post_save], sender=Folder)
def if_inbox_then_refresh(sender, instance, **kwargs):
    """
    Inform inbox_refresh channel group that user's inbox was updated
    """
    # Folder or Document instance was deleted/moved from//to user's Inbox folder
    try:
        instance.refresh_from_db()
    except (Document.DoesNotExist, Folder.DoesNotExist):
        logger.warning('Too late - Document/Folder was already deleted')
        return

    try:
        if instance.parent and instance.parent.title == Folder.INBOX_TITLE:
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                "inbox_refresh",
                {"type": "inbox.refresh", "user_id": str(instance.user.pk)}
            )
    except Exception as ex:
        logger.warning(ex, exc_info=True)


def get_channel_data(task_name, type):

    if task_name == 'papermerge.core.tasks.ocr_document_task':
        return {
            'type': f"ocrdocumenttask.{type}"
        }
    else:
        raise ValueError(f"Task name not in {MONITORED_TASKS}")


def channel_group_notify(task_name, task_kwargs, type):
    """
    Send group notification to the channel
    """
    channel_layer = get_channel_layer()
    channel_data = get_channel_data(task_name, type)

    channel_data.update(task_kwargs)
    task_short_name = task_name.split('.')[-1]

    logger.debug(
        f"channel_group_notify {task_short_name} {channel_data}"
    )
    async_to_sync(
        channel_layer.group_send
    )(
        task_short_name, channel_data
    )


@task_prerun.connect
def channel_group_notify_task_prerun(sender=None, **kwargs):
    if sender:
        if sender.name in MONITORED_TASKS:
            channel_group_notify(
                task_name=sender.name,
                task_kwargs=kwargs['kwargs'],
                type='taskstarted'
            )


@task_received.connect
def channel_group_notify_task_received(sender=None, **kwargs):
    request = kwargs.get('request')
    if request:
        if request.name in MONITORED_TASKS:
            channel_group_notify(
                task_name=request.name,
                task_kwargs=request.kwargs,
                type='taskreceived'
            )


@task_postrun.connect
def channel_group_notify_task_postrun(sender=None, **kwargs):
    if sender:
        if sender.name in MONITORED_TASKS:
            state = kwargs['state']
            if state == 'SUCCESS':
                type = 'tasksucceeded'
            else:
                type = 'taskfailed'

            channel_group_notify(
                task_name=sender.name,
                task_kwargs=kwargs['kwargs'],
                type=type
            )


@heartbeat_sent.connect
def heartbeat(**_):
    # liveness probe for celery worker
    # https://github.com/celery/celery/issues/4079
    HEARTBEAT_FILE.touch()


@worker_ready.connect
def worker_ready(**_):
    # readyness probe for celery worker
    # https://github.com/celery/celery/issues/4079
    READINESS_FILE.touch()


@worker_shutdown.connect
def worker_shutdown(**_):
    # https://github.com/celery/celery/issues/4079
    for file in (HEARTBEAT_FILE, READINESS_FILE):
        if file.is_file():
            file.unlink()
