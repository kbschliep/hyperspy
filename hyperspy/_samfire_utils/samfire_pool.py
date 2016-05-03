# -*- coding: utf-8 -*-
# Copyright 2007-2016 The HyperSpy developers
#
# This file is part of  HyperSpy.
#
#  HyperSpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
#  HyperSpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with  HyperSpy.  If not, see <http://www.gnu.org/licenses/>.


import time
import logging
from multiprocessing import Manager
from ipyparallel import Reference as ipp_Reference
import numpy as np

from hyperspy.utils.parallel_pool import ParallelPool
from hyperspy._samfire_utils.samfire_worker import create_worker

_logger = logging.getLogger(__name__)


class SamfirePool(ParallelPool):

    def __init__(self, **kwargs):
        super(SamfirePool, self).__init__(**kwargs)
        self.samf = None
        self.ping = {}
        self.pid = {}
        self.workers = {}
        self.rworker = None
        self.result_queue = None
        self.shared_queue = None
        self._last_time = 0

    def _timestep_set(self, value):
        value = np.abs(value)
        self._timestep = value
        if self.has_pool and self.is_multiprocessing:
            for this_queue in self.workers.values():
                this_queue.put(('change_timestep', (value,)))

    def prepare_workers(self, samfire):
        self.samf = samfire

        mall = samfire.model
        model = mall.inav[mall.axes_manager.indices]
        model.store('z')
        m_dict = model.spectrum._to_dictionary(False)
        m_dict['models'] = model.spectrum.models._models.as_dictionary()

        optional_names = {mall[c].name for c in samfire.optional_components}

        if self.is_ipyparallel:
            direct_view = self.pool.client[:self.num_workers]
            direct_view.block = True
            direct_view.execute("from hyperspy._samfire_utils.samfire_worker"
                                " import create_worker")
            direct_view.scatter('identity', range(self.num_workers),
                                flatten=True)
            direct_view.execute('worker = create_worker(identity)')
            self.rworker = ipp_Reference('worker')
            direct_view.apply(lambda worker, m_dict:
                              worker.create_model(m_dict, 'z'), self.rworker,
                              m_dict)
            direct_view.apply(lambda worker, ts: worker.setup_test(ts),
                              self.rworker, samfire._gt_dump)
            direct_view.apply(lambda worker, on: worker.set_optional_names(on),
                              self.rworker, optional_names)

        if self.is_multiprocessing:
            manager = Manager()
            self.shared_queue = manager.Queue()
            self.result_queue = manager.Queue()
            for i in range(self.num_workers):
                this_queue = manager.Queue()
                self.workers[i] = this_queue
                this_queue.put(('setup_test', (samfire._gt_dump,)))
                this_queue.put(('create_model', (m_dict, 'z')))
                this_queue.put(('set_optional_names', (optional_names,)))
                self.pool.apply_async(create_worker, args=(i, this_queue,
                                                           self.shared_queue,
                                                           self.result_queue))

    def update_optional_names(self):
        samfire = self.samf
        optional_names = {samfire.model[c].name for c in
                          samfire.optional_components}
        if self.is_multiprocessing:
            for this_queue in self.workers.values():
                this_queue.put(('set_optional_names', (optional_names,)))
        elif self.is_ipyparallel:
            direct_view = self.pool.client[:self.num_workers]
            direct_view.block = True
            direct_view.apply(lambda worker, on: worker.set_optional_names(on),
                              self.rworker, optional_names)

    def ping_workers(self, timeout=None):
        if self.samf is None:
            _logger.error('Have to add samfire to the pool first')
        else:
            if self.is_multiprocessing:
                for _id, this_queue in self.workers.items():
                    this_queue.put('ping')
                    self.ping[_id] = time.time()
            elif self.is_ipyparallel:
                for i in range(self.num_workers):
                    direct_view = self.pool.client[i]
                    self.results.append((direct_view.apply_async(lambda worker:
                                                                 worker.ping(),
                                                                 self.rworker),
                                         i))
                    self.ping[i] = time.time()
        time.sleep(0.5)
        self.collect_results(timeout)

    def __len__(self):
        if self.is_ipyparallel:
            return self.pool.client.queue_status()['unassigned']
        elif self.is_multiprocessing:
            return self.shared_queue.qsize()

    def add_jobs(self, needed_number=None):
        if needed_number is None:
            needed_number = self.need_pixels
        for ind, value_dict in self.samf._add_jobs(needed_number):
            if self.is_multiprocessing:
                self.shared_queue.put(('test', (ind, value_dict)))
            elif self.is_ipyparallel:
                def test_func(worker, ind, value_dict):
                    return worker.test(ind, value_dict)
                self.results.append((self.pool.apply_async(test_func,
                                                           self.rworker, ind,
                                                           value_dict), ind))

    def parse(self, value):
        if value is None:
            keyword = 'Failed'
        else:
            keyword, the_rest = value
        samf = self.samf
        if keyword == 'pong':
            _id, pid, pong_time, message = the_rest
            self.ping[_id] = pong_time - self.ping[_id]
            self.pid[_id] = pid
            _logger.info('pong worker %s with time %g and message'
                         '"%s"' % (str(_id), self.ping[_id], message))
        elif keyword == 'Error':
            _id, err_message = the_rest
            _logger.error('Error in worker %s\n%s' % (str(_id), err_message))
        elif keyword == 'result':
            _id, ind, result, isgood = the_rest
            if ind in samf._running_pixels:
                samf._running_pixels.remove(ind)
                samf._update(ind, result, isgood)
                samf._plot()
                samf._save()
                if hasattr(samf, '_log') and isinstance(samf._log, list):
                    samf._log.append((ind, isgood, samf.count, _id))
        else:
            _logger.error('Unusual return from some worker. The value '
                          'is:\n%s' % str(value))

    def collect_results(self, timeout=None):
        if timeout is None:
            timeout = self.timeout
        found_something = False
        if self.is_ipyparallel:
            for res, ind in reversed(self.results):
                if res.ready():
                    try:
                        result = res.get(timeout=timeout)
                    except TimeoutError:
                        _logger.info('Ind {} failed to come back in {} '
                                     'seconds. Assuming failed'.format(
                                         ind, timeout))
                        result = ('result', (-1, ind, None, False))
                    self.parse(result)
                    self.results.remove((res, ind))
                    found_something = True
                else:
                    pass
        elif self.is_multiprocessing:
            while not self.result_queue.empty():
                try:
                    result = self.result_queue.get(block=True,
                                                   timeout=timeout)
                    self.parse(result)
                    found_something = True
                except TimeoutError:
                    _logger.info('Some ind failed to come back in {} '
                                 'seconds.'.format(self.timeout))
        return found_something

    @property
    def need_pixels(self):
        return min(self.samf.pixels_done * self.samf.metadata.marker.ndim,
                   self.num_workers - len(self))

    @property
    def _not_too_long(self):
        if not hasattr(self, '_last_time') or not isinstance(self._last_time,
                                                             float):
            self._last_time = time.time()
        return (time.time() - self._last_time) <= self.timeout

    def run(self):
        while self._not_too_long and (self.samf.pixels_left or
                                      self.samf.running_pixels):
            # bool if got something
            new_result = self.collect_results()
            need_number = self.need_pixels

            if need_number:
                self.add_jobs(need_number)
            if not need_number or not new_result:
                # did not spend much time, since no new results or added pixels
                self.sleep()
            else:
                self._last_time = time.time()

    def stop(self):
        if self.is_multiprocessing:
            for queue in self.workers.values():
                queue.put('stop_listening')
            self.pool.close()
            # self.pool.terminate()
        elif self.is_ipyparallel:
            self.pool.client.clear()
