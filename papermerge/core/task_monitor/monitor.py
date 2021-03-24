import logging


from .query_set import QuerySet
from .task import Task


logger = logging.getLogger(__name__)


class Monitor:
    """
    Monitors celery task states based on incoming events.

    papermerge.avenues is basically a django channels based app.

    Celery does not provide a convinient task monitoring API, it just
    blindly saves tasks' metadata.
    This class is ment to fill that gap. It conviniently saves
    events information and provides a handy API to work with
    saved tasks.

    Example of usage:

    monitor = Monitor(store=RedisStore())

    # Monitor celery task with specified name

    monitor.add_condition(
        name='papermerge.core.tasks.ocr_page',
        attr=[  # extract this kwargs from the task
            'user_id',
            'document_id',
            'lang'
        ]

    # Get a QuerySet for 'papermerge.core.tasks.ocr_page' tasks
    # so that saved tasks can queries in similar way to django models are
    ocr_page_qs = monitor['papermerge.core.tasks.ocr_page']

    # total count of ocr_page tasks
    ocr_page_qs.count()

    # total count of ocr_page tasks executed specifically for DOC_ID
    ocr_page_qs.find(document_id=DOC_ID).count()

    # iterage over all tasks for document ID which are still active
    # i.e. their stage is one of:
    # * task-sent
    # * task-received
    # * task-started
    for task in ocr_page_qs.find(document_id=DOC_ID).active():
        print(task['page_num'], task['state'])

    """

    def __init__(self, store, prefix="task-monitor"):
        # redis store
        self.store = store
        self.prefix = prefix
        # A list of monitored tasks
        self._tasks = []
        # callback to invoke when new event arrived
        self.callback = None

    def add_task(self, task):
        self._tasks.append(task)

    def set_callback(self, callback):
        self.callback = callback

    def get_key(self, event):

        task_id = event['uuid']
        key = f"{self.prefix}-{task_id}"

        return key

    def save_event(self, event):
        task = self.get_task_from(event)

        updated_task_dict = self.update(event, task)

        if self.is_monitored_task(updated_task_dict['task_name']):
            self.callback(updated_task_dict)

    def update(self, event, task):
        """
        Merge new attributes into existing task key
        """
        key = self.get_key(event)
        found_attr = self.store.get(key, {})
        found_attr.update(dict(task))

        if len(found_attr) > 0:
            self.store[key] = found_attr
            self.store.expire(key)

        return found_attr

    def get_task_from(self, event):
        """
        Given even object (which is a dictionary)
        return a task object
        """
        task = None
        task_name = event.get('name', None)

        if self.is_monitored_task(task_name):
            task = self.get_task(task_name)
            task.update(event.get('kwargs', None))
            task['type'] = event.get('type', None)

        if not task:
            task = Task(task_name, type=event.get('type', None))

        return task

    def get_task(self, task_name):
        for task in self._tasks:
            if task == task_name:
                return task

        return None

    def is_monitored_task(self, task_name):
        for task in self._tasks:
            if task == task_name:
                return True

        return False

    def __getitem__(self, task_name):
        """
        Returns an iterator over saved tasks with specified name
        """
        return QuerySet(task_name=task_name)
