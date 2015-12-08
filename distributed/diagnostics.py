import logging
import sys
import threading
import time
from timeit import default_timer

import dask
from toolz import valmap, groupby
from tornado.ioloop import PeriodicCallback, IOLoop

from .utils import ignoring, sync
from .executor import default_executor


logger = logging.getLogger(__name__)


class SchedulerPlugin(object):
    """ Interface to plug into Scheduler

    The scheduler responds to externally driven events.  As changes within the
    system occur the scheduler calls various functions like ``task_finished``,
    ``update_graph``, ``task_erred``, etc..  A ``SchedulerPlugin`` allows user
    driven code to extend those events.

    By implementing these methods within a custom plugin and registering that
    plugin with the scheduler, we run user code within the scheduler thread
    that can perform arbitrary operations in synchrony with the scheduler
    itself.

    Examples
    --------
    >>> class Counter(SchedulerPlugin):
    ...     def __init__(self):
    ...         self.counter = 0
    ...
    ...     def task_finished(self, scheduler, key, worker, nbytes):
    ...         self.counter += 1

    >>> c = Counter()
    >>> scheduler.add_plugin(c)  # doctest: +SKIP
    """
    def start(self, scheduler):
        pass

    def task_finished(self, scheduler, key, worker, nbytes):
        pass

    def update_graph(self, scheduler, dsk, keys, restrictions):
        pass

    def task_erred(self, scheduler, key, worker, exception):
        pass


def dependent_keys(keys, who_has, processing, stacks, dependencies, complete=False):
    """ All keys that need to compute for these keys to finish """
    out = set()
    stack = list(keys)
    while stack:
        key = stack.pop()
        if key in out:
            continue
        if not complete and (who_has.get(key) or
                             key in processing or
                             key in stacks):
            continue

        out.add(key)
        stack.extend(dependencies.get(key, []))
    return out


class Progress(SchedulerPlugin):
    """ Tracks progress of a set of keys or futures """
    def __init__(self, keys, scheduler=None, minimum=0, dt=0.1):
        keys = {k.key if hasattr(k, 'key') else k for k in keys}

        if scheduler is None:
            executor = default_executor()
            if executor is None:
                raise ValueError("Can not find scheduler.\n"
                 "Either create an executor or supply scheduler as an argument")
            else:
                scheduler = default_executor().scheduler

        start = time.time()
        while not keys.issubset(scheduler.dask):
            time.sleep(0.01)
            if time.time() > start + 1:
                raise ValueError("Keys not found: %s" %
                                 str(keys - scheduler.in_play))

        self._start_time = default_timer()

        self._minimum = minimum
        self._dt = dt
        self.last_duration = 0
        self.keys = None

        self._running = False

        self.scheduler = scheduler

        def f():
            scheduler.add_diagnostic(self)  # subtle race condition here
            self.keys = dependent_keys(keys, scheduler.who_has,
                    scheduler.processing, scheduler.stacks, scheduler.waiting)
            self.all_keys = self.keys.copy()
            self.all_keys.update(keys)

        if scheduler.loop._thread_ident == threading.current_thread().ident:
            f()
        else:
            sync(scheduler.loop, f)

        self.status = None

    def start(self):
        self._start()
        if not self.keys:
            self.stop()
        elif all(k in self.scheduler.exceptions_blame for k in self.keys):
            self.stop(True)

    def _start(self):
        pass

    def task_finished(self, scheduler, key, worker, nbytes):
        if key in self.keys:
            self.keys.remove(key)

        if not self.keys:
            self.stop()

    def task_erred(self, scheduler, key, worker, exception):
        logger.info("Progress sees task erred")
        if key in self.all_keys:
            self.stop(exception=exception, key=key)

    def stop(self, exception=None, key=None):
        if self in self.scheduler.diagnostics:
            self.scheduler.diagnostics.remove(self)
        if exception:
            self.status = 'error'
        else:
            self.status = 'finished'

    @property
    def elapsed(self):
        return default_timer() - self._start_time


