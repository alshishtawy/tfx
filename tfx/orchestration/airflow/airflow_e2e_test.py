# Copyright 2019 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for tfx.orchestration.airflow.e2e."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import subprocess
import time

import tensorflow as tf
from typing import Sequence, Set, Text
from tfx.examples.chicago_taxi_pipeline import taxi_utils
from tfx.utils import io_utils


class AirflowSubprocess(object):
  """Launch an Airflow command."""

  def __init__(self, airflow_args):
    args = ['airflow'] + airflow_args
    self._sub_process = subprocess.Popen(args)

  def stop(self):
    self._sub_process.terminate()


# Number of seconds between polling pending task states.
_TASK_POLLING_INTERVAL_SEC = 10
# Maximum duration to allow no task state change.
_MAX_TASK_STATE_CHANGE_SEC = 60


class AirflowEndToEndTest(tf.test.TestCase):
  """An end to end test using fully orchestrated Airflow."""

  def _get_state(self, task_name: Text) -> Text:
    """Get a task state as a string."""
    output = subprocess.check_output([
        'airflow', 'task_state', self._dag_id, task_name, self._execution_date
    ]).split()
    # Some logs are emitted to stdout, so we take the last word as state.
    return tf.compat.as_str(output[-1])

  # TODO(b/130882241): Add validation on output artifact type and content.
  def _check_output_artifacts(self, task: Text) -> None:
    pass

  def _check_pending_tasks(self,
                           pending_task_names: Sequence[Text]) -> Set[Text]:
    unknown_tasks = set(pending_task_names) - set(self._all_tasks)
    assert not unknown_tasks, 'Unknown task name {}'.format(unknown_tasks)
    still_pending = set()
    for task in pending_task_names:
      task_state = self._get_state(task).lower()
      if task_state in ['success']:
        tf.logging.info('Task %s succeeded, checking output artifacts', task)
        self._check_output_artifacts(task)
      else:
        assert task_state in [
            'queued',
            'scheduled',
            'running',
            'none',
        ], 'Task %s in unknown state %s' % (task, task_state)
        still_pending.add(task)
    return still_pending

  def setUp(self):
    super(AirflowEndToEndTest, self).setUp()
    # setup airflow_home in a temp directory, config and init db.
    self._airflow_home = os.path.join(
        os.environ.get('TEST_UNDECLARED_OUTPUTS_DIR', self.get_temp_dir()),
        self._testMethodName)
    self._old_airflow_home = os.environ.get('AIRFLOW_HOME')
    os.environ['AIRFLOW_HOME'] = self._airflow_home
    self._old_home = os.environ.get('HOME')
    os.environ['HOME'] = self._airflow_home
    tf.logging.info('Using %s as AIRFLOW_HOME and HOME in this e2e test',
                    self._airflow_home)
    # Set a couple of important environment variables. See
    # https://airflow.apache.org/howto/set-config.html for details.
    os.environ['AIRFLOW__CORE__AIRFLOW_HOME'] = self._airflow_home
    os.environ['AIRFLOW__CORE__DAGS_FOLDER'] = os.path.join(
        self._airflow_home, 'dags')
    os.environ['AIRFLOW__CORE__BASE_LOG_FOLDER'] = os.path.join(
        self._airflow_home, 'logs')
    os.environ['AIRFLOW__CORE__SQL_ALCHEMY_CONN'] = ('sqlite:///%s/airflow.db' %
                                                     self._airflow_home)
    # Following environment variables make scheduler process dags faster.
    os.environ['AIRFLOW__SCHEDULER__JOB_HEARTBEAT_SEC'] = '1'
    os.environ['AIRFLOW__SCHEDULER__SCHEDULER_HEARTBEAT_SEC'] = '1'
    os.environ['AIRFLOW__SCHEDULER__RUN_DURATION'] = '-1'
    os.environ['AIRFLOW__SCHEDULER__MIN_FILE_PROCESS_INTERVAL'] = '1'
    os.environ['AIRFLOW__SCHEDULER__PRINT_STATS_INTERVAL'] = '30'

    # Following fields are specific to the chicago_taxi_simple example.
    self._dag_id = 'chicago_taxi_simple'
    self._run_id = 'manual_run_id_1'
    # This execution date must be after the start_date in chicago_taxi_simple
    # but before current execution date.
    self._execution_date = '2019-02-01T01:01:01+01:01'
    self._all_tasks = [
        'CsvExampleGen',
        'Evaluator',
        'ExampleValidator',
        'ModelValidator',
        'Pusher',
        'SchemaGen',
        'StatisticsGen',
        'Trainer',
        'Transform',
    ]
    # Copy dag file and data.
    taxi_util_file = taxi_utils.__file__
    simple_pipeline_file = os.path.join(
        os.path.dirname(taxi_util_file), 'taxi_pipeline_simple.py')
    data_dir = os.path.join(os.path.dirname(taxi_util_file), 'data')
    io_utils.copy_file(
        simple_pipeline_file,
        os.path.join(self._airflow_home, 'dags', 'taxi_pipeline_simple.py'))
    io_utils.copy_dir(data_dir, os.path.join(self._airflow_home, 'taxi',
                                             'data'))
    io_utils.copy_file(
        taxi_util_file, os.path.join(self._airflow_home, 'taxi',
                                     'taxi_utils.py'))

    # Initialize database.
    _ = subprocess.check_output(['airflow', 'initdb'])
    _ = subprocess.check_output(['airflow', 'unpause', self._dag_id])

    # We will use subprocess to start the DAG instead of webserver, so only
    # starting a scheduler for now.
    self._scheduler = AirflowSubprocess(['scheduler'])

  def testSimplePipeline(self):
    _ = subprocess.check_output([
        'airflow',
        'trigger_dag',
        self._dag_id,
        '-r',
        self._run_id,
        '-e',
        self._execution_date,
    ])
    pending_tasks = set(self._all_tasks.copy())
    attempts = int(_MAX_TASK_STATE_CHANGE_SEC / _TASK_POLLING_INTERVAL_SEC) + 1
    while True:
      if not pending_tasks:
        tf.logging.info('No pending task left anymore')
        return
      for _ in range(attempts):
        tf.logging.debug('Polling task state')
        still_pending = self._check_pending_tasks(pending_tasks)
        if len(still_pending) != len(pending_tasks):
          pending_tasks = still_pending
          break
        tf.logging.info('Polling task state after %d secs',
                        _TASK_POLLING_INTERVAL_SEC)
        time.sleep(_TASK_POLLING_INTERVAL_SEC)
      else:
        self.fail('No pending tasks in %s finished within %d secs' %
                  (pending_tasks, _MAX_TASK_STATE_CHANGE_SEC))

  def tearDown(self):
    super(AirflowEndToEndTest, self).tearDown()
    if self._old_airflow_home:
      os.environ['AIRFLOW_HOME'] = self._old_airflow_home
    if self._old_home:
      os.environ['HOME'] = self._old_home
    if self._scheduler:
      self._scheduler.stop()


if __name__ == '__main__':
  tf.test.main()