def key_split(s):
    """
    >>> key_split('x-1')
    'x'
    >>> key_split('x-1-2-3')
    'x-1-2'
    >>> key_split(('x-2', 1))
    'x'
    >>> key_split(None)
    'Other'
    """
    if isinstance(s, tuple):
        return key_split(s[0])
    try:
        return s.rsplit('-', 1)[0]
    except:
        return 'Other'


class MultiProgress(Progress):
    """ Progress variant that keeps track of different groups of keys

    See Progress for most details.  This only adds a function ``func=``
    that splits keys.  This defaults to ``key_split`` which aligns with naming
    conventions chosen in the dask project (tuples, hyphens, etc..)

    Examples
    --------
    >>> split = lambda s: s.split('-')[0]
    >>> p = MultiProgress(['y-2'], func=split)  # doctest: +SKIP
    >>> p.keys   # doctest: +SKIP
    {'x': {'x-1', 'x-2', 'x-3'},
     'y': {'y-1', 'y-2'}}
    """
    def __init__(self, keys, scheduler=None, func=key_split, minimum=0, dt=0.1):
        Progress.__init__(self, keys, scheduler, minimum, dt)
        self.func = func
        self.keys = valmap(set, groupby(self.func, self.keys))
        self.all_keys = valmap(set, groupby(self.func, self.all_keys))

    def task_finished(self, scheduler, key, worker, nbytes):
        s = self.keys.get(self.func(key), None)
        if s and key in s:
            s.remove(key)

        if not self.keys or not any(self.keys.values()):
            self.stop()

    def task_erred(self, scheduler, key, worker, exception):
        logger.info("Progress sees task erred")

        if (self.func(key) in self.all_keys and
            key in self.all_keys[self.func(key)]):
            self.stop(exception=exception, key=key)


class TextProgressBar(Progress):
    def __init__(self, keys, scheduler=None, minimum=0, dt=0.1, width=40):
        Progress.__init__(self, keys, scheduler, minimum, dt)
        self._width = width
        self._timer = None

    def _start(self):
        if not self._running:
            # Start background thread
            self._running = True
            self._timer = threading.Thread(target=self._timer_func)
            self._timer.daemon = True
            self._timer.start()

        self._update()

    def stop(self, exception=None, key=None):
        Progress.stop(self, exception, key=None)
        self._update()
        if self._running:
            self._running = False
            self._timer.join()
            self.last_duration = self.elapsed
            if self.last_duration < self._minimum:
                return
            else:
                self._update()
            sys.stdout.write('\n')
            sys.stdout.flush()

    def _timer_func(self):
        """Background thread for updating the progress bar"""
        while self._running:
            if self.elapsed > self._minimum:
                self._update()
            time.sleep(self._dt)

    def _update(self):
        ntasks = len(self.all_keys)
        ndone = ntasks - len(self.keys)
        self._draw_bar(ndone / ntasks if ntasks else 1.0, self.elapsed)

    def _draw_bar(self, frac, elapsed):
        bar = '#' * int(self._width * frac)
        percent = int(100 * frac)
        elapsed = format_time(elapsed)
        msg = '\r[{0:<{1}}] | {2}% Completed | {3}'.format(bar, self._width,
                                                           percent, elapsed)
        with ignoring(ValueError):
            sys.stdout.write(msg)
            sys.stdout.flush()


class ProgressWidget(Progress):
    def __init__(self, keys, scheduler=None, minimum=0, dt=0.1):
        Progress.__init__(self, keys, scheduler, minimum, dt)
        from ipywidgets import FloatProgress
        self.bar = FloatProgress(min=0, max=1, description='0.0s')
        self.widget = self.bar
        from zmq.eventloop.ioloop import IOLoop
        loop = IOLoop.instance()
        self.pc = PeriodicCallback(self._update, 1000 * self._dt, io_loop=loop)

    def _start(self):
        from IPython.display import display
        display(self.widget)
        self._update()
        self.pc.start()

    def stop(self, exception=None, key=None):
        Progress.stop(self, exception, key=None)
        self.pc.stop()
        if exception:
            self.bar.bar_style = 'danger'
        elif not self.keys:
            self.bar.bar_style = 'success'
        self._update()

    def _update(self):
        ntasks = len(self.all_keys)
        ndone = ntasks - len(self.keys)
        self.bar.value = ndone / ntasks if ntasks else 1.0
        self.bar.description = format_time(self.elapsed)


class MultiProgressWidget(MultiProgress):
    def __init__(self, keys, scheduler=None, minimum=0, dt=0.1, func=key_split):
        MultiProgress.__init__(self, keys, scheduler, func, minimum, dt)
        from ipywidgets import FloatProgress, VBox, HTML, HBox
        self.bars = {key: FloatProgress(min=0, max=1, description=key)
                        for key in self.keys}
        self.texts = {key: HTML() for key in self.keys}
        self.boxes = {key: HBox([self.bars[key], self.texts[key]])
                        for key in self.keys}
        self.time = HTML()
        self.widget = HBox([self.time, VBox([self.boxes[key] for key in
                                            sorted(self.bars, key=str)])])
        from tornado.ioloop import IOLoop
        loop = IOLoop.instance()
        self.pc = PeriodicCallback(self._update, 1000 * self._dt, io_loop=loop)

    start = ProgressWidget.start

    def stop(self, exception=None, key=None):
        self.pc.stop()
        Progress.stop(self, exception)
        self._update()
        for k, v in self.keys.items():
            if not v:
                self.bars[k].bar_style = 'success'
        if exception:
            self.bars[self.func(key)].value = 1
            self.bars[self.func(key)].bar_style = 'danger'

    def _update(self):
        for k in self.keys:
            ntasks = len(self.all_keys[k])
            ndone = ntasks - len(self.keys[k])
            self.bars[k].value = ndone / ntasks if ntasks else 1.0
            self.texts[k].value = "%d / %d" % (ndone, ntasks)
            self.time.value = format_time(self.elapsed)


def progress(*futures, notebook=None, multi=False):
    """ Track progress of futures

    This operates differently in the notebook and the console

    *  Notebook:  This returns immediately, leaving an IPython widget on screen
    *  Console:  This blocks until the computation completes

    Examples
    --------
    >>> progress(futures)  # doctest: +SKIP
    [########################################] | 100% Completed |  1.7s
    """
    futures = list(dask.core.flatten(list(futures)))
    if not isinstance(futures, (set, list)):
        futures = [futures]
    if notebook is None:
        notebook = is_kernel()  # often but not always correct assumption
    if notebook:
        if multi:
            bar = MultiProgressWidget(futures)
        else:
            bar = ProgressWidget(futures)
        bar.start()
    else:
        bar = TextProgressBar(futures)
        bar.start()
        bar._timer.join()


def is_kernel():
    # http://stackoverflow.com/questions/34091701/determine-if-were-in-an-ipython-notebook-session
    if 'IPython' not in sys.modules:
        # IPython hasn't been imported, definitely not
        return False
    from IPython import get_ipython
    # check for `kernel` attribute on the IPython instance
    return getattr(get_ipython(), 'kernel', None) is not None



def format_time(t):
    """Format seconds into a human readable form.

    >>> format_time(10.4)
    '10.4s'
    >>> format_time(1000.4)
    '16min 40.4s'
    """
    m, s = divmod(t, 60)
    h, m = divmod(m, 60)
    if h:
        return '{0:2.0f}hr {1:2.0f}min {2:4.1f}s'.format(h, m, s)
    elif m:
        return '{0:2.0f}min {1:4.1f}s'.format(m, s)
    else:
        return '{0:4.1f}s'.format(s)
